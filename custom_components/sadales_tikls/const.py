"""Constants for the Sadales Tīkls integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "sadales_tikls"
MANUFACTURER: Final = "Sadales Tīkls"

# --- API ----------------------------------------------------------------------

API_BASE_URL: Final = "https://services.e-st.lv/m2m"
API_ENDPOINT_OBJECT_LIST: Final = "/get-object-list"
API_ENDPOINT_OBJECT_CONSUMPTION: Final = "/get-object-consumption"

# Sadales Tīkls limits a single consumption query to one year, counted back
# from `dT`. We cap a tiny bit above 365 to allow for leap-year edge cases.
API_MAX_RANGE_DAYS: Final = 366

# Total request timeout (seconds). The API is usually fast (<2s) but we leave
# a generous ceiling for backfill payloads.
API_DEFAULT_TIMEOUT_S: Final = 30

# --- Statistics ---------------------------------------------------------------

# `source` and statistic-id prefix used by `async_add_external_statistics`.
STATISTICS_SOURCE: Final = DOMAIN
STATISTICS_ID_PREFIX: Final = f"{DOMAIN}:consumption_"
STATISTICS_UNIT: Final = "kWh"

# --- Coordinator / scheduling ------------------------------------------------

DEFAULT_UPDATE_INTERVAL_MIN: Final = 60
MIN_UPDATE_INTERVAL_MIN: Final = 15
MAX_UPDATE_INTERVAL_MIN: Final = 6 * 60

# Hours of recent data to (re)fetch on every poll. Covers late-arriving readings
# without re-pulling the full history.
RECENT_FETCH_HOURS: Final = 26

# Days of history to (re)fetch once a day to capture retroactive corrections
# (cVRSt = D / M).
DAILY_CATCHUP_DAYS: Final = 7

DEFAULT_BACKFILL_DAYS: Final = 30
MIN_BACKFILL_DAYS: Final = 0
MAX_BACKFILL_DAYS: Final = 365

# --- Consumption value selection ---------------------------------------------

# `cVR` = first read value from meter (raw)
# `cVV` = value used for billing (post-corrections)
CONSUMPTION_FIELD_RAW: Final = "cVR"
CONSUMPTION_FIELD_BILLING: Final = "cVV"
DEFAULT_CONSUMPTION_FIELD: Final = CONSUMPTION_FIELD_BILLING

# --- cVRSt status codes ------------------------------------------------------

STATUS_INCOMPLETE: Final = "C"
STATUS_ADJUSTED: Final = "D"
STATUS_ROUNDING_CORRECTED: Final = "M"
STATUS_CORRECTED_BY_IS: Final = "E"
STATUS_COMM_ERROR: Final = "N"
STATUS_UNUSABLE: Final = "U"
STATUS_COMBINED: Final = "CD"

# Status codes that indicate the value should NOT be used.
SKIPPED_STATUSES: Final = frozenset({STATUS_UNUSABLE, STATUS_COMM_ERROR})

# --- Object status -----------------------------------------------------------

OBJECT_STATUS_ACTIVE: Final = "A"
OBJECT_STATUS_INACTIVE: Final = "I"

# --- Config entry / option keys ----------------------------------------------
#
# CONF_API_KEY is re-exported from `homeassistant.const` by callers — it's the
# canonical "api_key" string. The keys below are integration-specific.

CONF_OBJECTS: Final = "objects"
CONF_UPDATE_INTERVAL: Final = "update_interval_min"
CONF_BACKFILL_DAYS: Final = "backfill_days"
CONF_CONSUMPTION_FIELD: Final = "consumption_field"
