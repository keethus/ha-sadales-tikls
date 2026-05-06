"""Config flow for the Sadales Tīkls integration.

Flow shape
----------

User add:
  user (paste APIKEY)
    └─ validate via /get-object-list
        ├─ no active objects   → abort
        ├─ already configured  → abort (unique_id = customer cEIC)
        └─ objects (multi-select active oEICs, default = all)
            └─ async_create_entry

Reauth (APIKEY rotated in e-st.lv):
  reauth_confirm (paste new APIKEY)
    └─ validate, ensure same customer cEIC as the existing entry
        └─ async_update_reload_and_abort

Options (Configure button on the entry):
  init (update interval, backfill window, consumption value)

Notes
-----
* The customer EIC (`cEIC`) returned by /get-object-list is used as the
  config entry's unique_id, so re-adding the same APIKEY (or any key that
  authenticates as the same customer) is rejected with
  `already_configured`.
* `selected objects` is **entry data** as the spec mandates; changing the
  set of imported objects requires removing and re-adding the entry. Per-
  entry tunables (interval, backfill, value selection) live in
  `entry.options` and are editable via the options flow.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_API_KEY
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    SadalesTiklsAPI,
    SadalesTiklsAuthError,
    SadalesTiklsConnectionError,
    SadalesTiklsError,
    SadalesTiklsRateLimitError,
)
from .const import (
    CONF_BACKFILL_DAYS,
    CONF_CONSUMPTION_FIELD,
    CONF_OBJECTS,
    CONF_UPDATE_INTERVAL,
    CONSUMPTION_FIELD_BILLING,
    CONSUMPTION_FIELD_RAW,
    DEFAULT_BACKFILL_DAYS,
    DEFAULT_CONSUMPTION_FIELD,
    DEFAULT_UPDATE_INTERVAL_MIN,
    DOMAIN,
    MAX_BACKFILL_DAYS,
    MAX_UPDATE_INTERVAL_MIN,
    MIN_BACKFILL_DAYS,
    MIN_UPDATE_INTERVAL_MIN,
    OBJECT_STATUS_ACTIVE,
)

if TYPE_CHECKING:
    from .api import ObjectInfo, ObjectListResponse

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_API_KEY): selector.TextSelector(
            selector.TextSelectorConfig(
                type=selector.TextSelectorType.PASSWORD,
                autocomplete="off",
            )
        ),
    }
)


_REAUTH_SCHEMA = _USER_SCHEMA


def _build_objects_schema(
    active_objects: list[ObjectInfo],
    *,
    default: list[str] | None = None,
) -> vol.Schema:
    """Schema for the per-object multi-select.

    The default is "all active objects" which matches the spec's
    "default all selected".
    """
    options = [
        selector.SelectOptionDict(
            value=obj["oEIC"],
            label=f"{obj['oName']} — {obj['oAddr']}",
        )
        for obj in active_objects
    ]
    default_values = default if default is not None else [obj["oEIC"] for obj in active_objects]
    return vol.Schema(
        {
            vol.Required(CONF_OBJECTS, default=default_values): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=options,
                    multiple=True,
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
        }
    )


_CONSUMPTION_FIELD_OPTIONS = [
    selector.SelectOptionDict(
        value=CONSUMPTION_FIELD_BILLING, label="Billing value (cVV) — recommended"
    ),
    selector.SelectOptionDict(value=CONSUMPTION_FIELD_RAW, label="Raw read (cVR)"),
]


def _build_options_schema(current: dict[str, Any]) -> vol.Schema:
    """Schema for the options flow.

    `current` is the entry's existing options dict; missing keys fall back
    to module-level defaults.
    """
    return vol.Schema(
        {
            vol.Required(
                CONF_UPDATE_INTERVAL,
                default=current.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL_MIN),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=MIN_UPDATE_INTERVAL_MIN,
                    max=MAX_UPDATE_INTERVAL_MIN,
                    step=15,
                    unit_of_measurement="min",
                    mode=selector.NumberSelectorMode.SLIDER,
                )
            ),
            vol.Required(
                CONF_BACKFILL_DAYS,
                default=current.get(CONF_BACKFILL_DAYS, DEFAULT_BACKFILL_DAYS),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=MIN_BACKFILL_DAYS,
                    max=MAX_BACKFILL_DAYS,
                    step=1,
                    unit_of_measurement="days",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_CONSUMPTION_FIELD,
                default=current.get(CONF_CONSUMPTION_FIELD, DEFAULT_CONSUMPTION_FIELD),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=_CONSUMPTION_FIELD_OPTIONS,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
        }
    )


# ---------------------------------------------------------------------------
# APIKEY validation
# ---------------------------------------------------------------------------


async def _validate_api_key(
    hass: Any, api_key: str
) -> tuple[ObjectListResponse, dict[str, str] | None]:
    """Hit /get-object-list to verify the key.

    Returns (response, errors). On success errors is None; on failure
    response is an empty dict and errors maps the form `base` key to a
    translation key from strings.json.
    """
    session = async_get_clientsession(hass)
    api = SadalesTiklsAPI(session, api_key)
    try:
        return await api.get_object_list(), None
    except SadalesTiklsAuthError:
        return {"cEIC": "", "cName": "", "oList": []}, {"base": "invalid_auth"}
    except SadalesTiklsRateLimitError:
        return {"cEIC": "", "cName": "", "oList": []}, {"base": "rate_limited"}
    except SadalesTiklsConnectionError:
        return {"cEIC": "", "cName": "", "oList": []}, {"base": "cannot_connect"}
    except SadalesTiklsError:
        _LOGGER.exception("Unexpected response from Sadales Tīkls during validation")
        return {"cEIC": "", "cName": "", "oList": []}, {"base": "unknown"}


# ---------------------------------------------------------------------------
# Config flow
# ---------------------------------------------------------------------------


class SadalesTiklsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle config-entry creation and reauth for Sadales Tīkls."""

    VERSION = 1

    def __init__(self) -> None:
        self._api_key: str | None = None
        self._customer_eic: str | None = None
        self._customer_name: str | None = None
        self._active_objects: list[ObjectInfo] = []

    # -- user-initiated add ------------------------------------------------

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Step 1: ask for the APIKEY."""
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=_USER_SCHEMA)

        api_key = user_input[CONF_API_KEY].strip()
        response, errors = await _validate_api_key(self.hass, api_key)
        if errors is not None:
            return self.async_show_form(step_id="user", data_schema=_USER_SCHEMA, errors=errors)

        active = [o for o in response["oList"] if o["oStatus"] == OBJECT_STATUS_ACTIVE]
        if not active:
            return self.async_abort(reason="no_active_objects")

        await self.async_set_unique_id(response["cEIC"])
        self._abort_if_unique_id_configured()

        self._api_key = api_key
        self._customer_eic = response["cEIC"]
        self._customer_name = response["cName"]
        self._active_objects = active

        return await self.async_step_objects()

    # -- object selection --------------------------------------------------

    async def async_step_objects(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2: pick which active objects to import."""
        if user_input is None:
            return self.async_show_form(
                step_id="objects",
                data_schema=_build_objects_schema(self._active_objects),
                description_placeholders={
                    "customer_name": self._customer_name or "",
                    "active_count": str(len(self._active_objects)),
                },
            )

        selected: list[str] = list(user_input[CONF_OBJECTS])
        if not selected:
            return self.async_show_form(
                step_id="objects",
                data_schema=_build_objects_schema(self._active_objects),
                errors={"base": "no_objects_selected"},
            )

        assert self._api_key is not None
        assert self._customer_name is not None
        return self.async_create_entry(
            title=self._customer_name,
            data={
                CONF_API_KEY: self._api_key,
                CONF_OBJECTS: selected,
            },
            options={
                CONF_UPDATE_INTERVAL: DEFAULT_UPDATE_INTERVAL_MIN,
                CONF_BACKFILL_DAYS: DEFAULT_BACKFILL_DAYS,
                CONF_CONSUMPTION_FIELD: DEFAULT_CONSUMPTION_FIELD,
            },
        )

    # -- reauth -----------------------------------------------------------

    async def async_step_reauth(self, _entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Triggered when async_setup_entry raises ConfigEntryAuthFailed."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask the user for a new APIKEY; verify it belongs to the same customer."""
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm", data_schema=_REAUTH_SCHEMA)

        api_key = user_input[CONF_API_KEY].strip()
        response, errors = await _validate_api_key(self.hass, api_key)
        if errors is not None:
            return self.async_show_form(
                step_id="reauth_confirm",
                data_schema=_REAUTH_SCHEMA,
                errors=errors,
            )

        # `_get_reauth_entry` is provided by the ConfigFlow base class and
        # only callable inside a reauth flow; we never enter this method
        # outside of one.
        existing = self._get_reauth_entry()
        if existing.unique_id is not None and existing.unique_id != response["cEIC"]:
            return self.async_abort(reason="reauth_account_mismatch")

        return self.async_update_reload_and_abort(
            existing,
            data={**existing.data, CONF_API_KEY: api_key},
            reason="reauth_successful",
        )

    # -- options-flow factory --------------------------------------------

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> OptionsFlow:
        return SadalesTiklsOptionsFlow(config_entry)


# ---------------------------------------------------------------------------
# Options flow
# ---------------------------------------------------------------------------


class SadalesTiklsOptionsFlow(OptionsFlow):
    """Tunable per-entry settings: interval, backfill, value selection."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        # `self.config_entry` is provided by the base class on newer HA
        # versions, but keeping the stash here is harmless and keeps us
        # working on older releases too.
        self._entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            # NumberSelector returns floats — coerce the integer-typed
            # options back to int for consistency.
            cleaned = {
                CONF_UPDATE_INTERVAL: int(user_input[CONF_UPDATE_INTERVAL]),
                CONF_BACKFILL_DAYS: int(user_input[CONF_BACKFILL_DAYS]),
                CONF_CONSUMPTION_FIELD: user_input[CONF_CONSUMPTION_FIELD],
            }
            return self.async_create_entry(title="", data=cleaned)

        return self.async_show_form(
            step_id="init",
            data_schema=_build_options_schema(dict(self._entry.options)),
        )
