"""Polling and long-term statistics import for Energi Fyn."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import AuthError, EnergiFynClient
from .const import (
    CONF_CUSTOMER_ID,
    DOMAIN,
    INITIAL_BACKFILL_DAYS,
    UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

# The API reports local Danish wall-clock time without an offset.
DK = ZoneInfo("Europe/Copenhagen")


@dataclass
class Meter:
    """One electricity meter."""

    estate_id: int
    api_id: str  # '<number>-<installationId>'
    slug: str
    name: str


@dataclass
class MeterData:
    """Everything published for one meter. Missing values stay None."""

    # latest published day (data arrives about a day late)
    day: datetime | None = None
    day_kwh: float | None = None
    day_cost: float | None = None
    day_estimated: bool | None = None
    day_avg_price: float | None = None  # DKK per kWh, cost / kWh
    day_peak_kwh: float | None = None
    day_peak_hour: datetime | None = None
    # last 7 published days
    week_kwh: float | None = None
    week_cost: float | None = None
    # calendar months; "vs benchmark" is % above (+) or below (-) similar homes
    month_kwh: float | None = None
    month_cost: float | None = None
    month_benchmark: float | None = None
    month_vs_benchmark: float | None = None
    prev_month_kwh: float | None = None
    prev_month_cost: float | None = None
    prev_month_benchmark: float | None = None
    prev_month_vs_benchmark: float | None = None
    # meter / settlement
    last_reading: float | None = None
    last_reading_date: datetime | None = None
    reading_method: str | None = None
    estimated_annual_kwh: float | None = None
    # electricity product (tariff)
    product_name: str | None = None
    product_price: float | None = None
    product_price_unit: str | None = None
    product_from: datetime | None = None
    product_vat_added: bool | None = None
    # home profile
    area: int | None = None
    residents: int | None = None
    heating: str | None = None
    electric_cars: int | None = None


def _benchmark_pct(period: dict | None) -> float | None:
    """% above (+) / below (-) the benchmark. The API's benchmarkPercent is the
    magnitude only (e.g. 41.4 for 369.8 kWh vs a 261.5 benchmark), so derive the sign."""
    if not period or not period.get("benchmarkValue"):
        return None
    return round((period["consumption"] / period["benchmarkValue"] - 1) * 100, 1)


class EnergiFynCoordinator(DataUpdateCoordinator[dict[str, MeterData]]):
    """Fetch consumption and import hourly history as external statistics."""

    config_entry: ConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, client: EnergiFynClient
    ) -> None:
        super().__init__(
            hass, _LOGGER, config_entry=entry, name=DOMAIN, update_interval=UPDATE_INTERVAL
        )
        self.client = client
        self.customer_id: str = entry.data[CONF_CUSTOMER_ID]
        self.meters: list[Meter] = []

    async def _async_setup(self) -> None:
        try:
            estates = await self.client.estates(self.customer_id)
        except AuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except aiohttp.ClientError as err:
            raise UpdateFailed(f"Could not load meters: {err}") from err
        for estate in estates:
            for meter in estate["meters"]:
                if not meter.get("isActive"):
                    continue
                api_id = self.client.meter_id(meter)
                self.meters.append(
                    Meter(
                        estate_id=estate["estateId"],
                        api_id=api_id,
                        slug=re.sub(r"[^a-z0-9]+", "_", api_id.lower()),
                        name=meter.get("name") or api_id,
                    )
                )

    async def _async_update_data(self) -> dict[str, MeterData]:
        try:
            estates = {e["estateId"]: e for e in await self.client.estates(self.customer_id)}
            products = await self.client.products(self.customer_id)
            return {
                m.slug: await self._update_meter(m, estates.get(m.estate_id), products)
                for m in self.meters
            }
        except AuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except aiohttp.ClientError as err:
            raise UpdateFailed(f"Energi Fyn request failed: {err}") from err

    async def _update_meter(
        self, meter: Meter, estate: dict | None, products: list[dict]
    ) -> MeterData:
        hourly = await self._import_statistics(meter)
        data = MeterData()
        self._fill_estate(data, meter, estate)
        self._fill_product(data, meter, products)

        today = datetime.now(DK).replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0)
        month_start = today.replace(day=1)
        next_month = (month_start + timedelta(days=32)).replace(day=1)
        prev_start = (month_start - timedelta(days=1)).replace(day=1)

        month = await self._month(meter, month_start, next_month)
        prev = await self._month(meter, prev_start, month_start)
        for prefix, resp in (("month", month), ("prev_month", prev)):
            period = resp.get("period")
            if period:
                setattr(data, f"{prefix}_kwh", period["consumption"])
                setattr(data, f"{prefix}_cost", period["price"])
                setattr(data, f"{prefix}_benchmark", period.get("benchmarkValue"))
                setattr(data, f"{prefix}_vs_benchmark", _benchmark_pct(period))

        days = [
            v
            for resp in (prev, month)
            for v in resp["consumptionValues"]
            if v["meassurements"]
        ]
        if days:
            last = days[-1]
            data.day = datetime.fromisoformat(last["date"]).replace(tzinfo=DK)
            data.day_kwh = last["meassurements"][0]["kWh"]
            data.day_cost = last["price"]
            data.day_estimated = last.get("isEstimatedMeassurement")
            if data.day_kwh:
                data.day_avg_price = round(data.day_cost / data.day_kwh, 4)
            week = days[-7:]
            data.week_kwh = round(sum(v["meassurements"][0]["kWh"] for v in week), 3)
            data.week_cost = round(sum(v["price"] for v in week), 2)
            await self._fill_peak(data, meter, hourly)
        return data

    async def _month(self, meter: Meter, start: datetime, end: datetime) -> dict:
        return await self.client.consumption(
            self.customer_id, meter.estate_id, meter.api_id, start, end, hourly=False
        )

    async def _fill_peak(
        self, data: MeterData, meter: Meter, imported: list[tuple[datetime, float, float]]
    ) -> None:
        """Busiest hour of the latest published day."""
        day = data.day.replace(tzinfo=None)
        rows = [r for r in imported if r[0].astimezone(DK).date() == day.date()]
        if not rows:
            rows = await self._fetch_day(meter, data.day)
        if rows:
            start, kwh, _ = max(rows, key=lambda r: r[1])
            data.day_peak_kwh = kwh
            data.day_peak_hour = start

    @staticmethod
    def _fill_estate(data: MeterData, meter: Meter, estate: dict | None) -> None:
        if not estate:
            return
        data.area = estate.get("squareMeters")
        data.heating = estate.get("heating")
        res = estate.get("residents") or {}
        data.residents = sum(res.values()) if res else None
        data.electric_cars = (estate.get("electricCars") or {}).get("numberOfElectricCars")
        for m in estate.get("meters", []):
            if f"{m['number']}-{m['installationId']}" != meter.api_id:
                continue
            for counter in m.get("counters", []):
                if counter.get("unitType") != "KWH":
                    continue
                data.estimated_annual_kwh = counter.get("estimatedConsumption")
                data.reading_method = counter.get("readingMethod")
                reading = counter.get("latestReading")
                if reading:
                    data.last_reading = reading["value"]
                    data.last_reading_date = datetime.fromisoformat(reading["date"]).replace(
                        tzinfo=DK
                    )
                break

    @staticmethod
    def _fill_product(data: MeterData, meter: Meter, products: list[dict]) -> None:
        """The current (no end date) main electricity product for this installation."""
        installation = meter.api_id.split("-", 1)[1]
        current = [
            p
            for p in products
            if p.get("productType") == "El"
            and p.get("toDate") is None
            and str(p.get("installationNumber")) == installation
        ]
        current.sort(key=lambda p: p.get("productLevel") != "Main")
        if not current:
            return
        p = current[0]
        data.product_name = p["name"]
        data.product_price = p.get("price")
        data.product_price_unit = p.get("priceUnit")
        data.product_vat_added = p.get("priceAddVat")
        if p.get("fromDate"):
            data.product_from = datetime.fromisoformat(p["fromDate"]).replace(tzinfo=DK)

    # ---- statistics -----------------------------------------------------

    async def _import_statistics(
        self, meter: Meter
    ) -> list[tuple[datetime, float, float]]:
        """Import new hours; return the hourly rows fetched this run."""
        kwh_id = f"{DOMAIN}:{meter.slug}_consumption"
        cost_id = f"{DOMAIN}:{meter.slug}_cost"

        last = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, kwh_id, True, {"start"}
        )
        today = datetime.now(DK).replace(hour=0, minute=0, second=0, microsecond=0)
        if last.get(kwh_id):
            # Re-import the last stored day too: Energi Fyn revises estimated hours.
            first_day = datetime.fromtimestamp(last[kwh_id][0]["start"], DK).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
        else:
            first_day = today - timedelta(days=INITIAL_BACKFILL_DAYS)

        # Today has no data yet (the API answers 500), so stop at yesterday.
        hourly: list[tuple[datetime, float, float]] = []
        day = first_day
        while day < today:
            hourly.extend(await self._fetch_day(meter, day))
            day += timedelta(days=1)
        if not hourly:
            return []

        hourly.sort(key=lambda row: row[0])
        window_start = hourly[0][0]
        base_kwh = await self._sum_before(kwh_id, window_start)
        base_cost = await self._sum_before(cost_id, window_start)

        kwh_stats: list[StatisticData] = []
        cost_stats: list[StatisticData] = []
        for start, kwh, cost in hourly:
            base_kwh += kwh
            base_cost += cost
            kwh_stats.append(StatisticData(start=start, state=kwh, sum=base_kwh))
            cost_stats.append(StatisticData(start=start, state=cost, sum=base_cost))

        async_add_external_statistics(
            self.hass,
            StatisticMetaData(
                mean_type=StatisticMeanType.NONE,
                has_sum=True,
                name=f"{meter.name} consumption",
                source=DOMAIN,
                statistic_id=kwh_id,
                unit_class="energy",
                unit_of_measurement="kWh",
            ),
            kwh_stats,
        )
        async_add_external_statistics(
            self.hass,
            StatisticMetaData(
                mean_type=StatisticMeanType.NONE,
                has_sum=True,
                name=f"{meter.name} cost",
                source=DOMAIN,
                statistic_id=cost_id,
                unit_class=None,
                unit_of_measurement="DKK",
            ),
            cost_stats,
        )
        return hourly

    async def _fetch_day(
        self, meter: Meter, day: datetime
    ) -> list[tuple[datetime, float, float]]:
        """Hourly (start_utc, kWh, DKK) rows for one local day."""
        naive = day.replace(tzinfo=None)
        try:
            resp = await self.client.consumption(
                self.customer_id,
                meter.estate_id,
                meter.api_id,
                naive,
                naive + timedelta(days=1),
                hourly=True,
            )
        except aiohttp.ClientResponseError as err:
            if err.status == 500:  # "Consumption could not be found" for that day
                _LOGGER.debug("No consumption for %s on %s", meter.api_id, naive.date())
                return []
            raise
        rows = []
        seen: set[datetime] = set()
        for entry in resp["consumptionValues"]:
            if not entry["meassurements"]:
                continue
            start = dt_util.as_utc(datetime.fromisoformat(entry["date"]).replace(tzinfo=DK))
            if start in seen:  # ambiguous local hour on the DST fall-back day
                continue
            seen.add(start)
            rows.append((start, entry["meassurements"][0]["kWh"], entry["price"]))
        return rows

    async def _sum_before(self, statistic_id: str, start: datetime) -> float:
        """Cumulative sum as of the last stored hour before `start`, or 0 if none."""
        stats = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            start - timedelta(days=2),
            start,
            {statistic_id},
            "hour",
            None,
            {"sum"},
        )
        rows = stats.get(statistic_id)
        return float(rows[-1]["sum"]) if rows and rows[-1].get("sum") is not None else 0.0
