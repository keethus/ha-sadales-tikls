"""Sadales Tīkls (Latvia) custom integration for Home Assistant.

Step 2 wires up the config-entry lifecycle. The integration validates the
APIKEY against the Sadales Tīkls API on every entry load and stores the
shared client on `entry.runtime_data`. Coordinator + sensor platforms land
in subsequent build steps.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.const import CONF_API_KEY
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    SadalesTiklsAPI,
    SadalesTiklsAuthError,
    SadalesTiklsConnectionError,
    SadalesTiklsError,
)
from .data import SadalesTiklsRuntimeData

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .data import SadalesTiklsConfigEntry

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: SadalesTiklsConfigEntry) -> bool:
    """Set up Sadales Tīkls from a config entry.

    Validates the APIKEY against the API; on auth failure starts a reauth
    flow, on connectivity failure raises ConfigEntryNotReady so HA retries
    with backoff.
    """
    session = async_get_clientsession(hass)
    api = SadalesTiklsAPI(session, entry.data[CONF_API_KEY])

    try:
        await api.get_object_list()
    except SadalesTiklsAuthError as err:
        # Will trigger HA's reauth flow.
        raise ConfigEntryAuthFailed("Sadales Tīkls APIKEY rejected") from err
    except SadalesTiklsConnectionError as err:
        raise ConfigEntryNotReady(f"Cannot reach Sadales Tīkls API: {err}") from err
    except SadalesTiklsError as err:
        raise ConfigEntryNotReady(f"Sadales Tīkls API returned an error: {err}") from err

    entry.runtime_data = SadalesTiklsRuntimeData(api=api)

    # Reload the entry whenever options change so the coordinator (step 3)
    # picks up the new interval / backfill / consumption-field settings.
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    # No platforms forwarded yet — coordinator and sensors land in steps 3-5.
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SadalesTiklsConfigEntry) -> bool:
    """Tear down a config entry. Once we forward to the sensor platform,
    this needs to call `hass.config_entries.async_unload_platforms`.
    """
    return True


async def _async_update_listener(hass: HomeAssistant, entry: SadalesTiklsConfigEntry) -> None:
    """Reload the entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)
