"""Sensors that read only live source entities (no tracker, no history).

- FullBatteryRangeSensor: range / soc * 100
- StateOfHealthSensor: actual / factory * 100
- EfficiencySensor: kWh/100 km or km/kWh from the car's range prediction
  (instantiated 4× per config entry: factory|actual × kWh/100km|km/kWh)
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfLength
from homeassistant.core import callback

from custom_components.bev_insights.capacity import CapacitySource
from custom_components.bev_insights.util import read_distance_km, read_float

from .base import BevDerivedSensor
from .formulas import _efficiency_value, _human_unit, _unit_variant_props


class FullBatteryRangeSensor(BevDerivedSensor):
    """Electric range extrapolated to a 100% state of charge.

    Computed as:  range_at_100% = current_range / current_soc * 100
    Uses the car's own range prediction, scaled by SoC.
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
        soc = read_float(self.hass, self._soc_entity)
        current_range = read_distance_km(self.hass, self._range_entity)

        if (
            soc is None
            or current_range is None
            or soc <= 0
            or current_range < 0
        ):
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
            "current_soc_percent": read_float(self.hass, self._soc_entity),
            "current_range_km": read_distance_km(
                self.hass, self._range_entity
            ),
        }


class StateOfHealthSensor(BevDerivedSensor):
    """Battery health as a percentage of nameplate capacity.

        state_of_health = actual / factory * 100

    Single sensor per entry — no unit or capacity variants. Recomputes
    whenever the actual-capacity source entity changes.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_icon = "mdi:battery-heart-variant"
    _attr_suggested_display_precision = 1
    _attr_translation_key = "state_of_health"

    def __init__(
        self,
        entry: ConfigEntry,
        capacity_factory: CapacitySource,
        capacity_actual: CapacitySource,
    ) -> None:
        sources = [
            cap.source_entity
            for cap in (capacity_factory, capacity_actual)
            if cap.source_entity
        ]
        super().__init__(entry, sources)
        self._capacity_factory = capacity_factory
        self._capacity_actual = capacity_actual
        self._attr_unique_id = f"{entry.entry_id}_state_of_health"
        self._attr_name = "State of Health"

    @callback
    def _recalculate(self) -> None:
        factory = self._capacity_factory.current()
        actual = self._capacity_actual.current()
        if factory is None or actual is None or factory <= 0:
            self._attr_available = False
            self._attr_native_value = None
            return
        self._attr_available = True
        self._attr_native_value = round(actual / factory * 100.0, 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "capacity_factory_kwh": self._capacity_factory.current(),
            "capacity_actual_kwh": self._capacity_actual.current(),
            "capacity_actual_source": self._capacity_actual.describe(),
        }


class EfficiencySensor(BevDerivedSensor):
    """Implied driving efficiency derived from the car's range prediction.

        kWh/100 km = capacity * soc / range_km
        km/kWh     = range_km / (capacity * soc / 100)

    Instantiated four times per config entry:
    {factory, actual} capacity × {kWh/100 km, km/kWh}.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        entry: ConfigEntry,
        soc_entity: str,
        range_entity: str,
        capacity: CapacitySource,
        capacity_variant: str,
        unit_variant: str,
    ) -> None:
        # Listen to the capacity-source entity too (if it's reactive) so
        # the sensor recomputes when the user moves the input_number slider.
        sources = [soc_entity, range_entity]
        if capacity.source_entity:
            sources.append(capacity.source_entity)
        super().__init__(entry, sources)
        self._soc_entity = soc_entity
        self._range_entity = range_entity
        self._capacity = capacity
        self._capacity_variant = capacity_variant
        self._unit_variant = unit_variant

        unit_label, icon, precision = _unit_variant_props(unit_variant)
        self._attr_native_unit_of_measurement = unit_label
        self._attr_icon = icon
        self._attr_suggested_display_precision = precision

        self._attr_unique_id = (
            f"{entry.entry_id}_efficiency_{capacity_variant}_{unit_variant}"
        )
        self._attr_translation_key = (
            f"efficiency_{capacity_variant}_{unit_variant}"
        )
        self._attr_name = (
            f"Efficiency ({capacity_variant} capacity, "
            f"{_human_unit(unit_variant)})"
        )

    @callback
    def _recalculate(self) -> None:
        soc = read_float(self.hass, self._soc_entity)
        current_range = read_distance_km(self.hass, self._range_entity)
        capacity_kwh = self._capacity.current()
        if capacity_kwh is None:
            self._attr_available = False
            self._attr_native_value = None
            return
        value = _efficiency_value(
            capacity_kwh=capacity_kwh,
            soc_percent=soc,
            distance_km=current_range,
            unit_variant=self._unit_variant,
        )
        if value is None:
            self._attr_available = False
            self._attr_native_value = None
            return
        self._attr_available = True
        self._attr_native_value = value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "capacity_variant": self._capacity_variant,
            "unit_variant": self._unit_variant,
            "capacity_kwh": self._capacity.current(),
            "capacity_source": self._capacity.describe(),
            "soc_source": self._soc_entity,
            "range_source": self._range_entity,
        }
