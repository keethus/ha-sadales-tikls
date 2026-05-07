"""DataUpdateCoordinator for Sadales Tīkls.

Refresh strategy
----------------
* **First refresh in this session** (`self.data is None`): pull
  `backfill_days` worth of history. If HA already has stats from a prior
  session, the statistics writer will overwrite the same hours with
  identical sums — no harm done.
* **Daily catch-up** (≥ 23h since the last catch-up): pull the last 7 days
  to absorb retroactive corrections (`cVRSt = D / M`).
* **Normal poll**: pull the last 26 hours, comfortably covering the
  1h–1d publication lag.

All datetimes that flow through the API and the in-memory snapshot are
**timezone-aware in `Europe/Riga`**. `cDt` from the wire uses end-of-hour
convention (hour 00–01 ⇒ `cDt 01:00`); we shift by −1h to get the
HA-statistics `start`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import SadalesTiklsAuthError, SadalesTiklsError
from .const import (
    CONF_BACKFILL_DAYS,
    CONF_CONSUMPTION_FIELD,
    CONF_OBJECTS,
    CONF_UPDATE_INTERVAL,
    CONSUMPTION_FIELD_BILLING,
    DAILY_CATCHUP_DAYS,
    DEFAULT_BACKFILL_DAYS,
    DEFAULT_CONSUMPTION_FIELD,
    DEFAULT_UPDATE_INTERVAL_MIN,
    DOMAIN,
    RECENT_FETCH_HOURS,
    SKIPPED_STATUSES,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .api import ConsumptionResponse, ObjectInfo, SadalesTiklsAPI
    from .data import SadalesTiklsConfigEntry

_LOGGER = logging.getLogger(__name__)

# Latvia / Sadales Tīkls service zone. All wire timestamps and sensor date
# boundaries use this TZ.
RIGA_TZ = ZoneInfo("Europe/Riga")

# Keep at most ~62 days of hourly data in memory — enough for the
# previous-month sensor with margin.
_IN_MEMORY_RETENTION = timedelta(days=62)

# Catchup-cooldown: do the 7-day pull at most once per ~23h.
_CATCHUP_INTERVAL = timedelta(hours=23)


@dataclass(slots=True)
class ObjectSnapshot:
    """In-memory consumption data for one object, used by sensors."""

    info: ObjectInfo
    # Start-of-hour (Riga TZ) → kWh. Hours with cVRSt in SKIPPED_STATUSES
    # are *excluded* — sensors and stats simply have no value for them.
    hourly: dict[datetime, float] = field(default_factory=dict)
    statuses: dict[datetime, str] = field(default_factory=dict)


type CoordinatorData = dict[str, ObjectSnapshot]


class SadalesTiklsCoordinator(DataUpdateCoordinator[CoordinatorData]):
    """Per-entry consumption fetcher."""

    config_entry: SadalesTiklsConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: SadalesTiklsConfigEntry,
        api: SadalesTiklsAPI,
        objects_meta: dict[str, ObjectInfo],
    ) -> None:
        self.api = api
        self.objects_meta = objects_meta
        self.selected_oeics: list[str] = list(entry.data[CONF_OBJECTS])
        self._last_catchup: datetime | None = None

        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(
                minutes=int(entry.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL_MIN))
            ),
        )

    @property
    def consumption_field(self) -> str:
        return str(self.config_entry.options.get(CONF_CONSUMPTION_FIELD, DEFAULT_CONSUMPTION_FIELD))

    @property
    def backfill_days(self) -> int:
        return int(self.config_entry.options.get(CONF_BACKFILL_DAYS, DEFAULT_BACKFILL_DAYS))

    async def _async_update_data(self) -> CoordinatorData:
        # Local import: statistics.py imports ObjectSnapshot from this module.
        from .statistics import async_write_object_statistics  # noqa: PLC0415

        now = datetime.now(RIGA_TZ)
        is_first = self.data is None
        do_catchup = is_first or self._needs_catchup(now)

        if is_first:
            d_from = now - timedelta(days=self.backfill_days)
        elif do_catchup:
            d_from = now - timedelta(days=DAILY_CATCHUP_DAYS)
        else:
            d_from = now - timedelta(hours=RECENT_FETCH_HOURS)

        d_from = d_from.replace(minute=0, second=0, microsecond=0)
        d_to = now

        new_data: CoordinatorData = dict(self.data) if self.data is not None else {}
        had_auth_error = False

        for o_eic in self.selected_oeics:
            try:
                response = await self.api.get_object_consumption(o_eic, d_from, d_to)
            except SadalesTiklsAuthError:
                had_auth_error = True
                break
            except SadalesTiklsError as err:
                _LOGGER.warning("Sadales Tīkls fetch failed for object %s: %s", o_eic, err)
                continue

            snapshot = self._merge_into_snapshot(o_eic, new_data.get(o_eic), response)
            new_data[o_eic] = snapshot

            await async_write_object_statistics(
                self.hass,
                snapshot,
                window_from=d_from,
                window_to=d_to,
                full_recompute=do_catchup,
            )

        if had_auth_error:
            raise ConfigEntryAuthFailed("Sadales Tīkls APIKEY rejected")

        if not new_data:
            raise UpdateFailed("All Sadales Tīkls objects failed to fetch")

        if do_catchup:
            self._last_catchup = now

        return new_data

    def _needs_catchup(self, now: datetime) -> bool:
        return self._last_catchup is None or (now - self._last_catchup) >= _CATCHUP_INTERVAL

    def _merge_into_snapshot(
        self,
        o_eic: str,
        existing: ObjectSnapshot | None,
        response: ConsumptionResponse,
    ) -> ObjectSnapshot:
        info = self.objects_meta.get(o_eic)
        snapshot = (
            existing
            if existing is not None
            else ObjectSnapshot(info=info or _placeholder_info(o_eic))
        )
        if info is not None:
            snapshot.info = info

        field_name = self.consumption_field

        # An object can have multiple meters (e.g. a building with separate
        # main + auxiliary meters). The API returns each meter as its own
        # entry; the *object's* consumption is the SUM across all meters
        # for the same hour. Aggregating per response and then writing back
        # avoids the trap of overwriting one meter's value with another's.
        incoming: dict[datetime, float] = {}
        incoming_statuses: dict[datetime, str] = {}

        for mp in response:
            for meter in mp["mList"]:
                for entry in meter["cList"]:
                    # cVRSt is only present when the reading has a flag
                    # ("D" adjusted, "M" rounding-corrected, "U" unusable,
                    # "N" comm error, etc.). Absence == normal good reading.
                    status = entry.get("cVRSt") or ""
                    if status in SKIPPED_STATUSES:
                        continue

                    cdt_str = entry.get("cDt")
                    if not cdt_str:
                        _LOGGER.debug("Skipping consumption entry without cDt: %r", entry)
                        continue
 
                    # Pick the configured field, fall back to the other if
                    # the API only sent one of the pair.
                    raw = (
                        entry.get("cVV")
                        if field_name == CONSUMPTION_FIELD_BILLING
                        else entry.get("cVR")
                    )
                    if raw is None:
                        raw = (
                            entry.get("cVR")
                            if field_name == CONSUMPTION_FIELD_BILLING
                            else entry.get("cVV")
                        )
                    if raw is None:
                        _LOGGER.debug(
                            "Skipping consumption entry without cVR/cVV: %r",
                            entry,
                        )
                        continue

                    try:
                        cdt_end = datetime.fromisoformat(cdt_str).astimezone(RIGA_TZ)
                    except (TypeError, ValueError):
                        _LOGGER.debug(
                            "Skipping consumption entry with bad cDt: %r",
                            cdt_str,
                        )
                        continue

                    # cDt is supposed to be exactly on the hour. Real
                    # responses occasionally include microseconds or are
                    # off by a fraction of a second; HA's recorder rejects
                    # timestamps where minute/second != 0. Snap to the hour
                    # — also makes our in-memory keys hour-aligned so
                    # retroactive corrections match the same dict key.
                    cdt_end = cdt_end.replace(minute=0, second=0, microsecond=0)
                    start = cdt_end - timedelta(hours=1)

                    incoming[start] = incoming.get(start, 0.0) + float(raw)
                    # Status: surface any meter's flag if present at this
                    # hour; otherwise empty (= unflagged across all meters).
                    if status or start not in incoming_statuses:
                        incoming_statuses[start] = status

        # Replace the snapshot's hours that this response covers. Hours not
        # present in the response keep their existing values — important so
        # a 26h "normal poll" doesn't wipe out the 30-day backfill.
        for hour, value in incoming.items():
            snapshot.hourly[hour] = value
            snapshot.statuses[hour] = incoming_statuses.get(hour, "")

        # Bound memory.
        cutoff = datetime.now(RIGA_TZ) - _IN_MEMORY_RETENTION
        for stale in [k for k in snapshot.hourly if k < cutoff]:
            snapshot.hourly.pop(stale, None)
            snapshot.statuses.pop(stale, None)

        return snapshot


def _placeholder_info(o_eic: str) -> ObjectInfo:
    """Synthesize an ObjectInfo when /get-object-list metadata is missing."""
    return {
        "oEIC": o_eic,
        "oName": o_eic,
        "oAddr": "",
        "oStatus": "A",
        "mpList": [],
    }
