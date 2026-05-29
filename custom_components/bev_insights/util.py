"""Shared helpers for reading state values from HA entities."""
from __future__ import annotations

import logging

from homeassistant.const import (
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfLength,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, State

_LOGGER = logging.getLogger(__name__)

# State values that mean "no usable number".
INVALID_STATES: frozenset[str | None] = frozenset(
    {None, "", STATE_UNAVAILABLE, STATE_UNKNOWN}
)

# Conversion factors to kilometres for any distance unit we might see.
# HA may have converted km → mi for imperial users before we read state.state.
# Named with a leading underscore because it's an implementation detail, but
# exported for the backfill path (and tracker tests) that need unit conversion
# from historical State objects without going through hass.states.
_DISTANCE_TO_KM: dict[str, float] = {
    "km": 1.0,
    UnitOfLength.KILOMETERS: 1.0,
    "mi": 1.609344,
    UnitOfLength.MILES: 1.609344,
    "m": 0.001,
    UnitOfLength.METERS: 0.001,
}


def read_float(hass: HomeAssistant, entity_id: str) -> float | None:
    """Return an entity's state as float, or None when unusable."""
    state = hass.states.get(entity_id)
    if state is None or state.state in INVALID_STATES:
        return None
    try:
        return float(state.state)
    except (TypeError, ValueError):
        _LOGGER.debug(
            "Could not parse %s state %r as float", entity_id, state.state
        )
        return None


def read_distance_km(hass: HomeAssistant, entity_id: str) -> float | None:
    """Return an entity's state in kilometres regardless of source unit."""
    state = hass.states.get(entity_id)
    if state is None or state.state in INVALID_STATES:
        return None
    try:
        value = float(state.state)
    except (TypeError, ValueError):
        return None
    unit = state.attributes.get("unit_of_measurement") or "km"
    factor = _DISTANCE_TO_KM.get(unit)
    if factor is None:
        _LOGGER.warning(
            "Unknown distance unit %r on %s, assuming kilometres",
            unit,
            entity_id,
        )
        factor = 1.0
    return value * factor


def _fahrenheit_to_celsius(value: float) -> float:
    return (value - 32.0) * 5.0 / 9.0


def read_temperature_c(hass: HomeAssistant, entity_id: str) -> float | None:
    """Return an entity's temperature in °C regardless of source unit.

    HA may report the outside-temperature sensor in °F for imperial users;
    we normalise to °C so the band thresholds stay unit-agnostic.
    """
    state = hass.states.get(entity_id)
    if state is None or state.state in INVALID_STATES:
        return None
    return temperature_c_from_state(state)


def temperature_c_from_state(state: State) -> float | None:
    """Parse a (possibly historical) State's temperature into °C, or None."""
    if state.state in INVALID_STATES:
        return None
    try:
        value = float(state.state)
    except (TypeError, ValueError):
        return None
    unit = state.attributes.get("unit_of_measurement")
    if unit == UnitOfTemperature.FAHRENHEIT:
        return _fahrenheit_to_celsius(value)
    return value


# Values we consider "the car is charging right now".
_CHARGING_TRUTHY = frozenset({"on", "true", "charging"})


def is_charging(state: State | None) -> bool:
    """Best-effort detection from a charging sensor or binary sensor."""
    if state is None or state.state in INVALID_STATES:
        return False
    return str(state.state).strip().lower() in _CHARGING_TRUTHY
