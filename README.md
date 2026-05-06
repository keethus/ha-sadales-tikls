# Sadales Tīkls (Latvia) — Home Assistant integration

[![GitHub Release][release-shield]][release]
[![License][license-shield]](LICENSE)
[![hacs][hacs-shield]][hacs]
![Project Stage][stage-shield]

_Custom Home Assistant integration for the Latvian electricity distribution
operator [**Sadales Tīkls**](https://sadalestikls.lv/) — pulls hourly
consumption data from the M2M API into Home Assistant for one or more
objects (homes, offices, warehouses) on your account._

> ⚠️ **Project stage: alpha — under active development.**
> Step 1 (API client + skeleton) is in place. Config flow, coordinator,
> external-statistics ingestion, and sensors land in subsequent steps.
> Track progress in the
> [issues](https://github.com/keethus/ha-sadales-tikls/issues).

## Table of contents

- [Features](#features)
- [How it works](#how-it-works)
- [Installation](#installation)
- [Configuration](#configuration)
- [Entities](#entities)
- [Options](#options)
- [Troubleshooting](#troubleshooting)
- [Privacy & security](#privacy--security)
- [Development](#development)
- [Contributing](#contributing)
- [License](#license)

## Features

- Reads **hourly electricity consumption** from the Sadales Tīkls M2M API
  (`https://services.e-st.lv/m2m`).
- Feeds consumption into the **Home Assistant Energy Dashboard** via the
  external-statistics API — values land on the correct historical hour, not
  as a cumulative figure arriving "now". Same pattern Tibber uses.
- Supports **multiple objects** (sites) on a single APIKEY — one HA *device*
  per object.
- **Backfills** the last 30 days on first setup (configurable, up to a year).
- **Daily catch-up** sweep handles retroactive corrections (`cVRSt = D / M`).
- Display sensors for last hour, today, yesterday, month-to-date, and
  previous month — plus diagnostic sensors for data lag and read status.
- **No third-party Python dependencies**: the integration ships with
  `requirements: []` and uses only `aiohttp` from HA core.

## How it works

Sadales Tīkls' smart-meter consumption arrives with a **1-hour to 1-day
delay** — readings are batched and may be retroactively corrected (rounding
fixes, comm-error replays, billing adjustments). A regular HA
`total_increasing` sensor would record those late-arriving readings against
"now", which is wrong for the Energy Dashboard.

This integration uses Home Assistant's
[`async_add_external_statistics`](https://developers.home-assistant.io/docs/core/entity/sensor/#long-term-statistics)
API instead. Each object gets a statistic id like
`sadales_tikls:consumption_<oeic>`, and the integration writes hourly values
*at their actual hour*. When the upstream data is corrected, the affected
hours are overwritten in place.

The regular HA sensors exposed by this integration (see [Entities](#entities))
are derived from the same data but are display-only — **not** the source for
the Energy Dashboard.

## Installation

### Option 1: HACS (recommended)

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=keethus&repository=ha-sadales-tikls&category=integration)

Or manually in HACS:

1. **HACS → Integrations → ⋮ (top-right) → Custom repositories**
2. Add `https://github.com/keethus/ha-sadales-tikls` with category
   **Integration**.
3. Install **Sadales Tīkls** and restart Home Assistant.

### Option 2: Manual

1. Download the latest release from the
   [Releases page](https://github.com/keethus/ha-sadales-tikls/releases).
2. Copy `custom_components/sadales_tikls/` into your HA config's
   `custom_components/` directory.
3. Restart Home Assistant.

## Configuration

You'll need an **APIKEY** from the Sadales Tīkls customer portal:

1. Log in at <https://e-st.lv/>.
2. Open **Data Services** (Datu pakalpojumi).
3. Generate an `APIKEY` for the M2M API.
4. In Home Assistant: **Settings → Devices & Services → Add Integration**,
   search for **Sadales Tīkls**, paste the APIKEY.
5. Pick which objects (active sites on your account) to import. All active
   objects are selected by default.

When the APIKEY is rotated in the e-st.lv portal, HA will surface a reauth
notification — paste the new key without losing any history.

## Entities

Each selected object becomes a HA *device* with the following entities:

| Entity                          | Class       | Description                                              |
| ------------------------------- | ----------- | -------------------------------------------------------- |
| `last_hour_consumption`         | sensor      | Most recent hourly value (kWh)                           |
| `today_consumption`             | sensor      | Sum of today's hours so far (kWh)                        |
| `yesterday_consumption`         | sensor      | Full previous day (kWh)                                  |
| `month_to_date_consumption`     | sensor      | Current calendar month so far (kWh)                      |
| `previous_month_consumption`    | sensor      | Full previous calendar month (kWh)                       |
| `data_lag`                      | diagnostic  | Hours since the most recent data point                   |
| `last_hour_status`              | diagnostic  | `cVRSt` code for the last hour (e.g. `C`, `D`, `M`)      |

Plus the **external statistics stream** `sadales_tikls:consumption_<oeic>`
that backs the Energy Dashboard — this is *not* a HA entity, it's a
long-term-statistics source.

## Options

Configurable per integration entry (Settings → Devices & Services → Sadales
Tīkls → Configure):

| Option              | Default | Range          | Notes                                                                              |
| ------------------- | ------- | -------------- | ---------------------------------------------------------------------------------- |
| Update interval     | 60 min  | 15 min – 6 h   | How often to poll for new hourly data.                                             |
| Backfill window     | 30 days | 0 – 365        | Days of history fetched on first setup.                                            |
| Consumption value   | `cVV`   | `cVR` / `cVV`  | `cVR` = raw meter read, `cVV` = post-correction (billing). `cVV` is recommended.   |
| Selected objects    | all active | —          | Which `oEIC`s on your account to import.                                           |

## Troubleshooting

**Energy Dashboard isn't showing my consumption.**
External statistics need a moment to backfill on first setup — check
**Settings → System → Repairs** and **Developer Tools → Statistics** for
the `sadales_tikls:consumption_<oeic>` source. The HA Energy Dashboard
re-reads stats hourly.

**The first 24 hours look empty.**
Sadales Tīkls reports consumption with a 1-hour-to-1-day lag. The
`data_lag` diagnostic sensor tells you how stale the most recent reading
is. This is upstream behavior, not a bug.

**A retro-corrected hour now reads differently.**
Expected. The integration overwrites the affected hours during the daily
catch-up sweep when `cVRSt` is `D` (adjusted) or `M` (rounding-corrected).

**Enable debug logs** by adding to `configuration.yaml`:

```yaml
logger:
  default: warning
  logs:
    custom_components.sadales_tikls: debug
```

The integration is careful **never** to log your APIKEY — even at DEBUG.
Safe to share log snippets in issues.

## Privacy & security

- The APIKEY is stored in the Home Assistant config entry and treated as
  a secret. The client never logs it; a regression test asserts this.
- All API traffic is HTTPS to `services.e-st.lv`.
- Diagnostics dumps (Settings → Devices & Services → Sadales Tīkls →
  Download diagnostics) redact the APIKEY before saving.

## Development

```bash
git clone https://github.com/keethus/ha-sadales-tikls.git
cd ha-sadales-tikls
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

pytest          # unit tests against mocked aiohttp responses
mypy            # type-check (mypy --strict)
ruff check .    # lint
ruff format .   # format
```

API reference (the source of truth for the wire format):
<https://raw.githubusercontent.com/vermut/sadales-tikls-m2m/master/openapi.yaml>.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). PRs welcome.

## License

[MIT](LICENSE) © 2026 Karlis Barbars.

This project is not affiliated with AS *Sadales Tīkls*.

---

[release-shield]: https://img.shields.io/github/v/release/keethus/ha-sadales-tikls?style=flat-square
[release]: https://github.com/keethus/ha-sadales-tikls/releases
[license-shield]: https://img.shields.io/github/license/keethus/ha-sadales-tikls?style=flat-square
[hacs-shield]: https://img.shields.io/badge/HACS-Custom-orange.svg?style=flat-square
[hacs]: https://github.com/hacs/integration
[stage-shield]: https://img.shields.io/badge/project%20stage-alpha-yellow.svg?style=flat-square
