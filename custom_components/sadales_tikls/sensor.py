"""Sensor platform for Sadales Tīkls.

Per object, exposes:

  consumption (kWh, derived from the in-memory snapshot — *not* the source
  for the Energy Dashboard; that's the external-statistics stream):
    - last_hour_consumption
    - today_consumption
    - yesterday_consumption
    - month_to_date_consumption
    - previous_month_consumption

  diagnostic:
    - data_lag (hours since the most recent reading)
    - last_hour_status (the cVRSt code)

All date/time boundaries are evaluated in `Europe/Riga`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfEnergy, UnitOfTime
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import RIGA_TZ, ObjectSnapshot, SadalesTiklsCoordinator

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .data import SadalesTiklsConfigEntry


# ---------------------------------------------------------------------------
# Value functions — pure, easy to unit-test.
# ---------------------------------------------------------------------------


def _last_hour_value(snap: ObjectSnapshot, _now: datetime) -> float | None:
    if not snap.hourly:
        return None
    last_key = max(snap.hourly)
    return round(snap.hourly[last_key], 3)


def _today_consumption(snap: ObjectSnapshot, now: datetime) -> float:
    today = now.date()
    return round(sum(v for h, v in snap.hourly.items() if h.date() == today), 3)


def _yesterday_consumption(snap: ObjectSnapshot, now: datetime) -> float:
    yesterday = (now - timedelta(days=1)).date()
    return round(sum(v for h, v in snap.hourly.items() if h.date() == yesterday), 3)


def _month_to_date(snap: ObjectSnapshot, now: datetime) -> float:
    return round(
        sum(v for h, v in snap.hourly.items() if h.year == now.year and h.month == now.month),
        3,
    )


def _previous_month(snap: ObjectSnapshot, now: datetime) -> float:
    if now.month == 1:
        py, pm = now.year - 1, 12
    else:
        py, pm = now.year, now.month - 1
    return round(
        sum(v for h, v in snap.hourly.items() if h.year == py and h.month == pm),
        3,
    )


def _data_lag_hours(snap: ObjectSnapshot, now: datetime) -> float | None:
    if not snap.hourly:
        return None
    last_end = max(snap.hourly) + timedelta(hours=1)  # end-of-hour
    return round((now - last_end).total_seconds() / 3600, 1)


def _last_hour_status(snap: ObjectSnapshot, _now: datetime) -> str | None:
    if not snap.statuses:
        return None
    return snap.statuses[max(snap.statuses)]


# ---------------------------------------------------------------------------
# Descriptions
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class SadalesTiklsSensorDescription(SensorEntityDescription):
    """Describes one of the per-object sensors."""

    value_fn: Callable[[ObjectSnapshot, datetime], float | str | None]


_ENERGY_KWH = UnitOfEnergy.KILO_WATT_HOUR


SENSOR_DESCRIPTIONS: tuple[SadalesTiklsSensorDescription, ...] = (
    SadalesTiklsSensorDescription(
        key="last_hour_consumption",
        translation_key="last_hour_consumption",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=_ENERGY_KWH,
        value_fn=_last_hour_value,
    ),
    SadalesTiklsSensorDescription(
        key="today_consumption",
        translation_key="today_consumption",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=_ENERGY_KWH,
        value_fn=_today_consumption,
    ),
    SadalesTiklsSensorDescription(
        key="yesterday_consumption",
        translation_key="yesterday_consumption",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=_ENERGY_KWH,
        value_fn=_yesterday_consumption,
    ),
    SadalesTiklsSensorDescription(
        key="month_to_date_consumption",
        translation_key="month_to_date_consumption",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=_ENERGY_KWH,
        value_fn=_month_to_date,
    ),
    SadalesTiklsSensorDescription(
        key="previous_month_consumption",
        translation_key="previous_month_consumption",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=_ENERGY_KWH,
        value_fn=_previous_month,
    ),
    SadalesTiklsSensorDescription(
        key="data_lag",
        translation_key="data_lag",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTime.HOURS,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_data_lag_hours,
    ),
    SadalesTiklsSensorDescription(
        key="last_hour_status",
        translation_key="last_hour_status",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_last_hour_status,
    ),
)


# ---------------------------------------------------------------------------
# Platform setup
# ---------------------------------------------------------------------------


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001 — required by the platform contract
    entry: SadalesTiklsConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.coordinator
    entities = [
        SadalesTiklsSensor(coordinator, o_eic, desc)
        for o_eic in coordinator.selected_oeics
        for desc in SENSOR_DESCRIPTIONS
    ]
    async_add_entities(entities)


# ---------------------------------------------------------------------------
# Entity
# ---------------------------------------------------------------------------


class SadalesTiklsSensor(CoordinatorEntity[SadalesTiklsCoordinator], SensorEntity):
    """One of the seven per-object sensors."""

    _attr_has_entity_name = True
    entity_description: SadalesTiklsSensorDescription

    def __init__(
        self,
        coordinator: SadalesTiklsCoordinator,
        o_eic: str,
        description: SadalesTiklsSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._o_eic = o_eic
        self._attr_unique_id = f"{o_eic}_{description.key}".lower()

        info = coordinator.objects_meta.get(o_eic)
        device_name = info["oName"] if info else o_eic
        device_addr = info["oAddr"] if info else None
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, o_eic)},
            name=device_name,
            manufacturer=MANUFACTURER,
            model=device_addr or None,
            serial_number=o_eic,
        )

    @property
    def native_value(self) -> float | str | None:
        snap = self.coordinator.data.get(self._o_eic) if self.coordinator.data else None
        if snap is None:
            return None
        return self.entity_description.value_fn(snap, datetime.now(RIGA_TZ))
