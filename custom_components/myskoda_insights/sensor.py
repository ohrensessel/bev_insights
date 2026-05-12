"""Derived sensors for MySkoda Insights.

Each sensor reads values from existing myskoda entities and recomputes
itself whenever those sources change state. Capacity-dependent sensors
are instantiated once per configured battery capacity (factory-new vs.
actual remaining).

The "measured full range" sensor is wired up to the ChargeTracker via the
HA dispatcher, so it also recomputes whenever a charging session ends and
the baseline is updated.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EntityCategory,
    UnitOfLength,
)
from homeassistant.core import (
    Event,
    EventStateChangedData,
    HomeAssistant,
    callback,
)
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from .const import (
    BASELINE_MILEAGE_KM,
    BASELINE_SOC_PERCENT,
    BASELINE_TIMESTAMP,
    CONF_CAPACITY_ACTUAL,
    CONF_CAPACITY_FACTORY,
    CONF_CHARGING_SENSOR,
    CONF_MILEAGE_SENSOR,
    CONF_RANGE_SENSOR,
    CONF_SOC_SENSOR,
    DEFAULT_CAPACITY_KWH,
    DOMAIN,
    UNIT_KM_PER_KWH,
    UNIT_KWH_PER_100KM,
    UNIT_VARIANT_KM_PER_KWH,
    UNIT_VARIANT_KWH_PER_100KM,
    VARIANT_ACTUAL,
    VARIANT_FACTORY,
    signal_baseline_updated,
)
from .tracker import ChargeTracker
from .util import read_distance_km, read_float

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up MySkoda Insights sensors from a config entry."""
    domain_data = hass.data[DOMAIN][entry.entry_id]
    data = domain_data["data"]
    tracker: ChargeTracker | None = domain_data.get("tracker")

    soc_entity: str = data[CONF_SOC_SENSOR]
    range_entity: str = data[CONF_RANGE_SENSOR]
    capacity_factory = float(data.get(CONF_CAPACITY_FACTORY, DEFAULT_CAPACITY_KWH))
    capacity_actual = float(data.get(CONF_CAPACITY_ACTUAL, DEFAULT_CAPACITY_KWH))

    entities: list[SensorEntity] = [
        FullBatteryRangeSensor(entry, soc_entity, range_entity),
    ]

    # Efficiency: 2 capacities × 2 units = 4 sensors
    for capacity_kwh, capacity_variant in (
        (capacity_factory, VARIANT_FACTORY),
        (capacity_actual, VARIANT_ACTUAL),
    ):
        for unit_variant in (UNIT_VARIANT_KWH_PER_100KM, UNIT_VARIANT_KM_PER_KWH):
            entities.append(
                EfficiencySensor(
                    entry, soc_entity, range_entity,
                    capacity_kwh=capacity_kwh,
                    capacity_variant=capacity_variant,
                    unit_variant=unit_variant,
                )
            )

    if tracker is not None:
        mileage_entity: str = data[CONF_MILEAGE_SENSOR]
        entities.append(
            MeasuredFullRangeSensor(entry, tracker, soc_entity, mileage_entity)
        )
        entities.append(LastChargedSensor(entry, tracker))

        # Measured efficiency: 2 capacities × 2 units = 4 sensors
        for capacity_kwh, capacity_variant in (
            (capacity_factory, VARIANT_FACTORY),
            (capacity_actual, VARIANT_ACTUAL),
        ):
            for unit_variant in (
                UNIT_VARIANT_KWH_PER_100KM,
                UNIT_VARIANT_KM_PER_KWH,
            ):
                entities.append(
                    MeasuredEfficiencySensor(
                        entry, tracker, soc_entity, mileage_entity,
                        capacity_kwh=capacity_kwh,
                        capacity_variant=capacity_variant,
                        unit_variant=unit_variant,
                    )
                )

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
# Capacity-independent sensors                                                #
# --------------------------------------------------------------------------- #


class FullBatteryRangeSensor(MySkodaDerivedSensor):
    """Electric range extrapolated to a 100% state of charge."""

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
            "current_soc_percent": read_float(self.hass, self._soc_entity),
            "current_range_km": read_distance_km(self.hass, self._range_entity),
        }


# --------------------------------------------------------------------------- #
# Capacity-dependent sensors                                                  #
# --------------------------------------------------------------------------- #


# Shared helpers for all efficiency sensors.

def _unit_variant_props(unit_variant: str) -> tuple[str, str, int]:
    """Return (HA unit string, icon, suggested precision) per unit variant."""
    if unit_variant == UNIT_VARIANT_KM_PER_KWH:
        return UNIT_KM_PER_KWH, "mdi:speedometer", 2
    return UNIT_KWH_PER_100KM, "mdi:lightning-bolt", 1


def _human_unit(unit_variant: str) -> str:
    """Friendly label used as the fallback entity name."""
    if unit_variant == UNIT_VARIANT_KM_PER_KWH:
        return "km/kWh"
    return "kWh/100 km"


