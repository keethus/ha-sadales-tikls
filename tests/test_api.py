"""Tests for `custom_components.sadales_tikls.api`.

The tests use `aioresponses` to stub HTTP traffic — no network is touched.
Coverage targets:
  * happy paths for both endpoints
  * full HTTP error taxonomy (401/403 → auth, 429 → rate limit, 5xx → server,
    4xx other → response error, network/timeout → connection error)
  * malformed JSON / unexpected top-level shape → response error
  * argument validation (empty key, empty oEIC, inverted/oversized date range)
  * query-parameter passthrough (mpNr / mNr)
  * Authorization header is set, and the API key is never written to logs
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp
import pytest
from aioresponses import aioresponses

from custom_components.sadales_tikls.api import (
    SadalesTiklsAPI,
    SadalesTiklsAuthError,
    SadalesTiklsConnectionError,
    SadalesTiklsRateLimitError,
    SadalesTiklsResponseError,
    SadalesTiklsServerError,
)
from custom_components.sadales_tikls.const import (
    API_BASE_URL,
    API_ENDPOINT_OBJECT_CONSUMPTION,
    API_ENDPOINT_OBJECT_LIST,
    API_MAX_RANGE_DAYS,
)

API_KEY = "secret-test-key-do-not-leak"

OBJECT_LIST_URL = f"{API_BASE_URL}{API_ENDPOINT_OBJECT_LIST}"
CONSUMPTION_URL_RE = re.compile(
    rf"^{re.escape(API_BASE_URL)}{re.escape(API_ENDPOINT_OBJECT_CONSUMPTION)}(\?.*)?$"
)


def _utc(year: int, month: int, day: int, hour: int = 0) -> datetime:
    return datetime(year, month, day, hour, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_constructor_rejects_empty_api_key(session: aiohttp.ClientSession) -> None:
    with pytest.raises(ValueError, match="api_key"):
        SadalesTiklsAPI(session, "")


# ---------------------------------------------------------------------------
# get_object_list — happy path & error taxonomy
# ---------------------------------------------------------------------------


async def test_get_object_list_happy_path(
    session: aiohttp.ClientSession, object_list_payload: dict[str, Any]
) -> None:
    api = SadalesTiklsAPI(session, API_KEY)
    with aioresponses() as m:
        m.get(OBJECT_LIST_URL, payload=object_list_payload, status=200)
        result = await api.get_object_list()

    assert result["cEIC"] == object_list_payload["cEIC"]
    assert result["cName"] == object_list_payload["cName"]
    assert len(result["oList"]) == len(object_list_payload["oList"])
    # The inactive object survives parsing — filtering is the caller's job.
    assert any(o["oStatus"] == "I" for o in result["oList"])


async def test_get_object_list_sets_bearer_header(
    session: aiohttp.ClientSession, object_list_payload: dict[str, Any]
) -> None:
    api = SadalesTiklsAPI(session, API_KEY)
    with aioresponses() as m:
        m.get(OBJECT_LIST_URL, payload=object_list_payload, status=200)
        await api.get_object_list()
        request_kwargs = next(iter(m.requests.values()))[0].kwargs
        sent_headers = request_kwargs["headers"]
        assert sent_headers["Authorization"] == f"Bearer {API_KEY}"
        assert sent_headers["Accept"] == "application/json"


@pytest.mark.parametrize("status", [401, 403])
async def test_get_object_list_auth_error(session: aiohttp.ClientSession, status: int) -> None:
    api = SadalesTiklsAPI(session, API_KEY)
    with aioresponses() as m:
        m.get(OBJECT_LIST_URL, status=status)
        with pytest.raises(SadalesTiklsAuthError):
            await api.get_object_list()


async def test_get_object_list_rate_limit(session: aiohttp.ClientSession) -> None:
    api = SadalesTiklsAPI(session, API_KEY)
    with aioresponses() as m:
        m.get(OBJECT_LIST_URL, status=429)
        with pytest.raises(SadalesTiklsRateLimitError):
            await api.get_object_list()


@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_get_object_list_server_error(session: aiohttp.ClientSession, status: int) -> None:
    api = SadalesTiklsAPI(session, API_KEY)
    with aioresponses() as m:
        m.get(OBJECT_LIST_URL, status=status)
        with pytest.raises(SadalesTiklsServerError):
            await api.get_object_list()


async def test_get_object_list_other_4xx_is_response_error(
    session: aiohttp.ClientSession,
) -> None:
    api = SadalesTiklsAPI(session, API_KEY)
    with aioresponses() as m:
        m.get(OBJECT_LIST_URL, status=418, body="I'm a teapot")
        with pytest.raises(SadalesTiklsResponseError):
            await api.get_object_list()


async def test_get_object_list_malformed_json(
    session: aiohttp.ClientSession,
) -> None:
    api = SadalesTiklsAPI(session, API_KEY)
    with aioresponses() as m:
        m.get(OBJECT_LIST_URL, status=200, body="<!doctype html>not json")
        with pytest.raises(SadalesTiklsResponseError):
            await api.get_object_list()


async def test_get_object_list_unexpected_shape(
    session: aiohttp.ClientSession,
) -> None:
    api = SadalesTiklsAPI(session, API_KEY)
    with aioresponses() as m:
        m.get(OBJECT_LIST_URL, status=200, payload=[1, 2, 3])
        with pytest.raises(SadalesTiklsResponseError):
            await api.get_object_list()


async def test_get_object_list_connection_error(
    session: aiohttp.ClientSession,
) -> None:
    api = SadalesTiklsAPI(session, API_KEY)
    with aioresponses() as m:
        m.get(OBJECT_LIST_URL, exception=aiohttp.ClientConnectionError("boom"))
        with pytest.raises(SadalesTiklsConnectionError):
            await api.get_object_list()


async def test_get_object_list_timeout(
    session: aiohttp.ClientSession,
) -> None:
    api = SadalesTiklsAPI(session, API_KEY)
    with aioresponses() as m:
        m.get(OBJECT_LIST_URL, exception=TimeoutError())
        with pytest.raises(SadalesTiklsConnectionError):
            await api.get_object_list()


# ---------------------------------------------------------------------------
# get_object_consumption — happy path, params, validation
# ---------------------------------------------------------------------------


async def test_get_object_consumption_happy_path(
    session: aiohttp.ClientSession, consumption_payload: list[dict[str, Any]]
) -> None:
    api = SadalesTiklsAPI(session, API_KEY)
    with aioresponses() as m:
        m.get(CONSUMPTION_URL_RE, payload=consumption_payload, status=200)
        result = await api.get_object_consumption(
            "12X-OBJ-OFFICE-RIGA0",
            _utc(2026, 5, 5),
            _utc(2026, 5, 6),
        )

    assert isinstance(result, list)
    assert result[0]["mpNr"] == consumption_payload[0]["mpNr"]
    cl = result[0]["mList"][0]["cList"]
    assert len(cl) == len(consumption_payload[0]["mList"][0]["cList"])
    assert cl[0]["cDt"] == "2026-05-05T01:00:00+03:00"


async def test_get_object_consumption_passes_required_params(
    session: aiohttp.ClientSession, consumption_payload: list[dict[str, Any]]
) -> None:
    api = SadalesTiklsAPI(session, API_KEY)
    d_from = _utc(2026, 5, 5)
    d_to = _utc(2026, 5, 6)
    with aioresponses() as m:
        m.get(CONSUMPTION_URL_RE, payload=consumption_payload, status=200)
        await api.get_object_consumption("12X-OBJ-A", d_from, d_to)
        sent = next(iter(m.requests.values()))[0].kwargs["params"]

    assert sent["oEIC"] == "12X-OBJ-A"
    assert sent["dF"] == d_from.isoformat()
    assert sent["dT"] == d_to.isoformat()
    assert "mpNr" not in sent
    assert "mNr" not in sent


async def test_get_object_consumption_passes_optional_params(
    session: aiohttp.ClientSession, consumption_payload: list[dict[str, Any]]
) -> None:
    api = SadalesTiklsAPI(session, API_KEY)
    with aioresponses() as m:
        m.get(CONSUMPTION_URL_RE, payload=consumption_payload, status=200)
        await api.get_object_consumption(
            "12X-OBJ-A",
            _utc(2026, 5, 5),
            _utc(2026, 5, 6),
            mp_nr="30000000001",
            m_nr="M-OFF-001",
        )
        sent = next(iter(m.requests.values()))[0].kwargs["params"]

    assert sent["mpNr"] == "30000000001"
    assert sent["mNr"] == "M-OFF-001"


async def test_get_object_consumption_unexpected_shape(
    session: aiohttp.ClientSession,
) -> None:
    api = SadalesTiklsAPI(session, API_KEY)
    with aioresponses() as m:
        m.get(CONSUMPTION_URL_RE, status=200, payload={"oops": "object not array"})
        with pytest.raises(SadalesTiklsResponseError):
            await api.get_object_consumption("12X-OBJ-A", _utc(2026, 5, 5), _utc(2026, 5, 6))


async def test_get_object_consumption_auth_error_propagates(
    session: aiohttp.ClientSession,
) -> None:
    api = SadalesTiklsAPI(session, API_KEY)
    with aioresponses() as m:
        m.get(CONSUMPTION_URL_RE, status=401)
        with pytest.raises(SadalesTiklsAuthError):
            await api.get_object_consumption("12X-OBJ-A", _utc(2026, 5, 5), _utc(2026, 5, 6))


async def test_get_object_consumption_validates_empty_oeic(
    session: aiohttp.ClientSession,
) -> None:
    api = SadalesTiklsAPI(session, API_KEY)
    # ValueError is raised before any I/O — no aioresponses needed.
    with pytest.raises(ValueError, match="o_eic"):
        await api.get_object_consumption("", _utc(2026, 5, 5), _utc(2026, 5, 6))


async def test_get_object_consumption_validates_inverted_range(
    session: aiohttp.ClientSession,
) -> None:
    api = SadalesTiklsAPI(session, API_KEY)
    with pytest.raises(ValueError, match="d_to must be"):
        await api.get_object_consumption("12X-OBJ-A", _utc(2026, 5, 6), _utc(2026, 5, 5))


async def test_get_object_consumption_validates_oversized_range(
    session: aiohttp.ClientSession,
) -> None:
    api = SadalesTiklsAPI(session, API_KEY)
    too_big = timedelta(days=API_MAX_RANGE_DAYS + 1)
    d_from = _utc(2025, 1, 1)
    with pytest.raises(ValueError, match="exceeds"):
        await api.get_object_consumption("12X-OBJ-A", d_from, d_from + too_big)


# ---------------------------------------------------------------------------
# Logging / secret hygiene
# ---------------------------------------------------------------------------


async def test_api_key_never_logged(
    session: aiohttp.ClientSession,
    object_list_payload: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Nothing the client logs — at any level — should contain the API key."""
    api = SadalesTiklsAPI(session, API_KEY)
    with (
        caplog.at_level(logging.DEBUG, logger="custom_components.sadales_tikls.api"),
        aioresponses() as m,
    ):
        m.get(OBJECT_LIST_URL, payload=object_list_payload, status=200)
        await api.get_object_list()

    full_log = "\n".join(r.getMessage() for r in caplog.records)
    assert API_KEY not in full_log
    assert "Bearer" not in full_log
