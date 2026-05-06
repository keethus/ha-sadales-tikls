"""External-statistics ingestion for Sadales Tīkls.

For each object we maintain a long-term-statistics stream
`sadales_tikls:consumption_<oeic_lower>` with `has_sum=True`. Hourly values
are written at start-of-hour with a running cumulative sum.

Two flavors of write
--------------------
* `full_recompute=True` (first refresh, daily catch-up): we recompute
  sums for every hour in the window starting from "the sum just before
  the window's earliest hour". Existing rows in the window are
  overwritten — this is exactly how retroactive corrections (`cVRSt = D /
  M`) are absorbed.
* `full_recompute=False` (normal hourly poll): we only append hours
  that are newer than the last existing stat. Idempotent on retries.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.components.recorder import get_instance  # type: ignore[attr-defined]
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)

from .const import STATISTICS_ID_PREFIX, STATISTICS_SOURCE, STATISTICS_UNIT

if TYPE_CHECKING:
    from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
    from homeassistant.core import HomeAssistant

    from .coordinator import ObjectSnapshot

_LOGGER = logging.getLogger(__name__)

# How far back to search for the "running sum just before our window".
_SUM_LOOKBACK = timedelta(days=400)


# Real EICs contain hyphens (e.g. "30X-AAA-BBBB-..."), but HA's recorder
# validates statistic-id object_ids against `[a-z0-9_]+` only — hyphens are
# rejected with `Invalid statistic_id`. We sanitize the EIC to satisfy:
# no leading/trailing/double underscores, only [a-z0-9_].
_NON_SAFE = re.compile(r"[^a-z0-9_]")
_RUNS = re.compile(r"_+")


def statistic_id_for(o_eic: str) -> str:
    """Statistic id used for the per-object consumption stream."""
    safe = _NON_SAFE.sub("_", o_eic.lower())
    safe = _RUNS.sub("_", safe).strip("_")
    return f"{STATISTICS_ID_PREFIX}{safe}"


async def async_write_object_statistics(
    hass: HomeAssistant,
    snapshot: ObjectSnapshot,
    *,
    window_from: datetime,
    window_to: datetime,
    full_recompute: bool,
) -> None:
    """Write external statistics for an object's window."""
    if not _recorder_loaded(hass):
        # Recorder is a core component but can be disabled. The integration
        # still functions (sensors work) — we just can't feed the Energy
        # Dashboard. Tests without the recorder fixture also reach this path.
        _LOGGER.debug(
            "Recorder not loaded; skipping external-statistics write for %s",
            snapshot.info["oEIC"],
        )
        return

    statistic_id = statistic_id_for(snapshot.info["oEIC"])

    hours_in_window: list[tuple[datetime, float]] = sorted(
        (h, v) for h, v in snapshot.hourly.items() if window_from <= h < window_to
    )
    if not hours_in_window:
        return

    if full_recompute:
        # Recompute sums for the whole window.
        candidates = hours_in_window
        base_sum = await _get_sum_just_before(hass, statistic_id, hours_in_window[0][0])
    else:
        # Append only hours newer than the last existing stat.
        last = await _get_last_stat(hass, statistic_id)
        if last is None:
            candidates = hours_in_window
            base_sum = 0.0
        else:
            last_start, last_sum = last
            candidates = [(h, v) for h, v in hours_in_window if h > last_start]
            if not candidates:
                return
            base_sum = last_sum

    statistics: list[StatisticData] = []
    running = base_sum
    for hour, value in candidates:
        running += value
        statistics.append(
            {
                "start": hour,
                "state": value,
                "sum": running,
            }
        )

    metadata: StatisticMetaData = {
        "has_mean": False,
        "mean_type": StatisticMeanType.NONE,
        "has_sum": True,
        "name": f"Sadales Tīkls — {snapshot.info['oName']}",
        "source": STATISTICS_SOURCE,
        "statistic_id": statistic_id,
        "unit_class": "energy",
        "unit_of_measurement": STATISTICS_UNIT,
    }
    async_add_external_statistics(hass, metadata, statistics)


# ---------------------------------------------------------------------------
# Recorder helpers
# ---------------------------------------------------------------------------


async def _get_last_stat(hass: HomeAssistant, statistic_id: str) -> tuple[datetime, float] | None:
    """Return (start, sum) of the most recent stat for `statistic_id`."""
    rows = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, statistic_id, True, {"sum"}
    )
    bucket = rows.get(statistic_id, [])
    if not bucket:
        return None
    row = bucket[0]
    return _row_start(row), float(row.get("sum") or 0.0)


async def _get_sum_just_before(hass: HomeAssistant, statistic_id: str, when: datetime) -> float:
    """Running sum at the latest stat with `start < when`. 0 if none."""
    period_start = when - _SUM_LOOKBACK
    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        period_start,
        when,  # half-open: returns rows where start < when
        {statistic_id},
        "hour",
        None,
        {"sum"},
    )
    bucket = rows.get(statistic_id, [])
    if not bucket:
        return 0.0
    bucket.sort(key=lambda r: _row_start(r))
    return float(bucket[-1].get("sum") or 0.0)


def _recorder_loaded(hass: HomeAssistant) -> bool:
    """True iff the recorder is initialized for this HA instance."""
    from homeassistant.helpers.recorder import DATA_INSTANCE  # noqa: PLC0415

    return DATA_INSTANCE in hass.data


def _row_start(row: Any) -> datetime:
    """Normalize the `start` field — recorder may return datetime or
    Unix-epoch float depending on the version."""
    raw = row.get("start") if hasattr(row, "get") else row["start"]
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, (int, float)):
        from datetime import UTC  # noqa: PLC0415

        return datetime.fromtimestamp(float(raw), tz=UTC)
    msg = f"Unexpected statistics row 'start' type: {type(raw).__name__}"
    raise TypeError(msg)
