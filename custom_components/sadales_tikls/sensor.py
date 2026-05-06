"""Sensor platform for Sadales Tīkls.

Per object, exposes:

  Visible (one card per object on the device page):
    - yesterday_consumption       state: kWh yesterday, attrs: hourly[]
    - month_to_date_consumption   state: kWh MTD,       attrs: daily[]
    - previous_month_consumption  state: kWh prev mo,   attrs: daily[]

  Diagnostic (auto-collapsed under Diagnostics):
    - most_recent_hour_consumption  state: kWh of the most recent published hour
    - data_lag                      state: hours since the most recent reading
    - most_recent_hour_status       state: cVRSt code of the most recent hour

The `hourly` / `daily` attributes are the data source for ApexCharts cards
(or any other chart). Each entry has `start` (or `date`) and `value`, plus
`status` for hourly entries. State values are derived from the same in-memory
snapshot — sensors never query the recorder.

`Today` is intentionally NOT a sensor: Sadales Tīkls publishes hourly data
with a 1–24h lag, so "today" would always be partial / often zero, which is
misleading. The MTD sensor's `daily` attribute already contains today as its
last entry for users who want the running tally.

All date/time boundaries are evaluated in `Europe/Riga`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

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


def _most_recent_hour_value(snap: ObjectSnapshot, _now: datetime) -> float | None:
    if not snap.hourly:
        return None
    last_key = max(snap.hourly)
    return round(snap.hourly[last_key], 3)


def _yesterday_consumption(snap: ObjectSnapshot, now: datetime) -> float:
    yesterday = (now - timedelta(days=1)).date()
    return round(sum(v for h, v in snap.hourly.items() if h.date() == yesterday), 3)


def _month_to_date(snap: ObjectSnapshot, now: datetime) -> float:
    return round(
        sum(v for h, v in snap.hourly.items() if h.year == now.year and h.month == now.month),
        3,
    )


def _previous_month(snap: ObjectSnapshot, now: datetime) -> float:
    py, pm = _previous_month_yearmonth(now)
    return round(
        sum(v for h, v in snap.hourly.items() if h.year == py and h.month == pm),
        3,
    )


def _data_lag_hours(snap: ObjectSnapshot, now: datetime) -> float | None:
    if not snap.hourly:
        return None
    last_end = max(snap.hourly) + timedelta(hours=1)  # end-of-hour
    return round((now - last_end).total_seconds() / 3600, 1)


def _most_recent_hour_status(snap: ObjectSnapshot, _now: datetime) -> str | None:
    if not snap.statuses:
        return None
    return snap.statuses[max(snap.statuses)]


# ---------------------------------------------------------------------------
# Attribute functions — produce the chart-friendly breakdowns.
# ---------------------------------------------------------------------------


def _hourly_for_date(snap: ObjectSnapshot, date_obj: Any) -> list[dict[str, Any]]:
    return [
        {
            "start": h.isoformat(),
            "value": round(v, 3),
            "status": snap.statuses.get(h, ""),
        }
        for h, v in sorted(snap.hourly.items())
        if h.date() == date_obj
    ]


def _yesterday_attrs(snap: ObjectSnapshot, now: datetime) -> dict[str, Any]:
    return {"hourly": _hourly_for_date(snap, (now - timedelta(days=1)).date())}


def _daily_for_year_month(snap: ObjectSnapshot, year: int, month: int) -> list[dict[str, Any]]:
    daily: dict[str, float] = {}
    for h, v in snap.hourly.items():
        if h.year == year and h.month == month:
            d = h.date().isoformat()
            daily[d] = daily.get(d, 0.0) + v
    return [{"date": d, "value": round(v, 3)} for d, v in sorted(daily.items())]


def _mtd_attrs(snap: ObjectSnapshot, now: datetime) -> dict[str, Any]:
    return {"daily": _daily_for_year_month(snap, now.year, now.month)}


def _previous_month_attrs(snap: ObjectSnapshot, now: datetime) -> dict[str, Any]:
    py, pm = _previous_month_yearmonth(now)
    return {"daily": _daily_for_year_month(snap, py, pm)}


def _previous_month_yearmonth(now: datetime) -> tuple[int, int]:
    if now.month == 1:
        return now.year - 1, 12
    return now.year, now.month - 1


# ---------------------------------------------------------------------------
# Descriptions
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class SadalesTiklsSensorDescription(SensorEntityDescription):
    """Describes one of the per-object sensors."""

    value_fn: Callable[[ObjectSnapshot, datetime], float | str | None]
    attrs_fn: Callable[[ObjectSnapshot, datetime], dict[str, Any]] | None = None


_ENERGY_KWH = UnitOfEnergy.KILO_WATT_HOUR


SENSOR_DESCRIPTIONS: tuple[SadalesTiklsSensorDescription, ...] = (
    # --- Visible (per-period totals with sub-granularity in attributes) ----
    SadalesTiklsSensorDescription(
        key="yesterday_consumption",
        translation_key="yesterday_consumption",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=_ENERGY_KWH,
        value_fn=_yesterday_consumption,
        attrs_fn=_yesterday_attrs,
    ),
    SadalesTiklsSensorDescription(
        key="month_to_date_consumption",
        translation_key="month_to_date_consumption",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=_ENERGY_KWH,
        value_fn=_month_to_date,
        attrs_fn=_mtd_attrs,
    ),
    SadalesTiklsSensorDescription(
        key="previous_month_consumption",
        translation_key="previous_month_consumption",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=_ENERGY_KWH,
        value_fn=_previous_month,
        attrs_fn=_previous_month_attrs,
    ),
    # --- Diagnostic (auto-collapsed) -------------------------------------
    SadalesTiklsSensorDescription(
        key="most_recent_hour_consumption",
        translation_key="most_recent_hour_consumption",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=_ENERGY_KWH,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_most_recent_hour_value,
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
        key="most_recent_hour_status",
        translation_key="most_recent_hour_status",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_most_recent_hour_status,
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
    """One of the per-object sensors."""

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
    def _snapshot(self) -> ObjectSnapshot | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get(self._o_eic)

    @property
    def native_value(self) -> float | str | None:
        snap = self._snapshot
        if snap is None:
            return None
        return self.entity_description.value_fn(snap, datetime.now(RIGA_TZ))

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs_fn is None:
            return None
        snap = self._snapshot
        if snap is None:
            return None
        return self.entity_description.attrs_fn(snap, datetime.now(RIGA_TZ))
