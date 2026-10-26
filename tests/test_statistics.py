"""Run the real coordinator against an in-memory recorder with a fake API."""
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import aiohttp
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.energi_fyn.api import EnergiFynClient
from custom_components.energi_fyn.const import DOMAIN
from custom_components.energi_fyn.coordinator import DK, EnergiFynCoordinator


def _day(day: datetime, skip_hours=(), kwh=1.0):
    return {
        "period": None,
        "consumptionValues": [
            {
                "date": (day + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%S"),
                "meassurements": [{"kWh": kwh}],
                "price": 2.0,
            }
            for h in range(24)
            if h not in skip_hours
        ],
    }


def _fake_client(calls):
    client = MagicMock(spec=EnergiFynClient)
    client.meter_id = EnergiFynClient.meter_id
    client.estates = AsyncMock(
        return_value=[
            {
                "estateId": 1,
                "squareMeters": 150,
                "heating": "Fjernvarme",
                "residents": {"adults": 2, "teenagers": 1, "children": 1},
                "electricCars": {"numberOfElectricCars": 0},
                "meters": [
                    {
                        "number": "3",
                        "installationId": "9",
                        "isActive": True,
                        "name": "Elmåler",
                        "counters": [
                            {
                                "unitType": "KWH",
                                "estimatedConsumption": 4753.0,
                                "readingMethod": "Net",
                                "latestReading": {"value": 22785.0, "date": "2022-03-31T00:00:00"},
                            }
                        ],
                    }
                ],
            }
        ]
    )
    client.products = AsyncMock(
        return_value=[
            {"name": "SpotEl", "productType": "El", "toDate": None, "installationNumber": "9",
             "productLevel": "Main", "price": 105.34, "priceUnit": "Øre", "priceAddVat": True,
             "fromDate": "2022-11-01T00:00:00"},
            {"name": "Old", "productType": "El", "toDate": "2022-10-31T23:59:59", "installationNumber": "9"},
            {"name": "Fiber", "productType": "Internet", "toDate": None, "installationNumber": "70"},
        ]
    )

    async def consumption(cust, estate, meter, start, end, hourly):
        calls.append((start, end, hourly))
        if not hourly:  # month view
            days = [
                _day(start)["consumptionValues"][0]
                | {"date": (start + timedelta(days=i)).strftime("%Y-%m-%dT00:00:00")}
                | {"meassurements": [{"kWh": 10.0 + i}], "price": 20.0 + i}
                for i in range(9)
            ]
            return {
                "period": {"consumption": 150.0, "price": 300.0, "benchmarkValue": 100.0},
                "consumptionValues": days,
            }
        if start.date() >= datetime.now(DK).date():
            raise aiohttp.ClientResponseError(MagicMock(), (), status=500)
        return _day(start, skip_hours=(2,))  # a gap must not break the sum

    client.consumption = AsyncMock(side_effect=consumption)
    return client


async def test_import_is_cumulative_and_idempotent(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant
):
    entry = MockConfigEntry(domain=DOMAIN, data={"customer_id": "1", "refresh_token": "x", "username": "u"})
    entry.add_to_hass(hass)
    calls = []
    coord = EnergiFynCoordinator(hass, entry, _fake_client(calls))
    await coord._async_setup()
    assert coord.meters[0].slug == "3_9"

    data = await coord._async_update_data()
    await async_wait_recording_done(hass)
    d = data["3_9"]
    # month and previous month are both served by the same fake: 9 days of 10..18 kWh
    assert d.month_kwh == 150.0 and d.month_vs_benchmark == 50.0
    assert d.prev_month_kwh == 150.0
    assert d.day_kwh == 18.0 and d.day_cost == 28.0 and d.day_avg_price == round(28 / 18, 4)
    assert d.week_kwh == 7 * 0 + sum(10.0 + i for i in range(2, 9))
    assert d.product_name == "SpotEl" and d.product_price == 105.34 and d.product_vat_added
    assert d.last_reading == 22785.0 and d.estimated_annual_kwh == 4753.0
    assert (d.area, d.residents, d.heating, d.electric_cars) == (150, 4, "Fjernvarme", 0)
    assert d.day_peak_kwh == 1.0 and d.day_peak_hour is not None

    sid = f"{DOMAIN}:3_9_consumption"
    start = dt_util.utcnow() - timedelta(days=40)
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, start, None, {sid}, "hour", None, {"sum", "state"}
    )
    rows = stats[sid]
    assert len(rows) == 30 * 23  # 30 backfilled days, 23 hours each (hour 2 missing)
    sums = [r["sum"] for r in rows]
    assert sums == sorted(sums) and sums[-1] == 30 * 23 * 1.0

    # second run re-imports the last day and must not double count
    await coord._async_update_data()
    await async_wait_recording_done(hass)
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, start, None, {sid}, "hour", None, {"sum"}
    )
    assert stats[sid][-1]["sum"] == 30 * 23 * 1.0
    cost = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, start, None, {f"{DOMAIN}:3_9_cost"}, "hour", None, {"sum"}
    )
    assert cost[f"{DOMAIN}:3_9_cost"][-1]["sum"] == 30 * 23 * 2.0


async def test_full_setup_creates_sensors(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant
):
    from unittest.mock import patch

    from homeassistant.helpers import entity_registry as er

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1",
        data={"customer_id": "1", "refresh_token": "x", "username": "u"},
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.energi_fyn.EnergiFynClient", return_value=_fake_client([])
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    reg = er.async_get(hass)
    entries = er.async_entries_for_config_entry(reg, entry.entry_id)
    by_key = {e.unique_id.split("_", 3)[-1]: e for e in entries}
    assert len(entries) == 22
    day = hass.states.get(by_key["day_consumption"].entity_id)
    assert day.state == "18.0" and day.attributes["date"]
    assert day.attributes["device_class"] == "energy"
    assert hass.states.get(by_key["month_vs_benchmark"].entity_id).state == "50.0"
    assert hass.states.get(by_key["product"].entity_id).state == "SpotEl"
    # static home profile sensors exist but are disabled until the user opts in
    assert by_key["home_area"].disabled_by is not None
    assert hass.states.get(by_key["home_area"].entity_id) is None
