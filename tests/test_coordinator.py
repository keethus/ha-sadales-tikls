"""Tests for `coordinator.py` — the wire-format → in-memory merge plus the
refresh strategy (first / catchup / normal).

The math-heavy parts (sensor value functions, sum-recompute window) live in
`test_sensor.py` and `test_statistics.py`.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

import pytest
from aioresponses import aioresponses
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sadales_tikls.const import (
    API_BASE_URL,
    API_ENDPOINT_OBJECT_CONSUMPTION,
    API_ENDPOINT_OBJECT_LIST,
    CONF_BACKFILL_DAYS,
    CONF_CONSUMPTION_FIELD,
    CONF_OBJECTS,
    CONF_UPDATE_INTERVAL,
    CONSUMPTION_FIELD_RAW,
    DEFAULT_BACKFILL_DAYS,
    DEFAULT_CONSUMPTION_FIELD,
    DEFAULT_UPDATE_INTERVAL_MIN,
    DOMAIN,
)
from custom_components.sadales_tikls.coordinator import RIGA_TZ, ObjectSnapshot

OBJECT_LIST_URL = f"{API_BASE_URL}{API_ENDPOINT_OBJECT_LIST}"
CONSUMPTION_URL_RE = re.compile(
    rf"^{re.escape(API_BASE_URL)}{re.escape(API_ENDPOINT_OBJECT_CONSUMPTION)}(\?.*)?$"
)

API_KEY = "valid-test-key"
TARGET_OEIC = "12X-OBJ-OFFICE-RIGA0"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entry(*, options: dict[str, Any] | None = None) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="12X-CUSTOMER-EXAMPLE0",
        data={CONF_API_KEY: API_KEY, CONF_OBJECTS: [TARGET_OEIC]},
        options=options
        or {
            CONF_UPDATE_INTERVAL: DEFAULT_UPDATE_INTERVAL_MIN,
            CONF_BACKFILL_DAYS: DEFAULT_BACKFILL_DAYS,
            CONF_CONSUMPTION_FIELD: DEFAULT_CONSUMPTION_FIELD,
        },
    )


def _consumption(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Wrap raw cList entries in the API's mp/m envelope."""
    return [{"mpNr": "MP", "mList": [{"mNr": "M", "cList": entries}]}]


def _entry_at(hour_end_riga: datetime, value: float, status: str = "C") -> dict[str, Any]:
    """One ConsumptionEntry — `hour_end_riga` is the end-of-hour timestamp
    used as `cDt` (in Riga TZ)."""
    return {
        "cDt": hour_end_riga.astimezone(RIGA_TZ).isoformat(),
        "cVR": value + 0.5,  # different from cVV so we can detect which is read
        "cVV": value,
        "cVRSt": status,
    }


# ---------------------------------------------------------------------------
# End-to-end through `async_setup_entry` — coordinator's first refresh fires.
# ---------------------------------------------------------------------------


async def test_first_refresh_populates_snapshot_with_billing_value(
    hass: HomeAssistant, object_list_payload: dict[str, Any]
) -> None:
    """cDt end-of-hour is shifted to start-of-hour, cVV (default) is read,
    statuses captured."""
    entry = _entry()
    entry.add_to_hass(hass)

    # 03:00–04:00 EEST consumption reported as cDt 04:00, value 1.234
    hour_start = datetime(2026, 5, 5, 3, tzinfo=RIGA_TZ)
    hour_end = hour_start + timedelta(hours=1)

    with aioresponses() as m:
        m.get(OBJECT_LIST_URL, payload=object_list_payload, status=200)
        m.get(
            CONSUMPTION_URL_RE,
            payload=_consumption([_entry_at(hour_end, 1.234, status="D")]),
            status=200,
            repeat=True,
        )
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    coordinator = entry.runtime_data.coordinator
    snapshot = coordinator.data[TARGET_OEIC]
    assert snapshot.hourly == {hour_start: 1.234}
    assert snapshot.statuses == {hour_start: "D"}


