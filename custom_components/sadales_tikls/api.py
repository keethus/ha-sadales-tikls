"""Async HTTP client for the Sadales Tīkls M2M API.

Reference: https://raw.githubusercontent.com/vermut/sadales-tikls-m2m/master/openapi.yaml

The client is intentionally thin: it owns request shaping, auth, error taxonomy,
and response-shape validation. Retries, scheduling, and business logic live in
the coordinator (next step).

Security: the API key is treated as a secret. It is sent only as a Bearer
header and never written to log records — not even at DEBUG. See
`test_api_key_never_logged` in tests/test_api.py.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Final, NotRequired, TypedDict, cast

import aiohttp
from aiohttp import ClientError, ClientTimeout

from .const import (
    API_BASE_URL,
    API_DEFAULT_TIMEOUT_S,
    API_ENDPOINT_OBJECT_CONSUMPTION,
    API_ENDPOINT_OBJECT_LIST,
    API_MAX_RANGE_DAYS,
)

_LOGGER = logging.getLogger(__name__)

_DEFAULT_TIMEOUT: Final = ClientTimeout(total=API_DEFAULT_TIMEOUT_S)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SadalesTiklsError(Exception):
    """Base exception for any failure raised by the Sadales Tīkls client."""


class SadalesTiklsAuthError(SadalesTiklsError):
    """The API key is invalid, expired, or rejected (HTTP 401 / 403).

    The coordinator should treat this as a signal to start the reauth flow.
    """


class SadalesTiklsRateLimitError(SadalesTiklsError):
    """Rate limit exceeded (HTTP 429). Caller should back off."""


class SadalesTiklsServerError(SadalesTiklsError):
    """Sadales Tīkls returned a 5xx response. Likely transient."""


class SadalesTiklsConnectionError(SadalesTiklsError):
    """Network-level failure: DNS, TCP, TLS, or timeout."""


class SadalesTiklsResponseError(SadalesTiklsError):
    """The HTTP call succeeded but the body could not be parsed or did not
    match the expected shape.
    """


# ---------------------------------------------------------------------------
# Response shapes (mirror the OpenAPI schema; field names are the API's, not
# Python-style — keeping them unchanged means we can pass parsed JSON straight
# through to the rest of the integration).
# ---------------------------------------------------------------------------


class Meter(TypedDict):
    """A single meter under a metering point."""

    mNr: str


class MeteringPoint(TypedDict):
    """A metering point on an object, holding one or more meters."""

    mpNr: str
    mList: list[Meter]


class ObjectInfo(TypedDict):
    """One object (a building / site) belonging to a customer.

    `oStatus` is "A" (active) or "I" (inactive).
    """

    oEIC: str
    oName: str
    oAddr: str
    oStatus: str
    mpList: list[MeteringPoint]


class ObjectListResponse(TypedDict):
    """Response payload of GET /get-object-list."""

    cEIC: str
    cName: str
    oList: list[ObjectInfo]


class ConsumptionEntry(TypedDict):
    """One hourly consumption record.

    `cDt` uses end-of-hour convention: hour 00–01 → cDt "...T01:00:00...".

    Real-world note: `cVRSt` is omitted by the API when the reading has no
    flag (i.e. the "all-good" case). Only `cDt` is reliably present in
    practice — the rest are marked NotRequired so the merge loop can defend
    itself with `.get()` instead of crashing on a missing key.
    """

    cDt: str
    cVR: NotRequired[float]
    cVV: NotRequired[float]
    cVRSt: NotRequired[str]


class ConsumptionMeter(TypedDict):
    """A meter's worth of consumption entries."""

    mNr: str
    cList: list[ConsumptionEntry]


class ConsumptionMeteringPoint(TypedDict):
    """Consumption grouped by metering point under one object."""

    mpNr: str
    mList: list[ConsumptionMeter]


