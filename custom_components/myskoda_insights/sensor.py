"""Derived sensors for MySkoda Insights.

Each sensor reads values from existing myskoda entities and recomputes
itself whenever those sources change state. Capacity-dependent sensors
are instantiated once per configured battery capacity (factory-new vs.
actual remaining).
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
from homeassistant.const import (
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfLength,
)
from homeassistant.core import (
    Event,
    EventStateChangedData,
    HomeAssistant,
    callback,
)
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event

from .const import (
    CONF_CAPACITY_ACTUAL,
    CONF_CAPACITY_FACTORY,
    CONF_RANGE_SENSOR,
    CONF_SOC_SENSOR,
    DEFAULT_CAPACITY_KWH,
    DOMAIN,
    UNIT_KWH_PER_100KM,
    VARIANT_ACTUAL,
    VARIANT_FACTORY,
)

_LOGGER = logging.getLogger(__name__)

_INVALID_STATES: frozenset[str | None] = frozenset(
    {None, "", STATE_UNAVAILABLE, STATE_UNKNOWN}
)

_DISTANCE_TO_KM: dict[str, float] = {
    "km": 1.0,
    "mi": 1.609344,
    "m": 0.001,
}


def _read_float(hass: HomeAssistant, entity_id: str) -> float | None:
    state = hass.states.get(entity_id)
    if state is None or state.state in _INVALID_STATES:
        return None
    try:
        return float(state.state)
    except (TypeError, ValueError):
        return None


def _read_distance_km(hass: HomeAssistant, entity_id: str) -> float | None:
    """Return an entity's state in kilometres regardless of source unit."""
    state = hass.states.get(entity_id)
    if state is None or state.state in _INVALID_STATES:
        return None
    try:
        value = float(state.state)
    except (TypeError, ValueError):
        return None
    unit = state.attributes.get("unit_of_measurement") or "km"
    factor = _DISTANCE_TO_KM.get(unit, 1.0)
    return value * factor


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up MySkoda Insights sensors from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]["data"]
    soc_entity: str = data[CONF_SOC_SENSOR]
    range_entity: str = data[CONF_RANGE_SENSOR]
    capacity_factory = float(data.get(CONF_CAPACITY_FACTORY, DEFAULT_CAPACITY_KWH))
    capacity_actual = float(data.get(CONF_CAPACITY_ACTUAL, DEFAULT_CAPACITY_KWH))

    entities: list[SensorEntity] = [
        FullBatteryRangeSensor(entry, soc_entity, range_entity),
        EfficiencySensor(
            entry, soc_entity, range_entity,
            capacity_kwh=capacity_factory,
            capacity_variant=VARIANT_FACTORY,
        ),
        EfficiencySensor(
            entry, soc_entity, range_entity,
            capacity_kwh=capacity_actual,
            capacity_variant=VARIANT_ACTUAL,
        ),
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

    @property
    def device_info(self) -> DeviceInfo:
        """Group all derived sensors of one config entry under one device."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=self._entry.title,
            manufacturer="MySkoda Insights",
            entry_type=DeviceEntryType.SERVICE,
        )

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
        soc = _read_float(self.hass, self._soc_entity)
        current_range = _read_distance_km(self.hass, self._range_entity)

        if soc is None or current_range is None or soc <= 0 or current_range < 0:
            self._attr_available = False
            self._attr_native_value = None
            return

        soc = min(soc, 100.0)
        self._attr_available = True
        self._attr_native_value = round(current_range * 100.0 / soc, 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "soc_source": self._soc_entity,
            "range_source": self._range_entity,
            "current_soc_percent": _read_float(self.hass, self._soc_entity),
            "current_range_km": _read_distance_km(self.hass, self._range_entity),
        }


class EfficiencySensor(MySkodaDerivedSensor):
    """Implied driving efficiency derived from the car's range prediction.

        kWh/100 km = capacity * soc / range_km
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UNIT_KWH_PER_100KM
    _attr_icon = "mdi:lightning-bolt"
    _attr_suggested_display_precision = 1

    def __init__(
        self,
        entry: ConfigEntry,
        soc_entity: str,
        range_entity: str,
        capacity_kwh: float,
        capacity_variant: str,
    ) -> None:
        super().__init__(entry, [soc_entity, range_entity])
        self._soc_entity = soc_entity
        self._range_entity = range_entity
        self._capacity_kwh = capacity_kwh
        self._capacity_variant = capacity_variant

        self._attr_unique_id = f"{entry.entry_id}_efficiency_{capacity_variant}"
        self._attr_translation_key = f"efficiency_{capacity_variant}"
        self._attr_name = f"Efficiency ({capacity_variant} capacity, kWh/100 km)"

    @callback
    def _recalculate(self) -> None:
        soc = _read_float(self.hass, self._soc_entity)
        current_range = _read_distance_km(self.hass, self._range_entity)

        if (
            soc is None
            or current_range is None
            or soc <= 0
            or current_range <= 0
            or self._capacity_kwh <= 0
        ):
            self._attr_available = False
            self._attr_native_value = None
            return

        soc = min(soc, 100.0)
        self._attr_available = True
        self._attr_native_value = round(
            self._capacity_kwh * soc / current_range, 2
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "capacity_variant": self._capacity_variant,
            "capacity_kwh": self._capacity_kwh,
            "soc_source": self._soc_entity,
            "range_source": self._range_entity,
        }