async def test_first_refresh_can_use_raw_value(
    hass: HomeAssistant, object_list_payload: dict[str, Any]
) -> None:
    entry = _entry(
        options={
            CONF_UPDATE_INTERVAL: DEFAULT_UPDATE_INTERVAL_MIN,
            CONF_BACKFILL_DAYS: DEFAULT_BACKFILL_DAYS,
            CONF_CONSUMPTION_FIELD: CONSUMPTION_FIELD_RAW,
        }
    )
    entry.add_to_hass(hass)

    hour_end = datetime(2026, 5, 5, 4, tzinfo=RIGA_TZ)
    with aioresponses() as m:
        m.get(OBJECT_LIST_URL, payload=object_list_payload, status=200)
        m.get(
            CONSUMPTION_URL_RE,
            payload=_consumption([_entry_at(hour_end, 1.0)]),  # cVR = 1.5
            status=200,
            repeat=True,
        )
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    snapshot = entry.runtime_data.coordinator.data[TARGET_OEIC]
    [(_, value)] = list(snapshot.hourly.items())
    assert value == 1.5  # cVR was 0.5 higher than cVV in the fixture


async def test_entry_without_cvrst_is_kept(
    hass: HomeAssistant, object_list_payload: dict[str, Any]
) -> None:
    """Real API omits `cVRSt` for the all-good case. Entry must be kept,
    not skipped, and not crash. Regression for the
    `KeyError: 'cVRSt'` we hit on a real install."""
    entry = _entry()
    entry.add_to_hass(hass)
    hour_end = datetime(2026, 5, 5, 4, tzinfo=RIGA_TZ)

    raw_entry = {
        "cDt": hour_end.isoformat(),
        "cVR": 1.5,
        "cVV": 1.234,
        # No cVRSt at all — mimics the real API for unflagged readings.
    }
    with aioresponses() as m:
        m.get(OBJECT_LIST_URL, payload=object_list_payload, status=200)
        m.get(
            CONSUMPTION_URL_RE,
            payload=_consumption([raw_entry]),
            status=200,
            repeat=True,
        )
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    snapshot = entry.runtime_data.coordinator.data[TARGET_OEIC]
    [(_, value)] = list(snapshot.hourly.items())
    assert value == 1.234
    # Status stored as empty string (no flag).
    [(_, status)] = list(snapshot.statuses.items())
    assert status == ""


async def test_cdt_with_microseconds_is_snapped_to_hour(
    hass: HomeAssistant, object_list_payload: dict[str, Any]
) -> None:
    """HA's recorder rejects external statistics whose `start` has non-zero
    minute/second. Real Sadales Tīkls cDt occasionally carries microseconds
    or sub-second drift — we must snap to the hour boundary before storing.

    Regression for `Invalid timestamp: timestamps must be from the top of
    the hour` we hit on the second real-install attempt.
    """
    entry = _entry()
    entry.add_to_hass(hass)

    raw_entry = {
        # Microseconds present + an extra second of drift — both would otherwise
        # propagate into snapshot keys and crash async_add_external_statistics.
        "cDt": "2026-05-05T04:00:00.123456+03:00",
        "cVR": 1.5,
        "cVV": 1.234,
    }
    with aioresponses() as m:
        m.get(OBJECT_LIST_URL, payload=object_list_payload, status=200)
        m.get(
            CONSUMPTION_URL_RE,
            payload=_consumption([raw_entry]),
            status=200,
            repeat=True,
        )
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    snapshot = entry.runtime_data.coordinator.data[TARGET_OEIC]
    [(start, _)] = list(snapshot.hourly.items())
    assert start.minute == 0
    assert start.second == 0
    assert start.microsecond == 0
    # And it's the right hour: cDt 04:00 → start 03:00.
    assert start.hour == 3


async def test_entry_without_cdt_is_skipped(
    hass: HomeAssistant, object_list_payload: dict[str, Any]
) -> None:
    """Entries with missing/empty `cDt` are unusable — log + skip, don't
    crash the coordinator."""
    entry = _entry()
    entry.add_to_hass(hass)

    bad_entry = {"cVR": 1.0, "cVV": 1.0}  # no cDt
    with aioresponses() as m:
        m.get(OBJECT_LIST_URL, payload=object_list_payload, status=200)
        m.get(
            CONSUMPTION_URL_RE,
            payload=_consumption([bad_entry]),
            status=200,
            repeat=True,
        )
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    snapshot = entry.runtime_data.coordinator.data[TARGET_OEIC]
    assert snapshot.hourly == {}


