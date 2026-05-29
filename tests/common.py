"""Helpers shared between BEV Insights test modules."""
from __future__ import annotations

from typing import Any

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bev_insights.const import (
    CONF_CAPACITY_ACTUAL_ENTITY,
    CONF_CAPACITY_FACTORY,
    CONF_CHARGING_SENSOR,
    CONF_MILEAGE_SENSOR,
    CONF_NAME,
    CONF_RANGE_SENSOR,
    CONF_SOC_SENSOR,
    CONFIG_ENTRY_VERSION,
    DOMAIN,
)

# Entity IDs we'll use across tests. Concrete IDs (rather than fixtures
# generating random ones) make assertions readable.
SOC_ENTITY = "sensor.car_battery_percentage"
RANGE_ENTITY = "sensor.car_range"
MILEAGE_ENTITY = "sensor.car_mileage"
CHARGING_ENTITY = "binary_sensor.car_charging"
ACTUAL_CAPACITY_ENTITY = "input_number.car_actual_capacity"


def base_entry_data(**overrides: Any) -> dict[str, Any]:
    """Return a full v2 config-entry data dict with sensible defaults."""
    data: dict[str, Any] = {
        CONF_NAME: "Test Car",
        CONF_SOC_SENSOR: SOC_ENTITY,
        CONF_RANGE_SENSOR: RANGE_ENTITY,
        CONF_CAPACITY_FACTORY: 77.0,
        CONF_CAPACITY_ACTUAL_ENTITY: ACTUAL_CAPACITY_ENTITY,
        CONF_CHARGING_SENSOR: CHARGING_ENTITY,
        CONF_MILEAGE_SENSOR: MILEAGE_ENTITY,
    }
    data.update(overrides)
    return data


def make_entry(
    version: int = CONFIG_ENTRY_VERSION,
    options: dict[str, Any] | None = None,
    **overrides: Any,
) -> MockConfigEntry:
    """Build a MockConfigEntry — not added to hass yet."""
    data = overrides.pop("data", None) or base_entry_data(**overrides)
    return MockConfigEntry(
        domain=DOMAIN,
        version=version,
        data=data,
        options=options or {},
        title=data[CONF_NAME],
        unique_id=f"{data[CONF_SOC_SENSOR]}|{data[CONF_RANGE_SENSOR]}",
    )


def set_state(hass, entity_id: str, state: str, **attributes: Any) -> None:
    """Convenience wrapper around hass.states.async_set."""
    hass.states.async_set(entity_id, state, attributes or None)


def seed_history(history: Any, samples: Any) -> None:
    """Replace an ``EntityHistory``'s recorded samples with ``samples``.

    The sample deque is a private implementation detail of ``EntityHistory``.
    Routing every test that seeds history through this one helper means a
    change to that storage (deque → list, added insert-time pruning, a rename)
    only has to be chased here, not across ~130 call sites.

    ``samples`` is any iterable of ``(timestamp, value)`` pairs; passing an
    empty iterable clears the history.
    """
    history._samples.clear()
    history._samples.extend(samples)


def seed_baseline(hass, entry, baseline: Any) -> None:
    """Replace a ``ChargeTracker``'s baseline (test seam for its private state).

    Like :func:`seed_history`, this is the single place that pokes the
    tracker's private ``_baseline`` so a change to that attribute only has to
    be chased here. Callers fire the baseline-updated dispatcher signal
    themselves if the sensors under test need to react.
    """
    tracker = hass.data[DOMAIN][entry.entry_id]["tracker"]
    tracker._baseline = baseline
