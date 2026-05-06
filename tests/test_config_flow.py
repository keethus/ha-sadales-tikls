"""Tests for `custom_components.sadales_tikls.config_flow`.

Flows under test:
  * user → objects → create entry (happy path)
  * user step: invalid_auth, cannot_connect, rate_limited, unknown
  * user step: account with no active objects → abort
  * user step: re-adding the same account → abort already_configured
  * objects step: empty selection re-shows form with error
  * reauth flow: success
  * reauth flow: account-mismatch abort
  * reauth flow: invalid key re-shows form
  * options flow: edits round-trip into entry.options
  * `async_setup_entry`: ConfigEntryAuthFailed triggers reauth path
  * `async_setup_entry`: ConfigEntryNotReady on connection error
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from aioresponses import aioresponses
from homeassistant import config_entries
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sadales_tikls.const import (
    API_BASE_URL,
    API_ENDPOINT_OBJECT_LIST,
    CONF_BACKFILL_DAYS,
    CONF_CONSUMPTION_FIELD,
    CONF_OBJECTS,
    CONF_UPDATE_INTERVAL,
    CONSUMPTION_FIELD_RAW,
    DEFAULT_BACKFILL_DAYS,
    DEFAULT_CONSUMPTION_FIELD,
    DEFAULT_UPDATE_INTERVAL_MIN,
    DOMAIN,
)

OBJECT_LIST_URL = f"{API_BASE_URL}{API_ENDPOINT_OBJECT_LIST}"
CONSUMPTION_URL_RE = re.compile(
    rf"^{re.escape(API_BASE_URL)}{re.escape('/get-object-consumption')}(\?.*)?$"
)

API_KEY = "valid-test-key-do-not-leak"
ROTATED_KEY = "rotated-test-key"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stub_object_list_ok(m: aioresponses, payload: dict[str, Any], *, repeat: bool = False) -> None:
    m.get(OBJECT_LIST_URL, payload=payload, status=200, repeat=repeat)


def _empty_object_list(payload: dict[str, Any]) -> dict[str, Any]:
    return {**payload, "oList": []}


def _inactive_only_object_list(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        **payload,
        "oList": [{**o, "oStatus": "I"} for o in payload["oList"]],
    }


# ---------------------------------------------------------------------------
# User step
# ---------------------------------------------------------------------------


async def test_user_flow_happy_path(
    hass: HomeAssistant, object_list_payload: dict[str, Any]
) -> None:
    """user → objects → entry created with all active objects selected."""
    with aioresponses() as m:
        _stub_object_list_ok(m, object_list_payload, repeat=True)

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "user"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_KEY: API_KEY}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "objects"

        active_eics = [o["oEIC"] for o in object_list_payload["oList"] if o["oStatus"] == "A"]

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_OBJECTS: active_eics}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == object_list_payload["cName"]
    assert result["data"][CONF_API_KEY] == API_KEY
    assert result["data"][CONF_OBJECTS] == active_eics
    assert result["options"] == {
        CONF_UPDATE_INTERVAL: DEFAULT_UPDATE_INTERVAL_MIN,
        CONF_BACKFILL_DAYS: DEFAULT_BACKFILL_DAYS,
        CONF_CONSUMPTION_FIELD: DEFAULT_CONSUMPTION_FIELD,
    }


@pytest.mark.parametrize(
    ("status", "expected_error"),
    [
        (401, "invalid_auth"),
        (403, "invalid_auth"),
        (429, "rate_limited"),
        (500, "unknown"),
    ],
)
async def test_user_flow_api_errors_show_in_form(
    hass: HomeAssistant, status: int, expected_error: str
) -> None:
    with aioresponses() as m:
        m.get(OBJECT_LIST_URL, status=status)

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_KEY: API_KEY}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": expected_error}


async def test_user_flow_connection_error(hass: HomeAssistant) -> None:
    with aioresponses() as m:
        import aiohttp

        m.get(OBJECT_LIST_URL, exception=aiohttp.ClientConnectionError("boom"))

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_KEY: API_KEY}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_no_active_objects_aborts(
    hass: HomeAssistant, object_list_payload: dict[str, Any]
) -> None:
    payload = _inactive_only_object_list(object_list_payload)
    with aioresponses() as m:
        _stub_object_list_ok(m, payload)

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_KEY: API_KEY}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_active_objects"


async def test_user_flow_already_configured(
    hass: HomeAssistant, object_list_payload: dict[str, Any]
) -> None:
    """Adding the same customer (same cEIC) twice is rejected."""
    MockConfigEntry(
        domain=DOMAIN,
        unique_id=object_list_payload["cEIC"],
        data={CONF_API_KEY: API_KEY, CONF_OBJECTS: []},
    ).add_to_hass(hass)

    with aioresponses() as m:
        _stub_object_list_ok(m, object_list_payload)

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_KEY: API_KEY}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# ---------------------------------------------------------------------------
# Objects step
# ---------------------------------------------------------------------------


async def test_objects_step_empty_selection_re_shows_form(
    hass: HomeAssistant, object_list_payload: dict[str, Any]
) -> None:
    with aioresponses() as m:
        _stub_object_list_ok(m, object_list_payload, repeat=True)

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_KEY: API_KEY}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_OBJECTS: []}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "objects"
    assert result["errors"] == {"base": "no_objects_selected"}


# ---------------------------------------------------------------------------
# Reauth flow
# ---------------------------------------------------------------------------


async def test_reauth_flow_success(
    hass: HomeAssistant, object_list_payload: dict[str, Any]
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=object_list_payload["cEIC"],
        data={CONF_API_KEY: API_KEY, CONF_OBJECTS: ["12X-OBJ-OFFICE-RIGA0"]},
    )
    entry.add_to_hass(hass)

    with aioresponses() as m:
        _stub_object_list_ok(m, object_list_payload, repeat=True)

        result = await entry.start_reauth_flow(hass)
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "reauth_confirm"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_KEY: ROTATED_KEY}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_API_KEY] == ROTATED_KEY


async def test_reauth_flow_invalid_key_re_shows_form(
    hass: HomeAssistant, object_list_payload: dict[str, Any]
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=object_list_payload["cEIC"],
        data={CONF_API_KEY: API_KEY, CONF_OBJECTS: []},
    )
    entry.add_to_hass(hass)

    with aioresponses() as m:
        m.get(OBJECT_LIST_URL, status=401)

        result = await entry.start_reauth_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_KEY: ROTATED_KEY}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": "invalid_auth"}
    # Existing key is unchanged.
    assert entry.data[CONF_API_KEY] == API_KEY


async def test_reauth_flow_account_mismatch_aborts(
    hass: HomeAssistant, object_list_payload: dict[str, Any]
) -> None:
    """Rotating to an APIKEY that authenticates as a different customer
    aborts the reauth flow rather than silently swapping accounts."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="12X-CUSTOMER-ORIGINAL0",  # different from fixture
        data={CONF_API_KEY: API_KEY, CONF_OBJECTS: []},
    )
    entry.add_to_hass(hass)

    with aioresponses() as m:
        _stub_object_list_ok(m, object_list_payload, repeat=True)

        result = await entry.start_reauth_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_KEY: ROTATED_KEY}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_account_mismatch"
    assert entry.data[CONF_API_KEY] == API_KEY  # untouched


