"""Tests for the per-object sensor value + attribute functions.

These are pure functions over an `ObjectSnapshot` and `now`, so they're
checked directly without spinning up HA.
"""

from __future__ import annotations

from datetime import datetime

from custom_components.sadales_tikls.coordinator import RIGA_TZ, ObjectSnapshot
from custom_components.sadales_tikls.sensor import (
    _data_lag_hours,
    _month_to_date,
    _most_recent_hour_status,
    _most_recent_hour_value,
    _mtd_attrs,
    _previous_month,
    _previous_month_attrs,
    _yesterday_attrs,
    _yesterday_consumption,
)


def _hour(year: int, month: int, day: int, hour: int) -> datetime:
    return datetime(year, month, day, hour, tzinfo=RIGA_TZ)


def _empty_snapshot() -> ObjectSnapshot:
    return ObjectSnapshot(
        info={
            "oEIC": "X",
            "oName": "X",
            "oAddr": "",
            "oStatus": "A",
            "mpList": [],
        }
    )


# Anchor "now" at 03:30 today so latest hour is 02:00–03:00.
NOW = datetime(2026, 5, 6, 3, 30, tzinfo=RIGA_TZ)


# ---------------------------------------------------------------------------
# Fixture snapshot covering parts of two months and two days
# ---------------------------------------------------------------------------


def _populated_snapshot() -> ObjectSnapshot:
    snap = _empty_snapshot()
    # Today (2026-05-06): hours 00, 01, 02 → 1 + 2 + 3 = 6 kWh
    snap.hourly[_hour(2026, 5, 6, 0)] = 1.0
    snap.hourly[_hour(2026, 5, 6, 1)] = 2.0
    snap.hourly[_hour(2026, 5, 6, 2)] = 3.0
    # Yesterday (2026-05-05): hours 22, 23 → 4 + 5 = 9 kWh
    snap.hourly[_hour(2026, 5, 5, 22)] = 4.0
    snap.hourly[_hour(2026, 5, 5, 23)] = 5.0
    # Earlier in May → 6 + 7 = 13 kWh
    snap.hourly[_hour(2026, 5, 1, 0)] = 6.0
    snap.hourly[_hour(2026, 5, 1, 1)] = 7.0
    # April (previous month) → 8 + 9 = 17 kWh
    snap.hourly[_hour(2026, 4, 30, 23)] = 8.0
    snap.hourly[_hour(2026, 4, 30, 22)] = 9.0
    # Statuses: only the latest matters
    snap.statuses[_hour(2026, 5, 6, 2)] = "C"
    snap.statuses[_hour(2026, 5, 5, 23)] = "D"
    return snap


# ---------------------------------------------------------------------------
# State values
# ---------------------------------------------------------------------------


def test_yesterday_consumption() -> None:
    assert _yesterday_consumption(_populated_snapshot(), NOW) == 9.0


def test_month_to_date() -> None:
    # 1 + 2 + 3 (today) + 4 + 5 (yesterday) + 6 + 7 (May 1) = 28
    assert _month_to_date(_populated_snapshot(), NOW) == 28.0


def test_previous_month() -> None:
    assert _previous_month(_populated_snapshot(), NOW) == 17.0


def test_previous_month_january_wraps_to_december() -> None:
    snap = _empty_snapshot()
    snap.hourly[_hour(2025, 12, 31, 23)] = 11.0
    snap.hourly[_hour(2025, 12, 30, 12)] = 22.0
    snap.hourly[_hour(2026, 1, 1, 0)] = 99.0  # current Jan, not prev Dec
    now_jan = _hour(2026, 1, 15, 12)
    assert _previous_month(snap, now_jan) == 33.0


def test_most_recent_hour_value() -> None:
    assert _most_recent_hour_value(_populated_snapshot(), NOW) == 3.0


def test_most_recent_hour_value_with_empty_snapshot() -> None:
    assert _most_recent_hour_value(_empty_snapshot(), NOW) is None


def test_data_lag_hours() -> None:
    """Latest hour key is 02:00 (so end-of-hour 03:00). NOW is 03:30 → 0.5h."""
    assert _data_lag_hours(_populated_snapshot(), NOW) == 0.5


def test_data_lag_with_no_data() -> None:
    assert _data_lag_hours(_empty_snapshot(), NOW) is None


def test_most_recent_hour_status() -> None:
    assert _most_recent_hour_status(_populated_snapshot(), NOW) == "C"


def test_yesterday_at_month_boundary() -> None:
    snap = _empty_snapshot()
    snap.hourly[_hour(2026, 4, 30, 12)] = 1.5
    snap.hourly[_hour(2026, 4, 30, 13)] = 2.5
    snap.hourly[_hour(2026, 5, 1, 0)] = 9.0
    now_may1 = _hour(2026, 5, 1, 12)
    assert _yesterday_consumption(snap, now_may1) == 4.0


# ---------------------------------------------------------------------------
# Attribute payloads (chart-friendly breakdowns)
# ---------------------------------------------------------------------------


def test_yesterday_attrs_returns_hourly_list_in_order() -> None:
    attrs = _yesterday_attrs(_populated_snapshot(), NOW)
    # Yesterday is 2026-05-05; we have 22 and 23.
    hourly = attrs["hourly"]
    assert len(hourly) == 2
    assert hourly[0]["start"] == _hour(2026, 5, 5, 22).isoformat()
    assert hourly[0]["value"] == 4.0
    assert hourly[1]["start"] == _hour(2026, 5, 5, 23).isoformat()
    assert hourly[1]["value"] == 5.0
    assert hourly[1]["status"] == "D"


def test_mtd_attrs_aggregates_by_day() -> None:
    attrs = _mtd_attrs(_populated_snapshot(), NOW)
    daily = attrs["daily"]
    by_date = {d["date"]: d["value"] for d in daily}
    # 2026-05-01 → 6 + 7 = 13
    # 2026-05-05 → 4 + 5 = 9
    # 2026-05-06 → 1 + 2 + 3 = 6
    assert by_date == {"2026-05-01": 13.0, "2026-05-05": 9.0, "2026-05-06": 6.0}
    # Output is sorted by date.
    assert [d["date"] for d in daily] == sorted(by_date)


def test_previous_month_attrs_aggregates_april() -> None:
    attrs = _previous_month_attrs(_populated_snapshot(), NOW)
    daily = attrs["daily"]
    # All April entries on the 30th: 8 + 9 = 17.
    assert len(daily) == 1
    assert daily[0] == {"date": "2026-04-30", "value": 17.0}


def test_previous_month_attrs_january_wraps_to_december() -> None:
    snap = _empty_snapshot()
    snap.hourly[_hour(2025, 12, 30, 12)] = 1.0
    snap.hourly[_hour(2025, 12, 31, 23)] = 2.0
    snap.hourly[_hour(2026, 1, 1, 0)] = 99.0  # not prev December
    now_jan = _hour(2026, 1, 15, 12)
    attrs = _previous_month_attrs(snap, now_jan)
    by_date = {d["date"]: d["value"] for d in attrs["daily"]}
    assert by_date == {"2025-12-30": 1.0, "2025-12-31": 2.0}


def test_yesterday_attrs_empty_for_day_with_no_data() -> None:
    snap = _empty_snapshot()
    attrs = _yesterday_attrs(snap, NOW)
    assert attrs == {"hourly": []}
