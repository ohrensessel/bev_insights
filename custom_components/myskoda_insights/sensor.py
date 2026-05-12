"""Derived sensors for MySkoda Insights.

Each sensor reads values from existing myskoda entities and recomputes
itself whenever those sources change state.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfLength
from homeassistant.core import (
    Event,
    EventStateChangedData,
    HomeAssistant,
    callback,
)
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event

from .const import (
    CONF_RANGE_SENSOR,
    CONF_SOC_SENSOR,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up MySkoda Insights sensors from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]["data"]
    soc_entity: str = data[CONF_SOC_SENSOR]
    range_entity: str = data[CONF_RANGE_SENSOR]

    entities: list[SensorEntity] = [
        FullBatteryRangeSensor(entry, soc_entity, range_entity),
    ]
    async_add_entities(entities)


# --------------------------------------------------------------------------- #
# Base class                                                                  #
# --------------------------------------------------------------------------- #


class MySkodaDerivedSensor(SensorEntity):
    """Base class for sensors that recompute when source entities change."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, source_entities: list[str]) -> None:
        self._entry = entry
        self._source_entities = source_entities
        self._attr_available = False

    async def async_added_to_hass(self) -> None:
        """Subscribe to state changes of source entities."""
        if self._source_entities:

            @callback
            def _state_listener(event: Event[EventStateChangedData]) -> None:
                self._recalculate()
                self.async_write_ha_state()

            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, self._source_entities, _state_listener
                )
            )

        self._recalculate()

    @callback
    def _recalculate(self) -> None:
        """Override in subclasses to update self._attr_native_value."""
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Sensors                                                                     #
# --------------------------------------------------------------------------- #


class FullBatteryRangeSensor(MySkodaDerivedSensor):
    """Electric range extrapolated to a 100% state of charge.

    Computed as:  range_at_100% = current_range / current_soc * 100
    """

    _attr_device_class = SensorDeviceClass.DISTANCE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfLength.KILOMETERS
    _attr_icon = "mdi:map-marker-distance"
    _attr_suggested_display_precision = 0
    _attr_translation_key = "full_battery_range"

    def __init__(
        self, entry: ConfigEntry, soc_entity: str, range_entity: str
    ) -> None:
        super().__init__(entry, [soc_entity, range_entity])
        self._soc_entity = soc_entity
        self._range_entity = range_entity
        self._attr_unique_id = f"{entry.entry_id}_full_battery_range"
        self._attr_name = "Full battery range"

    @callback
    def _recalculate(self) -> None:
        soc_state = self.hass.states.get(self._soc_entity)
        range_state = self.hass.states.get(self._range_entity)

        if soc_state is None or range_state is None:
            self._attr_available = False
            self._attr_native_value = None
            return

        try:
            soc = float(soc_state.state)
            current_range = float(range_state.state)
        except (TypeError, ValueError):
            self._attr_available = False
            self._attr_native_value = None
            return

        if soc <= 0 or current_range < 0:
            self._attr_available = False
            self._attr_native_value = None
            return

        soc = min(soc, 100.0)
        self._attr_available = True
        self._attr_native_value = round(current_range * 100.0 / soc, 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        soc_state = self.hass.states.get(self._soc_entity)
        range_state = self.hass.states.get(self._range_entity)
        try:
            soc = float(soc_state.state) if soc_state else None
        except (TypeError, ValueError):
            soc = None
        try:
            current_range = float(range_state.state) if range_state else None
        except (TypeError, ValueError):
            current_range = None
        return {
            "soc_source": self._soc_entity,
            "range_source": self._range_entity,
            "current_soc_percent": soc,
            "current_range_km": current_range,
        }