ConsumptionResponse = list[ConsumptionMeteringPoint]


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class SadalesTiklsAPI:
    """Async client for the Sadales Tīkls M2M API.

    The session is owned by the caller (typically Home Assistant's shared
    aiohttp session); this client never opens or closes it.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        api_key: str,
        *,
        base_url: str = API_BASE_URL,
        timeout: ClientTimeout = _DEFAULT_TIMEOUT,
    ) -> None:
        if not api_key:
            raise ValueError("api_key must be a non-empty string")
        self._session = session
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    # -- public surface -----------------------------------------------------

    async def get_object_list(self) -> ObjectListResponse:
        """Fetch customer info and the list of objects with metering points.

        Raises:
            SadalesTiklsAuthError: 401 / 403 — bad or expired API key.
            SadalesTiklsRateLimitError: 429.
            SadalesTiklsServerError: 5xx.
            SadalesTiklsConnectionError: network or timeout.
            SadalesTiklsResponseError: unparsable or unexpected response shape.
        """
        data = await self._request("GET", API_ENDPOINT_OBJECT_LIST)
        if not isinstance(data, dict) or "oList" not in data:
            raise SadalesTiklsResponseError(
                f"Unexpected response from {API_ENDPOINT_OBJECT_LIST}: "
                f"expected object with 'oList', got {type(data).__name__}"
            )
        return cast(ObjectListResponse, data)

    async def get_object_consumption(
        self,
        o_eic: str,
        d_from: datetime,
        d_to: datetime,
        *,
        mp_nr: str | None = None,
        m_nr: str | None = None,
    ) -> ConsumptionResponse:
        """Fetch hourly consumption for a single object over [d_from, d_to].

        Args:
            o_eic: Object identifier (`oEIC`).
            d_from: Period start (timezone-aware datetime recommended).
            d_to: Period end (timezone-aware datetime recommended).
            mp_nr: Optional — narrow to one metering point.
            m_nr: Optional — narrow to one meter (within mp_nr).

        Raises:
            ValueError: d_to < d_from, or range exceeds API_MAX_RANGE_DAYS.
            SadalesTiklsAuthError / RateLimitError / ServerError / ConnectionError /
                ResponseError: see `get_object_list`.
        """
        if not o_eic:
            raise ValueError("o_eic must be a non-empty string")
        if d_to < d_from:
            raise ValueError("d_to must be >= d_from")
        if (d_to - d_from) > timedelta(days=API_MAX_RANGE_DAYS):
            raise ValueError(
                "Date range exceeds Sadales Tīkls per-request limit of "
                f"{API_MAX_RANGE_DAYS} days; split the request"
            )

        params: dict[str, str] = {
            "oEIC": o_eic,
            "dF": d_from.isoformat(),
            "dT": d_to.isoformat(),
        }
        if mp_nr is not None:
            params["mpNr"] = mp_nr
        if m_nr is not None:
            params["mNr"] = m_nr

        data = await self._request("GET", API_ENDPOINT_OBJECT_CONSUMPTION, params=params)
        if not isinstance(data, list):
            raise SadalesTiklsResponseError(
                f"Unexpected response from {API_ENDPOINT_OBJECT_CONSUMPTION}: "
                f"expected array, got {type(data).__name__}"
            )
        return cast(ConsumptionResponse, data)

    # -- internals ----------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
    ) -> Any:
        url = f"{self._base_url}{path}"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        }
        # Log path + (non-secret) params only. Never include `headers` or
        # `self._api_key` in any log statement, at any level.
        _LOGGER.debug("Sadales Tīkls request: %s %s params=%s", method, path, params)

        try:
            async with self._session.request(
                method,
                url,
                params=params,
                headers=headers,
                timeout=self._timeout,
            ) as resp:
                status = resp.status
                if status in (401, 403):
                    raise SadalesTiklsAuthError(f"Authentication failed (HTTP {status})")
                if status == 429:
                    raise SadalesTiklsRateLimitError("Rate limit exceeded (HTTP 429)")
                if 500 <= status < 600:
                    raise SadalesTiklsServerError(f"Sadales Tīkls server error (HTTP {status})")
                if status >= 400:
                    body = (await resp.text())[:200]
                    raise SadalesTiklsResponseError(
                        f"Unexpected HTTP {status} from Sadales Tīkls: {body!r}"
                    )

                try:
                    return await resp.json(content_type=None)
                except (ValueError, ClientError) as err:
                    raise SadalesTiklsResponseError(
                        f"Failed to parse JSON response: {err}"
                    ) from err
        except SadalesTiklsError:
            raise
        except TimeoutError as err:
            raise SadalesTiklsConnectionError(
                f"Request to Sadales Tīkls timed out after {self._timeout.total}s"
            ) from err
        except ClientError as err:
            raise SadalesTiklsConnectionError(
                f"Network error talking to Sadales Tīkls: {err}"
            ) from err
