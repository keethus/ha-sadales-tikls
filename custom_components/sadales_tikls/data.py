"""Runtime data attached to a Sadales Tīkls config entry.

Uses the modern HA `ConfigEntry[T]` + `entry.runtime_data` pattern. Runtime
data is *not* persisted; only `entry.data` and `entry.options` are.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

    from .api import SadalesTiklsAPI
    from .coordinator import SadalesTiklsCoordinator


@dataclass(slots=True)
class SadalesTiklsRuntimeData:
    """Per-entry runtime objects, attached to `entry.runtime_data`."""

    api: SadalesTiklsAPI
    coordinator: SadalesTiklsCoordinator


if TYPE_CHECKING:
    type SadalesTiklsConfigEntry = ConfigEntry[SadalesTiklsRuntimeData]
