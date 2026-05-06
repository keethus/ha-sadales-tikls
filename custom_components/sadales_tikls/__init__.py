"""Sadales Tīkls (Latvia) custom integration for Home Assistant.

Lifecycle
---------
On entry setup we:
  1. Validate the APIKEY against /get-object-list (auth failure → reauth).
  2. Build the per-object metadata map for sensors.
  3. Create the DataUpdateCoordinator and run its first refresh.
  4. Forward the entry to the sensor platform.
  5. Listen for option changes and reload on edit.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.const import CONF_API_KEY, Platform
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    SadalesTiklsAPI,
    SadalesTiklsAuthError,
    SadalesTiklsConnectionError,
    SadalesTiklsError,
)
from .coordinator import SadalesTiklsCoordinator
from .data import SadalesTiklsRuntimeData

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .data import SadalesTiklsConfigEntry

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: SadalesTiklsConfigEntry) -> bool:
    """Set up Sadales Tīkls from a config entry."""
    session = async_get_clientsession(hass)
    api = SadalesTiklsAPI(session, entry.data[CONF_API_KEY])

    try:
        objects_response = await api.get_object_list()
    except SadalesTiklsAuthError as err:
        raise ConfigEntryAuthFailed("Sadales Tīkls APIKEY rejected") from err
    except SadalesTiklsConnectionError as err:
        raise ConfigEntryNotReady(f"Cannot reach Sadales Tīkls API: {err}") from err
    except SadalesTiklsError as err:
        raise ConfigEntryNotReady(f"Sadales Tīkls API error: {err}") from err

    objects_meta = {obj["oEIC"]: obj for obj in objects_response["oList"]}

    coordinator = SadalesTiklsCoordinator(hass, entry, api, objects_meta)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = SadalesTiklsRuntimeData(api=api, coordinator=coordinator)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: SadalesTiklsConfigEntry) -> bool:
    """Tear down a config entry and its sensor platform."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(hass: HomeAssistant, entry: SadalesTiklsConfigEntry) -> None:
    """Reload the entry when options change so the coordinator picks up
    the new interval / backfill / consumption-field settings."""
    await hass.config_entries.async_reload(entry.entry_id)
