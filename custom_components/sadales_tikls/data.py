"""Runtime data attached to a Sadales Tīkls config entry.

Uses the modern Home Assistant `ConfigEntry[T]` + `entry.runtime_data` pattern
(2024.11+). Runtime data is *not* persisted; only `entry.data` and
`entry.options` are. Anything in here is recreated on every entry load.

Step 2 (this step) only stores the API client. The coordinator is wired in
step 3 and added to this dataclass at that time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

    from .api import SadalesTiklsAPI


@dataclass(slots=True)
class SadalesTiklsRuntimeData:
    """Per-entry runtime objects, attached to `entry.runtime_data`."""

    api: SadalesTiklsAPI
    # coordinator: SadalesTiklsCoordinator   # populated in step 3


if TYPE_CHECKING:
    type SadalesTiklsConfigEntry = ConfigEntry[SadalesTiklsRuntimeData]
