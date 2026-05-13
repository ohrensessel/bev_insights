"""Shared fixtures for MySkoda Insights tests.

Wires up `pytest_homeassistant_custom_component` so each test gets a fresh
`hass` instance and `custom_components/myskoda_insights/` is discovered as a
custom integration.
"""
from __future__ import annotations

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading of the custom_components/ directory for every test."""
    yield