def _efficiency_value(
    capacity_kwh: float,
    soc_percent: float | None,
    distance_km: float | None,
    unit_variant: str,
) -> float | None:
    """Compute one efficiency figure or return None for invalid inputs."""
    if (
        soc_percent is None
        or distance_km is None
        or soc_percent <= 0
        or distance_km <= 0
        or capacity_kwh <= 0
    ):
        return None

    soc_percent = min(soc_percent, 100.0)
    energy_kwh = capacity_kwh * soc_percent / 100.0

    if unit_variant == UNIT_VARIANT_KM_PER_KWH:
        return round(distance_km / energy_kwh, 3)
    return round(energy_kwh / distance_km * 100.0, 2)


class EfficiencySensor(MySkodaDerivedSensor):
    """Implied driving efficiency derived from the car's range prediction.

    Instantiated four times per config entry:
    {factory, actual} capacity × {kWh/100 km, km/kWh}.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        entry: ConfigEntry,
        soc_entity: str,
        range_entity: str,
        capacity_kwh: float,
        capacity_variant: str,
        unit_variant: str,
    ) -> None:
        super().__init__(entry, [soc_entity, range_entity])
        self._soc_entity = soc_entity
        self._range_entity = range_entity
        self._capacity_kwh = capacity_kwh
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
            f"Efficiency ({capacity_variant} capacity, {_human_unit(unit_variant)})"
        )

    @callback
    def _recalculate(self) -> None:
        soc = read_float(self.hass, self._soc_entity)
        current_range = read_distance_km(self.hass, self._range_entity)
        value = _efficiency_value(
            capacity_kwh=self._capacity_kwh,
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
            "capacity_kwh": self._capacity_kwh,
            "soc_source": self._soc_entity,
            "range_source": self._range_entity,
        }


# --------------------------------------------------------------------------- #
# Tracker-dependent sensors                                                   #
# --------------------------------------------------------------------------- #


class _TrackerLinkedMixin:
    """Adds a subscription to baseline-updated dispatcher signals."""

    _entry: ConfigEntry

    def _subscribe_baseline_updates(self) -> None:
        @callback
        def _baseline_listener() -> None:
            self._recalculate()  # type: ignore[attr-defined]
            self.async_write_ha_state()  # type: ignore[attr-defined]

        self.async_on_remove(  # type: ignore[attr-defined]
            async_dispatcher_connect(
                self.hass,  # type: ignore[attr-defined]
                signal_baseline_updated(self._entry.entry_id),
                _baseline_listener,
            )
        )


class MeasuredFullRangeSensor(_TrackerLinkedMixin, MySkodaDerivedSensor):
    """Range at 100% SoC, measured from actual driving since last charge."""

    _attr_device_class = SensorDeviceClass.DISTANCE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfLength.KILOMETERS
    _attr_icon = "mdi:map-marker-path"
    _attr_suggested_display_precision = 0
    _attr_translation_key = "measured_full_range"

    def __init__(
        self,
        entry: ConfigEntry,
        tracker: ChargeTracker,
        soc_entity: str,
        mileage_entity: str,
    ) -> None:
        super().__init__(entry, [soc_entity, mileage_entity])
        self._tracker = tracker
        self._soc_entity = soc_entity
        self._mileage_entity = mileage_entity
        self._attr_unique_id = f"{entry.entry_id}_measured_full_range"
        self._attr_name = "Measured full range"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._subscribe_baseline_updates()

    @callback
    def _recalculate(self) -> None:
        baseline = self._tracker.baseline
        if baseline is None:
            self._attr_available = False
            self._attr_native_value = None
            return

        baseline_mileage = baseline.get(BASELINE_MILEAGE_KM)
        baseline_soc = baseline.get(BASELINE_SOC_PERCENT)
        if baseline_mileage is None or baseline_soc is None:
            self._attr_available = False
            self._attr_native_value = None
            return

        current_mileage = read_distance_km(self.hass, self._mileage_entity)
        current_soc = read_float(self.hass, self._soc_entity)
        if current_mileage is None or current_soc is None:
            self._attr_available = False
            self._attr_native_value = None
            return

        distance_km = current_mileage - baseline_mileage
        soc_consumed = baseline_soc - current_soc

        if distance_km <= 0 or soc_consumed <= 0:
            self._attr_available = False
            self._attr_native_value = None
            return

        self._attr_available = True
        self._attr_native_value = round(distance_km / soc_consumed * 100.0, 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        baseline = self._tracker.baseline or {}
        current_mileage = read_distance_km(self.hass, self._mileage_entity)
        current_soc = read_float(self.hass, self._soc_entity)

        attrs: dict[str, Any] = {
            "baseline_mileage_km": baseline.get(BASELINE_MILEAGE_KM),
            "baseline_soc_percent": baseline.get(BASELINE_SOC_PERCENT),
            "baseline_timestamp": baseline.get(BASELINE_TIMESTAMP),
            "current_mileage_km": current_mileage,
            "current_soc_percent": current_soc,
        }
        if (
            baseline.get(BASELINE_MILEAGE_KM) is not None
            and current_mileage is not None
        ):
            attrs["distance_since_last_charge_km"] = round(
                current_mileage - baseline[BASELINE_MILEAGE_KM], 1
            )
        if (
            baseline.get(BASELINE_SOC_PERCENT) is not None
            and current_soc is not None
        ):
            attrs["soc_consumed_since_last_charge_percent"] = round(
                baseline[BASELINE_SOC_PERCENT] - current_soc, 1
            )
        return attrs


class LastChargedSensor(_TrackerLinkedMixin, MySkodaDerivedSensor):
    """Diagnostic: timestamp + mileage/SoC at the last detected charge end."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:battery-charging"
    _attr_translation_key = "last_charged"

    def __init__(self, entry: ConfigEntry, tracker: ChargeTracker) -> None:
        super().__init__(entry, source_entities=[])
        self._tracker = tracker
        self._attr_unique_id = f"{entry.entry_id}_last_charged"
        self._attr_name = "Last charged"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._subscribe_baseline_updates()

    @callback
    def _recalculate(self) -> None:
        baseline = self._tracker.baseline
        if baseline is None:
            self._attr_available = False
            self._attr_native_value = None
            return

        ts = baseline.get(BASELINE_TIMESTAMP)
        if not ts:
            self._attr_available = False
            self._attr_native_value = None
            return

        parsed: datetime | None = dt_util.parse_datetime(ts)
        if parsed is None:
            self._attr_available = False
            self._attr_native_value = None
            return

        self._attr_available = True
        self._attr_native_value = parsed

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        baseline = self._tracker.baseline or {}
        return {
            "mileage_km": baseline.get(BASELINE_MILEAGE_KM),
            "soc_percent": baseline.get(BASELINE_SOC_PERCENT),
        }


