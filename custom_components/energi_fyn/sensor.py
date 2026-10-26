"""Sensors for Energi Fyn."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfArea, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import EnergiFynConfigEntry
from .const import DOMAIN
from .coordinator import DK, EnergiFynCoordinator, Meter, MeterData


@dataclass(frozen=True, kw_only=True)
class EnergiFynSensorDescription(SensorEntityDescription):
    value_fn: Callable[[MeterData], StateType | datetime]
    attr_fn: Callable[[MeterData], dict[str, Any]] | None = None


_KWH = UnitOfEnergy.KILO_WATT_HOUR


def _energy(key: str, field: str, precision: int = 2, **kw: Any) -> EnergiFynSensorDescription:
    return EnergiFynSensorDescription(
        key=key,
        translation_key=key,
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=_KWH,
        suggested_display_precision=precision,
        value_fn=lambda d: getattr(d, field),
        **kw,
    )


def _cost(key: str, field: str) -> EnergiFynSensorDescription:
    return EnergiFynSensorDescription(
        key=key,
        translation_key=key,
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="DKK",
        suggested_display_precision=2,
        value_fn=lambda d: getattr(d, field),
    )


def _plain(key: str, field: str, **kw: Any) -> EnergiFynSensorDescription:
    return EnergiFynSensorDescription(
        key=key, translation_key=key, value_fn=lambda d: getattr(d, field), **kw
    )


def _day_attrs(d: MeterData) -> dict[str, Any]:
    return {
        "date": d.day.date().isoformat() if d.day else None,
        "estimated": d.day_estimated,
    }


_STATIC = {
    "entity_category": EntityCategory.DIAGNOSTIC,
    "entity_registry_enabled_default": False,
}

SENSORS: tuple[EnergiFynSensorDescription, ...] = (
    # latest published day
    _energy("day_consumption", "day_kwh", attr_fn=_day_attrs),
    _cost("day_cost", "day_cost"),
    _plain(
        "day_avg_price",
        "day_avg_price",
        native_unit_of_measurement="DKK/kWh",
        suggested_display_precision=3,
    ),
    EnergiFynSensorDescription(
        key="day_peak_hour",
        translation_key="day_peak_hour",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=_KWH,
        suggested_display_precision=2,
        value_fn=lambda d: d.day_peak_kwh,
        attr_fn=lambda d: {
            "hour_start": d.day_peak_hour.astimezone(DK).isoformat() if d.day_peak_hour else None
        },
    ),
    # last 7 published days
    _energy("week_consumption", "week_kwh", 1),
    _cost("week_cost", "week_cost"),
    # months
    _energy("month_consumption", "month_kwh", 1),
    _cost("month_cost", "month_cost"),
    _energy("month_benchmark", "month_benchmark", 1),
    _plain(
        "month_vs_benchmark",
        "month_vs_benchmark",
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=1,
    ),
    _energy("prev_month_consumption", "prev_month_kwh", 1),
    _cost("prev_month_cost", "prev_month_cost"),
    _energy("prev_month_benchmark", "prev_month_benchmark", 1),
    _plain(
        "prev_month_vs_benchmark",
        "prev_month_vs_benchmark",
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=1,
    ),
    # meter / settlement
    _energy(
        "last_meter_reading",
        "last_reading",
        0,
        entity_category=EntityCategory.DIAGNOSTIC,
        attr_fn=lambda d: {
            "reading_date": d.last_reading_date.date().isoformat() if d.last_reading_date else None,
            "reading_method": d.reading_method,
        },
    ),
    _energy(
        "estimated_annual_consumption",
        "estimated_annual_kwh",
        0,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # electricity product
    _plain(
        "product",
        "product_name",
        entity_category=EntityCategory.DIAGNOSTIC,
        attr_fn=lambda d: {
            "since": d.product_from.date().isoformat() if d.product_from else None
        },
    ),
    # Energi Fyn only labels this price "Øre"; what it is per is not stated, so don't claim a rate.
    _plain(
        "product_price",
        "product_price",
        native_unit_of_measurement="øre",
        entity_category=EntityCategory.DIAGNOSTIC,
        attr_fn=lambda d: {
            "price_unit_from_api": d.product_price_unit,
            "vat_added_on_top": d.product_vat_added,
        },
    ),
    # home profile (static)
    _plain("home_area", "area", native_unit_of_measurement=UnitOfArea.SQUARE_METERS, **_STATIC),
    _plain("residents", "residents", **_STATIC),
    _plain("heating", "heating", **_STATIC),
    _plain("electric_cars", "electric_cars", **_STATIC),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnergiFynConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        EnergiFynSensor(coordinator, meter, description)
        for meter in coordinator.meters
        for description in SENSORS
    )


class EnergiFynSensor(CoordinatorEntity[EnergiFynCoordinator], SensorEntity):
    """Latest published day, or the month to date, for one meter."""

    _attr_has_entity_name = True
    entity_description: EnergiFynSensorDescription

    def __init__(
        self,
        coordinator: EnergiFynCoordinator,
        meter: Meter,
        description: EnergiFynSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._meter = meter
        self._attr_unique_id = f"{coordinator.customer_id}_{meter.slug}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{coordinator.customer_id}_{meter.slug}")},
            name=meter.name,
            manufacturer="Energi Fyn",
            model="Electricity meter",
        )

    @property
    def native_value(self) -> StateType | datetime:
        return self.entity_description.value_fn(self.coordinator.data[self._meter.slug])

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        fn = self.entity_description.attr_fn
        return fn(self.coordinator.data[self._meter.slug]) if fn else None