# ---------------------------------------------------------------------------
# Options flow
# ---------------------------------------------------------------------------


async def test_options_flow_round_trip(
    hass: HomeAssistant, object_list_payload: dict[str, Any]
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=object_list_payload["cEIC"],
        data={CONF_API_KEY: API_KEY, CONF_OBJECTS: ["12X-OBJ-OFFICE-RIGA0"]},
        options={
            CONF_UPDATE_INTERVAL: DEFAULT_UPDATE_INTERVAL_MIN,
            CONF_BACKFILL_DAYS: DEFAULT_BACKFILL_DAYS,
            CONF_CONSUMPTION_FIELD: DEFAULT_CONSUMPTION_FIELD,
        },
    )
    entry.add_to_hass(hass)

    with aioresponses() as m:
        _stub_object_list_ok(m, object_list_payload, repeat=True)
        # async_setup_entry validates the key
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "init"

        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_UPDATE_INTERVAL: 30,
                CONF_BACKFILL_DAYS: 90,
                CONF_CONSUMPTION_FIELD: CONSUMPTION_FIELD_RAW,
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {
        CONF_UPDATE_INTERVAL: 30,
        CONF_BACKFILL_DAYS: 90,
        CONF_CONSUMPTION_FIELD: CONSUMPTION_FIELD_RAW,
    }


# ---------------------------------------------------------------------------
# async_setup_entry
# ---------------------------------------------------------------------------


async def test_setup_entry_validates_and_loads(
    hass: HomeAssistant, object_list_payload: dict[str, Any]
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=object_list_payload["cEIC"],
        data={CONF_API_KEY: API_KEY, CONF_OBJECTS: ["12X-OBJ-OFFICE-RIGA0"]},
        options={
            CONF_UPDATE_INTERVAL: DEFAULT_UPDATE_INTERVAL_MIN,
            CONF_BACKFILL_DAYS: DEFAULT_BACKFILL_DAYS,
            CONF_CONSUMPTION_FIELD: DEFAULT_CONSUMPTION_FIELD,
        },
    )
    entry.add_to_hass(hass)

    with aioresponses() as m:
        _stub_object_list_ok(m, object_list_payload)
        ok = await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert ok
    assert entry.state is config_entries.ConfigEntryState.LOADED
    assert entry.runtime_data is not None
    assert entry.runtime_data.api is not None


async def test_setup_entry_auth_failure_starts_reauth(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="12X-CUSTOMER-EXAMPLE0",
        data={CONF_API_KEY: "stale-key", CONF_OBJECTS: []},
    )
    entry.add_to_hass(hass)

    with aioresponses() as m:
        m.get(OBJECT_LIST_URL, status=401)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is config_entries.ConfigEntryState.SETUP_ERROR
    flows_in_progress = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert any(
        f["context"].get("source") == config_entries.SOURCE_REAUTH for f in flows_in_progress
    )


async def test_setup_entry_connection_error_is_retryable(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="12X-CUSTOMER-EXAMPLE0",
        data={CONF_API_KEY: API_KEY, CONF_OBJECTS: []},
    )
    entry.add_to_hass(hass)

    with aioresponses() as m:
        import aiohttp

        m.get(OBJECT_LIST_URL, exception=aiohttp.ClientConnectionError("nope"))
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is config_entries.ConfigEntryState.SETUP_RETRY
