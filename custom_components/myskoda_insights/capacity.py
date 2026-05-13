"""Capacity sources for MySkoda Insights.

Two flavours:
- `FixedCapacity` returns a constant kWh value (used for the factory /
  nameplate capacity).
- `EntityCapacity` reads its value from another HA entity (input_number,
  sensor, etc.) every time it's asked. Used for the actual remaining
  capacity so the user can update it from a dashboard slider, automation
  or external source without reconfiguring the integration.

Both expose the same minimal interface:

    .current() -> float | None
        Returns the capacity in kWh right now, or None if unavailable.
    .source_entity -> str | None
        Entity to listen on for change events, or None for fixed sources.
    .describe() -> str
        Human-readable identifier for diagnostics / state attributes.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from homeassistant.core import HomeAssistant

from .util import read_float

_LOGGER = logging.getLogger(__name__)


class CapacitySource(ABC):
    """Strategy for obtaining a battery capacity (kWh) at calc-time."""

    @abstractmethod
    def current(self) -> float | None:
        """Return the current capacity in kWh, or None when unusable."""

    @property
    def source_entity(self) -> str | None:
        """Entity to subscribe to for live updates; None if not reactive."""
        return None

    @abstractmethod
    def describe(self) -> str:
        """Short label used in state attributes."""


class FixedCapacity(CapacitySource):
    """A constant capacity, configured via the config flow."""

    def __init__(self, value_kwh: float) -> None:
        self._value = float(value_kwh)

    def current(self) -> float | None:
        return self._value if self._value > 0 else None

    def describe(self) -> str:
        return f"{self._value:g} kWh (fixed)"


class EntityCapacity(CapacitySource):
    """A capacity sourced from another HA entity.

    The entity's state is read at every recalculation, so changes
    propagate immediately. Invalid / unavailable / non-numeric states
    cause `current()` to return None — sensors then go unavailable
    rather than carry stale data.
    """

    def __init__(self, hass: HomeAssistant, entity_id: str) -> None:
        self._hass = hass
        self._entity_id = entity_id

    def current(self) -> float | None:
        value = read_float(self._hass, self._entity_id)
        if value is None or value <= 0:
            return None
        return value

    @property
    def source_entity(self) -> str | None:
        return self._entity_id

    def describe(self) -> str:
        return f"{self._entity_id}"