async def test_entry_with_only_one_value_field_uses_fallback(
    hass: HomeAssistant, object_list_payload: dict[str, Any]
) -> None:
    """If the configured field (cVV by default) is absent but the other is
    present, fall back rather than skip the hour."""
    entry = _entry()
    entry.add_to_hass(hass)
    hour_end = datetime(2026, 5, 5, 4, tzinfo=RIGA_TZ)

    only_cvr = {
        "cDt": hour_end.isoformat(),
        "cVR": 0.42,
        # No cVV — but we configured cVV as the preferred field.
    }
    with aioresponses() as m:
        m.get(OBJECT_LIST_URL, payload=object_list_payload, status=200)
        m.get(
            CONSUMPTION_URL_RE,
            payload=_consumption([only_cvr]),
            status=200,
            repeat=True,
        )
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    snapshot = entry.runtime_data.coordinator.data[TARGET_OEIC]
    [(_, value)] = list(snapshot.hourly.items())
    assert value == 0.42


@pytest.mark.parametrize("status", ["U", "N"])
async def test_skipped_statuses_are_dropped(
    hass: HomeAssistant, object_list_payload: dict[str, Any], status: str
) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    hour_end = datetime(2026, 5, 5, 4, tzinfo=RIGA_TZ)

    with aioresponses() as m:
        m.get(OBJECT_LIST_URL, payload=object_list_payload, status=200)
        m.get(
            CONSUMPTION_URL_RE,
            payload=_consumption([_entry_at(hour_end, 9.99, status=status)]),
            status=200,
            repeat=True,
        )
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    snapshot = entry.runtime_data.coordinator.data[TARGET_OEIC]
    assert snapshot.hourly == {}
    assert snapshot.statuses == {}


async def test_normal_poll_does_not_drop_existing_data(
    hass: HomeAssistant, object_list_payload: dict[str, Any]
) -> None:
    """A second refresh that returns nothing new should preserve the snapshot
    accumulated by earlier refreshes."""
    entry = _entry()
    entry.add_to_hass(hass)
    hour_end = datetime(2026, 5, 5, 4, tzinfo=RIGA_TZ)

    with aioresponses() as m:
        m.get(OBJECT_LIST_URL, payload=object_list_payload, status=200)
        # First refresh returns one entry, subsequent returns empty.
        m.get(
            CONSUMPTION_URL_RE,
            payload=_consumption([_entry_at(hour_end, 1.0)]),
            status=200,
        )
        m.get(CONSUMPTION_URL_RE, payload=[], status=200, repeat=True)

        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data.coordinator
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    snapshot = coordinator.data[TARGET_OEIC]
    assert len(snapshot.hourly) == 1


async def test_auth_failure_during_refresh_triggers_reauth(
    hass: HomeAssistant, object_list_payload: dict[str, Any]
) -> None:
    """If the APIKEY starts working then later returns 401 mid-refresh, the
    config-entry auth-failure flow kicks in (HA marks setup_error)."""
    entry = _entry()
    entry.add_to_hass(hass)

    with aioresponses() as m:
        m.get(OBJECT_LIST_URL, payload=object_list_payload, status=200)
        m.get(CONSUMPTION_URL_RE, status=401)

        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    # The entry is in setup_error and a reauth flow has been kicked off.
    from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState

    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert any(f["context"].get("source") == SOURCE_REAUTH for f in flows)


# ---------------------------------------------------------------------------
# DST round-trip — make sure the parse + −1h pipeline keeps fall-back's
# duplicated 03:00 distinct.
# ---------------------------------------------------------------------------


def test_dst_fallback_two_distinct_hours() -> None:
    """The two physical 03:00 hours on fall-back day must end up as
    different dict keys in `ObjectSnapshot.hourly`. The wire form
    distinguishes them by offset (+03:00 vs +02:00); we verify the parse +
    −1h pipeline preserves the distinction."""
    # End-of-hour stamps the API would return for the two physical hours
    # whose wall-clock end is "04:00" on 2026-10-25.
    end_first = datetime.fromisoformat("2026-10-25T04:00:00+03:00").astimezone(RIGA_TZ)
    end_second = datetime.fromisoformat("2026-10-25T04:00:00+02:00").astimezone(RIGA_TZ)

    start_first = end_first - timedelta(hours=1)
    start_second = end_second - timedelta(hours=1)

    # Different physical moments → different aware datetimes → different
    # dict keys.
    assert start_first != start_second
    assert hash(start_first) != hash(start_second)
    bag: dict[datetime, str] = {}
    bag[start_first] = "first"
    bag[start_second] = "second"
    assert len(bag) == 2


def test_object_snapshot_in_memory_only() -> None:
    snap = ObjectSnapshot(
        info={
            "oEIC": "X",
            "oName": "Office",
            "oAddr": "Street 1",
            "oStatus": "A",
            "mpList": [],
        }
    )
    assert snap.hourly == {}
    assert snap.statuses == {}
