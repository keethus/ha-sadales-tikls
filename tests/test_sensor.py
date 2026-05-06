"""Tests for the per-object sensor value functions.

These are pure functions over an `ObjectSnapshot` and `now`, so they're
checked directly without spinning up HA.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from custom_components.sadales_tikls.coordinator import RIGA_TZ, ObjectSnapshot
from custom_components.sadales_tikls.sensor import (
    _data_lag_hours,
    _last_hour_status,
    _last_hour_value,
    _month_to_date,
    _previous_month,
    _today_consumption,
    _yesterday_consumption,
)


def _hour(year: int, month: int, day: int, hour: int) -> datetime:
    return datetime(year, month, day, hour, tzinfo=RIGA_TZ)


@pytest.fixture
def snapshot() -> ObjectSnapshot:
    """A snapshot covering parts of two months and two days."""
    snap = ObjectSnapshot(
        info={
            "oEIC": "X",
            "oName": "Office",
            "oAddr": "Street 1",
            "oStatus": "A",
            "mpList": [],
        }
    )
    # Today = 2026-05-06 in tests below.
    # Today, hours 00, 01, 02 → 1 + 2 + 3 = 6 kWh
    snap.hourly[_hour(2026, 5, 6, 0)] = 1.0
    snap.hourly[_hour(2026, 5, 6, 1)] = 2.0
    snap.hourly[_hour(2026, 5, 6, 2)] = 3.0
    # Yesterday, hours 22, 23 → 4 + 5 = 9 kWh
    snap.hourly[_hour(2026, 5, 5, 22)] = 4.0
    snap.hourly[_hour(2026, 5, 5, 23)] = 5.0
    # 2 hours earlier this month → 6 + 7 = 13 kWh
    snap.hourly[_hour(2026, 5, 1, 0)] = 6.0
    snap.hourly[_hour(2026, 5, 1, 1)] = 7.0
    # April (previous month) → 8 + 9 = 17 kWh
    snap.hourly[_hour(2026, 4, 30, 23)] = 8.0
    snap.hourly[_hour(2026, 4, 30, 22)] = 9.0
    # Statuses: only the latest matters for the sensor
    snap.statuses[_hour(2026, 5, 6, 2)] = "C"
    snap.statuses[_hour(2026, 5, 5, 23)] = "D"
    return snap


# Anchor "now" at 03:30 today so the latest hour is 02:00–03:00.
NOW = datetime(2026, 5, 6, 3, 30, tzinfo=RIGA_TZ)


def test_last_hour_value(snapshot: ObjectSnapshot) -> None:
    assert _last_hour_value(snapshot, NOW) == 3.0


def test_today_consumption(snapshot: ObjectSnapshot) -> None:
    assert _today_consumption(snapshot, NOW) == 6.0


def test_yesterday_consumption(snapshot: ObjectSnapshot) -> None:
    assert _yesterday_consumption(snapshot, NOW) == 9.0


def test_month_to_date(snapshot: ObjectSnapshot) -> None:
    # 1 + 2 + 3 (today) + 4 + 5 (yesterday) + 6 + 7 (May 1) = 28
    assert _month_to_date(snapshot, NOW) == 28.0


def test_previous_month(snapshot: ObjectSnapshot) -> None:
    assert _previous_month(snapshot, NOW) == 17.0


def test_previous_month_january_wraps_to_december() -> None:
    snap = ObjectSnapshot(
        info={
            "oEIC": "X",
            "oName": "X",
            "oAddr": "",
            "oStatus": "A",
            "mpList": [],
        }
    )
    snap.hourly[_hour(2025, 12, 31, 23)] = 11.0
    snap.hourly[_hour(2025, 12, 30, 12)] = 22.0
    snap.hourly[_hour(2026, 1, 1, 0)] = 99.0  # in current Jan, not prev Dec
    now_jan = _hour(2026, 1, 15, 12)
    assert _previous_month(snap, now_jan) == 33.0


def test_data_lag_hours(snapshot: ObjectSnapshot) -> None:
    """Latest snapshot key is 02:00 (so end-of-hour 03:00). NOW is 03:30 →
    lag = 0.5h."""
    assert _data_lag_hours(snapshot, NOW) == 0.5


def test_data_lag_with_no_data() -> None:
    snap = ObjectSnapshot(
        info={
            "oEIC": "X",
            "oName": "X",
            "oAddr": "",
            "oStatus": "A",
            "mpList": [],
        }
    )
    assert _data_lag_hours(snap, NOW) is None


def test_last_hour_status(snapshot: ObjectSnapshot) -> None:
    assert _last_hour_status(snapshot, NOW) == "C"


def test_last_hour_value_with_empty_snapshot_returns_none() -> None:
    snap = ObjectSnapshot(
        info={
            "oEIC": "X",
            "oName": "X",
            "oAddr": "",
            "oStatus": "A",
            "mpList": [],
        }
    )
    assert _last_hour_value(snap, NOW) is None


def test_month_to_date_with_only_previous_month_data() -> None:
    """Edge: a snapshot with only April data should yield 0 for May MTD."""
    snap = ObjectSnapshot(
        info={
            "oEIC": "X",
            "oName": "X",
            "oAddr": "",
            "oStatus": "A",
            "mpList": [],
        }
    )
    snap.hourly[_hour(2026, 4, 1, 0)] = 5.0
    assert _month_to_date(snap, NOW) == 0
    assert _previous_month(snap, NOW) == 5.0


def test_yesterday_at_month_boundary() -> None:
    """Today=May 1, yesterday should still grab April 30 hours."""
    snap = ObjectSnapshot(
        info={
            "oEIC": "X",
            "oName": "X",
            "oAddr": "",
            "oStatus": "A",
            "mpList": [],
        }
    )
    snap.hourly[_hour(2026, 4, 30, 12)] = 1.5
    snap.hourly[_hour(2026, 4, 30, 13)] = 2.5
    snap.hourly[_hour(2026, 5, 1, 0)] = 9.0
    now_may1 = _hour(2026, 5, 1, 12)
    assert _yesterday_consumption(snap, now_may1) == 4.0


def test_today_with_hours_at_midnight_boundary() -> None:
    """Hour 23:00–24:00 of yesterday is `cDt 00:00 today` end-of-hour →
    start `23:00 yesterday`. Make sure today_consumption doesn't pick it up.
    """
    snap = ObjectSnapshot(
        info={
            "oEIC": "X",
            "oName": "X",
            "oAddr": "",
            "oStatus": "A",
            "mpList": [],
        }
    )
    snap.hourly[_hour(2026, 5, 5, 23)] = 9.0  # yesterday 23–24
    snap.hourly[_hour(2026, 5, 6, 0)] = 1.0  # today 00–01
    now_today = _hour(2026, 5, 6, 12)
    assert _today_consumption(snap, now_today) == 1.0
