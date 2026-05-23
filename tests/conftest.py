"""Shared fixtures for BEV Insights tests.

Wires up `pytest_homeassistant_custom_component` so each test gets a fresh
`hass` instance and `custom_components/bev_insights/` is discovered as a
custom integration.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from syrupy.assertion import SnapshotAssertion
from syrupy.extensions.amber import AmberSnapshotExtension
from syrupy.location import PyTestLocation

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading of the custom_components/ directory for every test."""
    yield


class _BevSnapshotExtension(AmberSnapshotExtension):
    """Pin snapshot files to `tests/snapshots/` independent of plugin overrides.

    `pytest_homeassistant_custom_component` ships its own `snapshot`
    fixture that points syrupy at `snapshots/` instead of the default
    `__snapshots__/`. On the Python 3.14 + HA-dev CI matrix row that
    override stopped winning (likely a pytest/PHACC fixture-resolution
    quirk on the newer stack), and syrupy fell back to the default
    directory — surfacing as "Snapshot does not exist!" because our
    files live under `snapshots/`. Defining our own extension here in
    conftest guarantees the directory regardless of plugin behaviour.
    """

    @classmethod
    def dirname(cls, *, test_location: PyTestLocation) -> str:
        return str(Path(test_location.filepath).parent.joinpath("snapshots"))


@pytest.fixture
def snapshot(snapshot: SnapshotAssertion) -> SnapshotAssertion:
    """Override the inherited `snapshot` fixture with our pinned extension.

    Conftest fixtures take precedence over plugin fixtures, so this wins
    on every CI row — stable and dev alike.
    """
    return snapshot.use_extension(_BevSnapshotExtension)
