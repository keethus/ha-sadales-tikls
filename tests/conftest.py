"""Shared pytest fixtures for the Sadales Tīkls integration tests.

Uses `pytest-homeassistant-custom-component`, which provides the `hass`
fixture and supporting machinery. The `enable_custom_integrations`
auto-fixture below tells HA to discover our `custom_components/` directory
during tests.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load(name: str) -> Any:
    with (FIXTURES_DIR / name).open(encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture
def object_list_payload() -> dict[str, Any]:
    """A realistic /get-object-list response."""
    return _load("object_list.json")


@pytest.fixture
def consumption_payload() -> list[dict[str, Any]]:
    """A realistic /get-object-consumption response."""
    return _load("consumption.json")


@pytest.fixture
async def session() -> AsyncIterator[aiohttp.ClientSession]:
    """A fresh aiohttp ClientSession per test (api.py tests only)."""
    async with aiohttp.ClientSession() as s:
        yield s


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: Any,  # noqa: ARG001 — fixture is used for its side effect
) -> None:
    """Auto-enable the integration under test in every test.

    Without this, HA's `async_get_integration` cannot find
    `custom_components/sadales_tikls` and the config-flow tests fail at
    flow-init time.
    """
    return