class MeasuredEfficiencySensor(_TrackerLinkedMixin, MySkodaDerivedSensor):
    """Implied efficiency from real driving since the last charge end.

    Like EfficiencySensor, instantiated four times per config entry:
    {factory, actual} capacity × {kWh/100 km, km/kWh}.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        entry: ConfigEntry,
        tracker: ChargeTracker,
        soc_entity: str,
        mileage_entity: str,
        capacity_kwh: float,
        capacity_variant: str,
        unit_variant: str,
    ) -> None:
        super().__init__(entry, [soc_entity, mileage_entity])
        self._tracker = tracker
        self._soc_entity = soc_entity
        self._mileage_entity = mileage_entity
        self._capacity_kwh = capacity_kwh
        self._capacity_variant = capacity_variant
        self._unit_variant = unit_variant

        unit_label, icon, precision = _unit_variant_props(unit_variant)
        self._attr_native_unit_of_measurement = unit_label
        self._attr_icon = icon
        self._attr_suggested_display_precision = precision

        self._attr_unique_id = (
            f"{entry.entry_id}_measured_efficiency_"
            f"{capacity_variant}_{unit_variant}"
        )
        self._attr_translation_key = (
            f"measured_efficiency_{capacity_variant}_{unit_variant}"
        )
        self._attr_name = (
            f"Measured efficiency ({capacity_variant} capacity, "
            f"{_human_unit(unit_variant)})"
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._subscribe_baseline_updates()

    @callback
    def _recalculate(self) -> None:
        baseline = self._tracker.baseline
        if baseline is None:
            self._attr_available = False
            self._attr_native_value = None
            return

        baseline_mileage = baseline.get(BASELINE_MILEAGE_KM)
        baseline_soc = baseline.get(BASELINE_SOC_PERCENT)
        current_mileage = read_distance_km(self.hass, self._mileage_entity)
        current_soc = read_float(self.hass, self._soc_entity)

        if (
            baseline_mileage is None
            or baseline_soc is None
            or current_mileage is None
            or current_soc is None
        ):
            self._attr_available = False
            self._attr_native_value = None
            return

        distance_km = current_mileage - baseline_mileage
        soc_consumed = baseline_soc - current_soc

        value = _efficiency_value(
            capacity_kwh=self._capacity_kwh,
            soc_percent=soc_consumed,
            distance_km=distance_km,
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
        baseline = self._tracker.baseline or {}
        current_mileage = read_distance_km(self.hass, self._mileage_entity)
        current_soc = read_float(self.hass, self._soc_entity)

        attrs: dict[str, Any] = {
            "capacity_variant": self._capacity_variant,
            "unit_variant": self._unit_variant,
            "capacity_kwh": self._capacity_kwh,
            "baseline_mileage_km": baseline.get(BASELINE_MILEAGE_KM),
            "baseline_soc_percent": baseline.get(BASELINE_SOC_PERCENT),
            "baseline_timestamp": baseline.get(BASELINE_TIMESTAMP),
        }
        if (
            baseline.get(BASELINE_MILEAGE_KM) is not None
            and current_mileage is not None
        ):
            attrs["distance_since_last_charge_km"] = round(
                current_mileage - baseline[BASELINE_MILEAGE_KM], 1
            )
        if (
            baseline.get(BASELINE_SOC_PERCENT) is not None
            and current_soc is not None
        ):
            attrs["soc_consumed_since_last_charge_percent"] = round(
                baseline[BASELINE_SOC_PERCENT] - current_soc, 1
            )
        return attrs
