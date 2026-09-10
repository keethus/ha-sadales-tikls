"""Cost external-statistics ingestion for Sadales Tīkls.

Alongside the per-object consumption stream
(`sadales_tikls:consumption_<eic>`) we can maintain a matching money
stream `sadales_tikls:cost_<eic>` with `has_sum=True`, which the HA
Energy Dashboard accepts as a grid source's `stat_cost`.

Why this lives here and not in the Energy Dashboard UI
------------------------------------------------------
HA can compute cost itself only for grid sources backed by a *real*
sensor: it multiplies the sensor's state changes by the price at that
moment, which needs recorder *states*. External statistics have no
states — only finished hourly buckets — so HA refuses
`entity_energy_price` for them ("Entity or number price is not supported
for external statistics. Use stat_cost instead") and expects a
pre-computed cost statistic. This module is that computation.

Price source
------------
The hourly price comes from the recorder's own long-term statistics for
a user-chosen price entity (e.g. a Nordpool spot-price sensor). That is
deliberate: statistics go back as far as the recorder keeps them, so
enabling this feature **backfills every hour we already have
consumption for** instead of only starting from "now".

Per hour:

    cost = kWh * (spot + extra_eur_kwh) * (1 + vat_pct / 100)

`extra_eur_kwh` is where the distribution tariff and mandatory
procurement component go — the spot price alone is not what lands on
the invoice.

Hours with consumption but no price statistic are skipped rather than
guessed, so a partially-priced window produces a partially-priced cost
stream instead of a silently wrong one.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.components.recorder import get_instance  # type: ignore[attr-defined]
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    statistics_during_period,
)

from .const import STATISTICS_COST_ID_PREFIX, STATISTICS_SOURCE
from .statistics import _get_last_stat, _get_sum_just_before, _recorder_loaded, _row_start, _safe_eic

if TYPE_CHECKING:
    from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
    from homeassistant.core import HomeAssistant

    from .coordinator import ObjectSnapshot

_LOGGER = logging.getLogger(__name__)


def cost_statistic_id_for(o_eic: str) -> str:
    """Statistic id used for the per-object cost stream."""
    return f"{STATISTICS_COST_ID_PREFIX}{_safe_eic(o_eic)}"


async def async_write_object_cost_statistics(
    hass: HomeAssistant,
    snapshot: ObjectSnapshot,
    *,
    window_from: datetime,
    window_to: datetime,
    full_recompute: bool,
    price_statistic_id: str,
    extra_eur_kwh: float = 0.0,
    vat_pct: float = 0.0,
    currency: str = "EUR",
) -> None:
    """Write the cost external-statistics stream for an object's window.

    Mirrors `async_write_object_statistics`: `full_recompute` rewrites the
    whole window (absorbing retroactive kWh corrections and late-arriving
    prices), otherwise we append only hours newer than the last cost row.
    """
    if not _recorder_loaded(hass):
        _LOGGER.debug(
            "Recorder not loaded; skipping cost-statistics write for %s",
            snapshot.info["oEIC"],
        )
        return

    statistic_id = cost_statistic_id_for(snapshot.info["oEIC"])

    hours_in_window: list[tuple[datetime, float]] = sorted(
        (h, v) for h, v in snapshot.hourly.items() if window_from <= h < window_to
    )
    if not hours_in_window:
        return

    if full_recompute:
        candidates = hours_in_window
        base_sum = await _get_sum_just_before(hass, statistic_id, hours_in_window[0][0])
    else:
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

    prices = await _hourly_prices(
        hass,
        price_statistic_id,
        candidates[0][0],
        candidates[-1][0] + timedelta(hours=1),
    )
    if not prices:
        _LOGGER.warning(
            "No price statistics found for %s in %s..%s; cost stream for %s not updated. "
            "Does that entity have long-term statistics?",
            price_statistic_id,
            candidates[0][0].isoformat(),
            candidates[-1][0].isoformat(),
            snapshot.info["oEIC"],
        )
        return

    vat_factor = 1.0 + (vat_pct / 100.0)

    statistics: list[StatisticData] = []
    running = base_sum
    missing = 0
    for hour, kwh in candidates:
        spot = prices.get(hour)
        if spot is None:
            # Never guess a price: an unpriced hour would silently understate
            # or overstate the running total for every hour after it.
            missing += 1
            continue
        value = kwh * (spot + extra_eur_kwh) * vat_factor
        running += value
        statistics.append({"start": hour, "state": value, "sum": running})

    if missing:
        _LOGGER.debug(
            "Cost stream for %s: skipped %d of %d hour(s) with no price in %s",
            snapshot.info["oEIC"],
            missing,
            len(candidates),
            price_statistic_id,
        )

    if not statistics:
        return

    metadata: StatisticMetaData = {
        "has_mean": False,
        "mean_type": StatisticMeanType.NONE,
        "has_sum": True,
        "name": f"Sadales Tīkls — {snapshot.info['oName']} (izmaksas)",
        "source": STATISTICS_SOURCE,
        "statistic_id": statistic_id,
        "unit_class": None,
        "unit_of_measurement": currency,
    }
    async_add_external_statistics(hass, metadata, statistics)


async def _hourly_prices(
    hass: HomeAssistant,
    price_statistic_id: str,
    start: datetime,
    end: datetime,
) -> dict[datetime, float]:
    """Start-of-hour → price for `price_statistic_id` over [start, end).

    Prefers the hourly `mean`. Price sensors are commonly recorded with
    `state_class: total` rather than `measurement`, in which case the
    recorder stores no mean and we fall back to `state` — the last value
    seen inside the hour, which for an hourly tariff is that hour's price.
    """
    rows: dict[str, list[Any]] = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        start,
        end,
        {price_statistic_id},
        "hour",
        None,
        {"mean", "state"},
    )
    out: dict[datetime, float] = {}
    for row in rows.get(price_statistic_id, []):
        raw = row.get("mean")
        if raw is None:
            raw = row.get("state")
        if raw is None:
            continue
        out[_row_start(row)] = float(raw)
    return out
