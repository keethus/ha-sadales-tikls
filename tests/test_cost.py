"""Tests for `cost.py` — the money stream that feeds `stat_cost`.

The pure parts are the statistic-id shape and the per-hour arithmetic
(spot + extra, then VAT). The recorder-backed write path is covered via
monkeypatched recorder helpers so these stay fast and hermetic: what we
assert is the *sequence* handed to `async_add_external_statistics`, since
a wrong running `sum` is the failure mode that silently corrupts the
Energy Dashboard.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from custom_components.sadales_tikls import cost as cost_mod
from custom_components.sadales_tikls.cost import cost_statistic_id_for

RIGA = ZoneInfo("Europe/Riga")

# HA's actual regex (homeassistant/components/recorder/util.py).
HA_VALID_STATISTIC_ID = re.compile(r"^(?!.+__)(?!_)[\da-z_]+(?<!_):(?!_)[\da-z_]+(?<!_)$")


@pytest.mark.parametrize(
    "o_eic",
    [
        "12X-OBJ-OFFICE-RIGA0",
        "30X-AAA-BBBB-1234CDEF",
        "----STARTS-AND-ENDS----",
        "Has__Double",
    ],
)
def test_cost_statistic_id_passes_ha_validation(o_eic: str) -> None:
    sid = cost_statistic_id_for(o_eic)
    assert HA_VALID_STATISTIC_ID.match(sid), f"{sid!r} does not satisfy HA's statistic-id regex"


def test_cost_statistic_id_known_shape() -> None:
    assert cost_statistic_id_for("12X-OBJ-OFFICE-RIGA0") == (
        "sadales_tikls:cost_12x_obj_office_riga0"
    )


def test_cost_and_consumption_ids_are_distinct() -> None:
    from custom_components.sadales_tikls.statistics import statistic_id_for

    eic = "12X-OBJ-OFFICE-RIGA0"
    assert cost_statistic_id_for(eic) != statistic_id_for(eic)


class _Snapshot:
    """Minimal stand-in for ObjectSnapshot (cost.py only reads these)."""

    def __init__(self, hourly: dict[datetime, float]) -> None:
        self.info = {"oEIC": "12X-OBJ-OFFICE-RIGA0", "oName": "Office"}
        self.hourly = hourly


@pytest.fixture
def three_hours() -> tuple[datetime, _Snapshot]:
    h0 = datetime(2026, 7, 1, 10, tzinfo=RIGA)
    return h0, _Snapshot({h0: 2.0, h0 + timedelta(hours=1): 3.0, h0 + timedelta(hours=2): 5.0})


async def _run(
    monkeypatch: pytest.MonkeyPatch,
    snapshot: _Snapshot,
    h0: datetime,
    *,
    prices: dict[datetime, float],
    base_sum: float = 0.0,
    last: tuple[datetime, float] | None = None,
    full_recompute: bool = True,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Drive the writer with the recorder stubbed out; return the rows written."""
    written: list[dict[str, Any]] = []

    monkeypatch.setattr(cost_mod, "_recorder_loaded", lambda _hass: True)
    monkeypatch.setattr(cost_mod, "_get_sum_just_before", _async(base_sum))
    monkeypatch.setattr(cost_mod, "_get_last_stat", _async(last))
    monkeypatch.setattr(cost_mod, "_hourly_prices", _async(prices))
    monkeypatch.setattr(
        cost_mod,
        "async_add_external_statistics",
        lambda _hass, _meta, stats: written.extend(stats),
    )

    await cost_mod.async_write_object_cost_statistics(
        None,  # type: ignore[arg-type]
        snapshot,  # type: ignore[arg-type]
        window_from=h0,
        window_to=h0 + timedelta(hours=24),
        full_recompute=full_recompute,
        price_statistic_id="sensor.spot_price",
        **kwargs,
    )
    return written


def _async(value: Any):
    async def _inner(*_args: Any, **_kwargs: Any) -> Any:
        return value

    return _inner


async def test_cost_is_kwh_times_price(
    monkeypatch: pytest.MonkeyPatch, three_hours: tuple[datetime, _Snapshot]
) -> None:
    h0, snap = three_hours
    prices = {h: 0.10 for h in snap.hourly}

    rows = await _run(monkeypatch, snap, h0, prices=prices)

    assert [r["state"] for r in rows] == pytest.approx([0.2, 0.3, 0.5])
    # The running sum is what the Energy Dashboard actually reads.
    assert [r["sum"] for r in rows] == pytest.approx([0.2, 0.5, 1.0])


async def test_extra_and_vat_are_applied(
    monkeypatch: pytest.MonkeyPatch, three_hours: tuple[datetime, _Snapshot]
) -> None:
    h0, snap = three_hours
    prices = {h: 0.10 for h in snap.hourly}

    rows = await _run(
        monkeypatch, snap, h0, prices=prices, extra_eur_kwh=0.05, vat_pct=21.0
    )

    # 2 kWh * (0.10 + 0.05) * 1.21
    assert rows[0]["state"] == pytest.approx(2.0 * 0.15 * 1.21)


async def test_hour_without_price_is_skipped_not_guessed(
    monkeypatch: pytest.MonkeyPatch, three_hours: tuple[datetime, _Snapshot]
) -> None:
    h0, snap = three_hours
    prices = {h0: 0.10, h0 + timedelta(hours=2): 0.10}  # middle hour missing

    rows = await _run(monkeypatch, snap, h0, prices=prices)

    assert [r["start"] for r in rows] == [h0, h0 + timedelta(hours=2)]
    # 2*0.10 then +5*0.10 — the unpriced hour contributes nothing at all.
    assert [r["sum"] for r in rows] == pytest.approx([0.2, 0.7])


async def test_append_mode_continues_from_last_sum(
    monkeypatch: pytest.MonkeyPatch, three_hours: tuple[datetime, _Snapshot]
) -> None:
    h0, snap = three_hours
    prices = {h: 0.10 for h in snap.hourly}

    rows = await _run(
        monkeypatch,
        snap,
        h0,
        prices=prices,
        full_recompute=False,
        last=(h0, 100.0),  # first hour already written
    )

    assert [r["start"] for r in rows] == [h0 + timedelta(hours=1), h0 + timedelta(hours=2)]
    assert [r["sum"] for r in rows] == pytest.approx([100.3, 100.8])


async def test_no_prices_at_all_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, three_hours: tuple[datetime, _Snapshot]
) -> None:
    h0, snap = three_hours

    rows = await _run(monkeypatch, snap, h0, prices={})

    assert rows == []
