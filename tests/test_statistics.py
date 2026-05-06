"""Tests for `statistics.py` helpers that don't require a live recorder.

The statistic_id sanitizer is the most failure-prone bit — HA's recorder
rejects anything that doesn't match `[a-z0-9_]` after the colon, including
hyphens which real EICs contain.
"""

from __future__ import annotations

import re

import pytest

from custom_components.sadales_tikls.statistics import statistic_id_for

# HA's actual regex (homeassistant/components/recorder/util.py).
HA_VALID_STATISTIC_ID = re.compile(r"^(?!.+__)(?!_)[\da-z_]+(?<!_):(?!_)[\da-z_]+(?<!_)$")


@pytest.mark.parametrize(
    "o_eic",
    [
        "12X-OBJ-OFFICE-RIGA0",  # our test fixture shape
        "30X-AAA-BBBB-1234CDEF",  # real EIC shape
        "12x-already-lower",
        "ABC123",  # hyphen-free
        "----STARTS-AND-ENDS----",  # would otherwise produce leading/trailing _
        "Has__Double",  # would otherwise produce __ in the middle
        "MIX-of_underscores-and-hyphens",
    ],
)
def test_statistic_id_passes_ha_validation(o_eic: str) -> None:
    sid = statistic_id_for(o_eic)
    assert HA_VALID_STATISTIC_ID.match(sid), f"{sid!r} does not satisfy HA's statistic-id regex"


def test_statistic_id_known_shape() -> None:
    assert (
        statistic_id_for("12X-OBJ-OFFICE-RIGA0") == "sadales_tikls:consumption_12x_obj_office_riga0"
    )
