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

from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfEnergy,
    UnitOfLength,
    UnitOfTime,
)
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
)
from homeassistant.util import dt as dt_util

from .capacity import CapacitySource
from .const import (
    BASELINE_MILEAGE_KM,
    BASELINE_SOC_PERCENT,
    BASELINE_TIMESTAMP,
    CONF_CHARGING_SENSOR,
    CONF_MILEAGE_SENSOR,
    CONF_RANGE_SENSOR,
    CONF_SOC_SENSOR,
    DOMAIN,
    MIN_MEASURED_RANGE_KM,
    MIN_MEASURED_RANGE_SOC_PERCENT,
    SESSION_END_SOC_PERCENT,
    SESSION_END_TIMESTAMP,
    SESSION_START_SOC_PERCENT,
    SESSION_START_TIMESTAMP,
    UNIT_KM_PER_KWH,
    UNIT_KWH_PER_100KM,
    UNIT_VARIANT_KM_PER_KWH,
    UNIT_VARIANT_KWH_PER_100KM,
    VARIANT_ACTUAL,
    VARIANT_FACTORY,
    signal_baseline_updated,
    signal_mileage_history_updated,
    signal_soc_history_updated,
)
from .tracker import ChargeTracker, MileageHistory, SocHistory
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
    mileage_history: MileageHistory | None = domain_data.get("mileage_history")
    soc_history: SocHistory | None = domain_data.get("soc_history")
    capacity_factory: CapacitySource = domain_data["capacity_factory"]
    capacity_actual: CapacitySource = domain_data["capacity_actual"]

    soc_entity: str = data[CONF_SOC_SENSOR]
    range_entity: str = data[CONF_RANGE_SENSOR]

    entities: list[SensorEntity] = [
        FullBatteryRangeSensor(entry, soc_entity, range_entity),
        StateOfHealthSensor(entry, capacity_factory, capacity_actual),
    ]

    # Efficiency: 2 capacities × 2 units = 4 sensors
    for capacity, capacity_variant in (
        (capacity_factory, VARIANT_FACTORY),
        (capacity_actual, VARIANT_ACTUAL),
    ):
        for unit_variant in (
            UNIT_VARIANT_KWH_PER_100KM,
            UNIT_VARIANT_KM_PER_KWH,
        ):
            entities.append(
                EfficiencySensor(
                    entry,
                    soc_entity,
                    range_entity,
                    capacity=capacity,
                    capacity_variant=capacity_variant,
                    unit_variant=unit_variant,
                )
            )

    # Tracker-dependent sensors only if the user wired up the prerequisites.
    if tracker is not None:
        mileage_entity: str = data[CONF_MILEAGE_SENSOR]
        charging_entity: str = data[CONF_CHARGING_SENSOR]
        entities.append(
            MeasuredFullRangeSensor(
                entry, tracker, soc_entity, mileage_entity, charging_entity
            )
        )
        entities.append(LastChargedSensor(entry, tracker))
        entities.append(TimeSinceLastChargeSensor(entry, tracker))

        # Last charge added (kWh): one per capacity variant.
        for capacity, capacity_variant in (
            (capacity_factory, VARIANT_FACTORY),
            (capacity_actual, VARIANT_ACTUAL),
        ):
            entities.append(
                LastChargeAddedSensor(
                    entry,
                    tracker,
                    capacity=capacity,
                    capacity_variant=capacity_variant,
                )
            )

        # Measured efficiency: 2 capacities × 2 units = 4 sensors
        for capacity, capacity_variant in (
            (capacity_factory, VARIANT_FACTORY),
            (capacity_actual, VARIANT_ACTUAL),
        ):
            for unit_variant in (
                UNIT_VARIANT_KWH_PER_100KM,
                UNIT_VARIANT_KM_PER_KWH,
            ):
                entities.append(
                    MeasuredEfficiencySensor(
                        entry,
                        tracker,
                        soc_entity,
                        mileage_entity,
                        capacity=capacity,
                        capacity_variant=capacity_variant,
                        unit_variant=unit_variant,
                    )
                )

    # Mileage-history sensors only require the odometer; they work even
    # if no charging sensor was configured.
    if mileage_history is not None:
        entities.append(
            DistanceRolling7DaysSensor(entry, mileage_history)
        )
        entities.append(
            DistanceThisWeekSensor(
                entry, mileage_history, data[CONF_MILEAGE_SENSOR]
            )
        )

    # Two windowed shapes: rolling 7 days, calendar week. Used for both
    # kWh consumed and weekly average efficiency. `kind` differentiates
    # the cutoff calculation in the sensor.
    WINDOWS = (
        ("rolling_7_days", "Rolling 7 days"),
        ("this_week", "This week"),
    )

    # kWh consumed per window — needs SoC history only.
    if soc_history is not None:
        for window_key, window_label in WINDOWS:
            for capacity, capacity_variant in (
                (capacity_factory, VARIANT_FACTORY),
                (capacity_actual, VARIANT_ACTUAL),
            ):
                entities.append(
                    EnergyConsumedWindowSensor(
                        entry,
                        soc_history,
                        capacity=capacity,
                        capacity_variant=capacity_variant,
                        window_key=window_key,
                        window_label=window_label,
                    )
                )

    # Weekly average efficiency — needs both histories together.
    if soc_history is not None and mileage_history is not None:
        for window_key, window_label in WINDOWS:
            for capacity, capacity_variant in (
                (capacity_factory, VARIANT_FACTORY),
                (capacity_actual, VARIANT_ACTUAL),
            ):
                for unit_variant in (
                    UNIT_VARIANT_KWH_PER_100KM,
                    UNIT_VARIANT_KM_PER_KWH,
                ):
                    entities.append(
                        AverageEfficiencyWindowSensor(
                            entry,
                            soc_history,
                            mileage_history,
                            capacity=capacity,
                            capacity_variant=capacity_variant,
                            unit_variant=unit_variant,
                            window_key=window_key,
                            window_label=window_label,
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

        # Compute an initial value as soon as we're added to hass.
        self._recalculate()

    @callback
    def _recalculate(self) -> None:
        """Override in subclasses to update self._attr_native_value."""
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Capacity-independent sensors                                                #
# --------------------------------------------------------------------------- #


class FullBatteryRangeSensor(MySkodaDerivedSensor):
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


class StateOfHealthSensor(MySkodaDerivedSensor):
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


# --------------------------------------------------------------------------- #
# Capacity-dependent sensors                                                  #
# --------------------------------------------------------------------------- #


class EfficiencySensor(MySkodaDerivedSensor):
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


# Shared helpers for the four efficiency sensors and their measured twins.

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
    """Compute one efficiency figure or return None for invalid inputs.

    `soc_percent` and `distance_km` describe a *consumption sample*:
      - For the car-prediction variant: current SoC and current range.
      - For the measured variant: SoC consumed and distance driven
        since the last charge end.

    The math is identical in both cases, which is why this helper is shared.
    """
    if (
        soc_percent is None
        or distance_km is None
        or soc_percent <= 0
        or distance_km <= 0
        or capacity_kwh <= 0
    ):
        return None

    soc_percent = min(soc_percent, 100.0)
    # Energy represented by `soc_percent` of the configured capacity.
    energy_kwh = capacity_kwh * soc_percent / 100.0

    if unit_variant == UNIT_VARIANT_KM_PER_KWH:
        return round(distance_km / energy_kwh, 3)
    # Default: kWh per 100 km
    return round(energy_kwh / distance_km * 100.0, 2)


# --------------------------------------------------------------------------- #
# Tracker-dependent sensors                                                   #
# --------------------------------------------------------------------------- #


class _TrackerLinkedMixin:
    """Adds a subscription to baseline-updated dispatcher signals.

    Mixin used by sensors that need to recompute when the ChargeTracker
    writes a new baseline. Expects the host class to define `self._entry`
    and the standard HA entity API (`hass`, `async_on_remove`,
    `_recalculate`, `async_write_ha_state`).
    """

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
    """Range at 100% SoC, measured from actual driving since last charge.

        distance_since_charge = current_mileage_km - baseline_mileage_km
        soc_consumed          = baseline_soc_percent - current_soc_percent
        measured_full_range   = distance_since_charge / soc_consumed * 100

    Reflects real-world consumption rather than the car's range prediction.

    Unavailable when:
      - no charging session has ended yet (no baseline),
      - the vehicle is currently charging (SoC is rising back toward the
        baseline, which makes `soc_consumed` shrink and the calculated
        range explode toward infinity), or
      - the post-charge drive hasn't produced enough data yet:
        less than `MIN_MEASURED_RANGE_KM` driven or
        less than `MIN_MEASURED_RANGE_SOC_PERCENT` consumed.
    """

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
        charging_entity: str,
    ) -> None:
        # Listen to the charging entity too so the sensor flips to/from
        # unavailable at the instant charging starts or ends, rather than
        # only when the next SoC tick lands.
        super().__init__(entry, [soc_entity, mileage_entity, charging_entity])
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
        # Suppress during charging: SoC is rising back toward baseline,
        # which makes the ratio diverge and produces nonsense values.
        if self._tracker.is_charging:
            self._attr_available = False
            self._attr_native_value = None
            return

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

        # Below these floors, SoC quantization (typically 1% steps) makes
        # the ratio too noisy to be meaningful.
        if (
            distance_km < MIN_MEASURED_RANGE_KM
            or soc_consumed < MIN_MEASURED_RANGE_SOC_PERCENT
        ):
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
        # No live HA sources to track; updates come exclusively via the
        # baseline dispatcher.
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


class TimeSinceLastChargeSensor(_TrackerLinkedMixin, MySkodaDerivedSensor):
    """Hours elapsed since the most recent charge end.

    Ticks once an hour so dashboards/automations don't need a template
    sensor to compute "days since charge". Resets toward zero whenever
    a new charge ends and the baseline updates.
    """

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTime.HOURS
    _attr_icon = "mdi:timer-sand"
    _attr_suggested_display_precision = 1
    _attr_translation_key = "time_since_last_charge"

    def __init__(self, entry: ConfigEntry, tracker: ChargeTracker) -> None:
        super().__init__(entry, source_entities=[])
        self._tracker = tracker
        self._attr_unique_id = f"{entry.entry_id}_time_since_last_charge"
        self._attr_name = "Time since last charge"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._subscribe_baseline_updates()

        @callback
        def _tick(_now: datetime) -> None:
            self._recalculate()
            self.async_write_ha_state()

        # Hourly tick so the value advances even when nothing else changes.
        self.async_on_remove(
            async_track_time_change(self.hass, _tick, minute=0, second=0)
        )

    @callback
    def _recalculate(self) -> None:
        baseline = self._tracker.baseline
        if baseline is None:
            self._attr_available = False
            self._attr_native_value = None
            return
        ts_str = baseline.get(BASELINE_TIMESTAMP)
        if not ts_str:
            self._attr_available = False
            self._attr_native_value = None
            return
        ts = dt_util.parse_datetime(ts_str)
        if ts is None:
            self._attr_available = False
            self._attr_native_value = None
            return
        elapsed_hours = (dt_util.utcnow() - ts).total_seconds() / 3600.0
        self._attr_available = True
        self._attr_native_value = round(max(0.0, elapsed_hours), 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        baseline = self._tracker.baseline or {}
        return {
            "last_charge_timestamp": baseline.get(BASELINE_TIMESTAMP),
        }


class LastChargeAddedSensor(_TrackerLinkedMixin, MySkodaDerivedSensor):
    """Energy added during the most recently completed charge session.

        kWh_added = capacity * (end_soc - start_soc) / 100

    Available once a full off→on→off cycle has been observed. Negative
    deltas (battery somehow dropping during charge — API quirks) clamp to 0.
    Instantiated once per capacity variant.
    """

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_icon = "mdi:battery-plus-variant"
    _attr_suggested_display_precision = 2

    def __init__(
        self,
        entry: ConfigEntry,
        tracker: ChargeTracker,
        capacity: CapacitySource,
        capacity_variant: str,
    ) -> None:
        sources = []
        if capacity.source_entity:
            sources.append(capacity.source_entity)
        super().__init__(entry, sources)
        self._tracker = tracker
        self._capacity = capacity
        self._capacity_variant = capacity_variant
        self._attr_unique_id = (
            f"{entry.entry_id}_last_charge_added_{capacity_variant}"
        )
        self._attr_translation_key = f"last_charge_added_{capacity_variant}"
        self._attr_name = f"Last charge added ({capacity_variant} capacity)"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._subscribe_baseline_updates()

    @callback
    def _recalculate(self) -> None:
        session = self._tracker.last_session
        capacity_kwh = self._capacity.current()
        if session is None or capacity_kwh is None:
            self._attr_available = False
            self._attr_native_value = None
            return

        start_soc = session.get(SESSION_START_SOC_PERCENT)
        end_soc = session.get(SESSION_END_SOC_PERCENT)
        if start_soc is None or end_soc is None:
            self._attr_available = False
            self._attr_native_value = None
            return

        soc_delta = max(0.0, end_soc - start_soc)
        self._attr_available = True
        self._attr_native_value = round(capacity_kwh * soc_delta / 100.0, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        session = self._tracker.last_session or {}
        return {
            "capacity_variant": self._capacity_variant,
            "capacity_kwh": self._capacity.current(),
            "capacity_source": self._capacity.describe(),
            "start_soc_percent": session.get(SESSION_START_SOC_PERCENT),
            "end_soc_percent": session.get(SESSION_END_SOC_PERCENT),
            "start_timestamp": session.get(SESSION_START_TIMESTAMP),
            "end_timestamp": session.get(SESSION_END_TIMESTAMP),
        }


class MeasuredEfficiencySensor(_TrackerLinkedMixin, MySkodaDerivedSensor):
    """Implied efficiency from real driving since the last charge end.

    Uses the same `_efficiency_value` math as `EfficiencySensor`, but
    sourced from the tracker baseline:
        soc_consumed = baseline_soc - current_soc        [%]
        distance     = current_mileage - baseline_mileage [km]

    Like the car-prediction efficiency, instantiated four times per
    config entry: {factory, actual} capacity × {kWh/100 km, km/kWh}.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        entry: ConfigEntry,
        tracker: ChargeTracker,
        soc_entity: str,
        mileage_entity: str,
        capacity: CapacitySource,
        capacity_variant: str,
        unit_variant: str,
    ) -> None:
        sources = [soc_entity, mileage_entity]
        if capacity.source_entity:
            sources.append(capacity.source_entity)
        super().__init__(entry, sources)
        self._tracker = tracker
        self._soc_entity = soc_entity
        self._mileage_entity = mileage_entity
        self._capacity = capacity
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
        capacity_kwh = self._capacity.current()

        if (
            baseline_mileage is None
            or baseline_soc is None
            or current_mileage is None
            or current_soc is None
            or capacity_kwh is None
        ):
            self._attr_available = False
            self._attr_native_value = None
            return

        distance_km = current_mileage - baseline_mileage
        soc_consumed = baseline_soc - current_soc

        value = _efficiency_value(
            capacity_kwh=capacity_kwh,
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
            "capacity_kwh": self._capacity.current(),
            "capacity_source": self._capacity.describe(),
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

# --------------------------------------------------------------------------- #
# Mileage-history sensors                                                     #
# --------------------------------------------------------------------------- #


def _local_week_start(now_utc: datetime, hass: HomeAssistant) -> datetime:
    """Return the local Monday 00:00 of the week containing `now_utc`, in UTC.

    Uses Home Assistant's configured time zone so the week boundary
    matches the user's locale, not server UTC.
    """
    local_tz = dt_util.get_time_zone(hass.config.time_zone) or dt_util.UTC
    local = now_utc.astimezone(local_tz)
    monday_local = local - timedelta(days=local.weekday())
    monday_local = monday_local.replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return monday_local.astimezone(dt_util.UTC)


class DistanceRolling7DaysSensor(_TrackerLinkedMixin, MySkodaDerivedSensor):
    """Kilometres driven in the trailing 7 days (rolling window).

    Always reflects "the last 168 hours of driving" rather than resetting
    on a calendar boundary. Useful for spotting trends and for budgets
    that don't align to weeks.
    """

    _attr_device_class = SensorDeviceClass.DISTANCE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfLength.KILOMETERS
    _attr_icon = "mdi:calendar-week"
    _attr_suggested_display_precision = 1
    _attr_translation_key = "distance_rolling_7_days"

    def __init__(
        self, entry: ConfigEntry, mileage_history: MileageHistory
    ) -> None:
        # No source entities — the mileage history listens for itself and
        # signals us via the dispatcher.
        super().__init__(entry, source_entities=[])
        self._mileage_history = mileage_history
        self._attr_unique_id = f"{entry.entry_id}_distance_rolling_7_days"
        self._attr_name = "Distance driven (rolling 7 days)"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        # Re-use the existing mixin pattern but with a different dispatcher
        # signal. (Slight abuse of the mixin: cheaper than duplicating the
        # connect/recompute boilerplate.)
        @callback
        def _on_history_update() -> None:
            self._recalculate()
            self.async_write_ha_state()

        @callback
        def _on_tick(_now: datetime) -> None:
            self._recalculate()
            self.async_write_ha_state()

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_mileage_history_updated(self._entry.entry_id),
                _on_history_update,
            )
        )

        # Time-based ticker so the window actually rolls even when the
        # car isn't moving. Once per hour is plenty for a 7-day window.
        self.async_on_remove(
            async_track_time_change(
                self.hass, _on_tick, minute=0, second=0
            )
        )

    @callback
    def _recalculate(self) -> None:
        cutoff = dt_util.utcnow() - timedelta(days=7)
        distance = self._mileage_history.distance_since(cutoff)
        if distance is None:
            self._attr_available = False
            self._attr_native_value = None
            return
        self._attr_available = True
        self._attr_native_value = round(distance, 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        oldest = self._mileage_history.oldest_sample
        attrs: dict[str, Any] = {
            "window": "rolling_7_days",
        }
        if oldest is not None:
            attrs["oldest_sample_timestamp"] = oldest[0].isoformat()
            attrs["oldest_sample_mileage_km"] = oldest[1]
        return attrs


class DistanceThisWeekSensor(MySkodaDerivedSensor):
    """Kilometres driven since local Monday 00:00 (calendar week).

    Resets to 0 every Monday at midnight in the HA-configured timezone.
    """

    _attr_device_class = SensorDeviceClass.DISTANCE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfLength.KILOMETERS
    _attr_icon = "mdi:calendar-today"
    _attr_suggested_display_precision = 1
    _attr_translation_key = "distance_this_week"

    def __init__(
        self,
        entry: ConfigEntry,
        mileage_history: MileageHistory,
        mileage_entity: str,
    ) -> None:
        # Listen to the odometer directly: this sensor doesn't care about
        # week-old history, only "what changed since Monday".
        super().__init__(entry, source_entities=[mileage_entity])
        self._mileage_history = mileage_history
        self._mileage_entity = mileage_entity
        self._attr_unique_id = f"{entry.entry_id}_distance_this_week"
        self._attr_name = "Distance driven (this week)"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        # Fire a recompute at every minute past midnight so the value
        # snaps to zero promptly when a new week begins.
        @callback
        def _midnight_tick(now: datetime) -> None:
            self._recalculate()
            self.async_write_ha_state()

        self.async_on_remove(
            async_track_time_change(
                self.hass, _midnight_tick, hour=0, minute=0, second=0
            )
        )

    @callback
    def _recalculate(self) -> None:
        week_start = _local_week_start(dt_util.utcnow(), self.hass)
        # Prefer the history's baseline-aware lookup. If the user installed
        # the integration midweek and the deque starts after Monday, fall
        # back to "distance since first sample" — explicitly noting that
        # in attributes so the value isn't misleading.
        distance = self._mileage_history.distance_since(week_start)
        if distance is None:
            # No pre-week sample yet — use the oldest sample we have.
            oldest = self._mileage_history.oldest_sample
            current = read_distance_km(self.hass, self._mileage_entity)
            if oldest is None or current is None:
                self._attr_available = False
                self._attr_native_value = None
                return
            distance = max(0.0, current - oldest[1])
        self._attr_available = True
        self._attr_native_value = round(distance, 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        week_start = _local_week_start(dt_util.utcnow(), self.hass)
        oldest = self._mileage_history.oldest_sample
        attrs: dict[str, Any] = {
            "window": "calendar_week",
            "week_start": week_start.isoformat(),
        }
        # Tell the user when the figure is approximate because we don't
        # yet have a pre-week sample to anchor on.
        if oldest is not None and oldest[0] > week_start:
            attrs["partial_week_data"] = True
            attrs["oldest_sample_timestamp"] = oldest[0].isoformat()
        else:
            attrs["partial_week_data"] = False
        return attrs


# --------------------------------------------------------------------------- #
# Window-based sensors (kWh consumed, average efficiency)                     #
# --------------------------------------------------------------------------- #


def _window_cutoff(
    hass: HomeAssistant, window_key: str, now_utc: datetime
) -> datetime:
    """Return the cutoff timestamp for a named window."""
    if window_key == "this_week":
        return _local_week_start(now_utc, hass)
    # Default: rolling 7 days
    return now_utc - timedelta(days=7)


class _WindowedSensor(_TrackerLinkedMixin, MySkodaDerivedSensor):
    """Common scaffolding for window-based sensors.

    Listens to both the SoC and the (optional) mileage history dispatchers,
    plus an hourly time tick so the rolling window keeps rolling and the
    calendar-week one resets cleanly at midnight. Subclasses implement
    `_recalculate()` to do the actual math.
    """

    def __init__(
        self,
        entry: ConfigEntry,
        window_key: str,
        window_label: str,
        listen_soc_history: bool,
        listen_mileage_history: bool,
        capacity_entity: str | None = None,
    ) -> None:
        # If the capacity is sourced from an entity, list it as a source
        # entity so the base class wires up a state-change listener for us.
        sources = [capacity_entity] if capacity_entity else []
        super().__init__(entry, source_entities=sources)
        self._window_key = window_key
        self._window_label = window_label
        self._listen_soc_history = listen_soc_history
        self._listen_mileage_history = listen_mileage_history

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        @callback
        def _tick_dispatcher() -> None:
            self._recalculate()
            self.async_write_ha_state()

        @callback
        def _tick_time(_now: datetime) -> None:
            self._recalculate()
            self.async_write_ha_state()

        if self._listen_soc_history:
            self.async_on_remove(
                async_dispatcher_connect(
                    self.hass,
                    signal_soc_history_updated(self._entry.entry_id),
                    _tick_dispatcher,
                )
            )
        if self._listen_mileage_history:
            self.async_on_remove(
                async_dispatcher_connect(
                    self.hass,
                    signal_mileage_history_updated(self._entry.entry_id),
                    _tick_dispatcher,
                )
            )

        # Hourly tick keeps the window "sliding" even when no source
        # entity changes; midnight covers the calendar-week reset.
        self.async_on_remove(
            async_track_time_change(self.hass, _tick_time, minute=0, second=0)
        )


class EnergyConsumedWindowSensor(_WindowedSensor):
    """kWh consumed over a window.

        kWh = capacity * soc_consumed_percent / 100

    `soc_consumed_percent` comes from SocHistory.consumed_since() and
    correctly ignores upward SoC steps (i.e. charging) within the window.
    """

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_icon = "mdi:lightning-bolt-circle"
    _attr_suggested_display_precision = 2

    def __init__(
        self,
        entry: ConfigEntry,
        soc_history: SocHistory,
        capacity: CapacitySource,
        capacity_variant: str,
        window_key: str,
        window_label: str,
    ) -> None:
        super().__init__(
            entry,
            window_key,
            window_label,
            listen_soc_history=True,
            listen_mileage_history=False,
            capacity_entity=capacity.source_entity,
        )
        self._soc_history = soc_history
        self._capacity = capacity
        self._capacity_variant = capacity_variant
        self._attr_unique_id = (
            f"{entry.entry_id}_energy_consumed_"
            f"{window_key}_{capacity_variant}"
        )
        self._attr_translation_key = (
            f"energy_consumed_{window_key}_{capacity_variant}"
        )
        self._attr_name = (
            f"Energy consumed ({window_label.lower()}, "
            f"{capacity_variant} capacity)"
        )

    @callback
    def _recalculate(self) -> None:
        cutoff = _window_cutoff(self.hass, self._window_key, dt_util.utcnow())
        consumed_pct = self._soc_history.consumed_since(cutoff)
        capacity_kwh = self._capacity.current()
        if consumed_pct is None or capacity_kwh is None:
            self._attr_available = False
            self._attr_native_value = None
            return
        self._attr_available = True
        self._attr_native_value = round(capacity_kwh * consumed_pct / 100.0, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        cutoff = _window_cutoff(self.hass, self._window_key, dt_util.utcnow())
        consumed_pct = self._soc_history.consumed_since(cutoff)
        return {
            "window": self._window_key,
            "window_start": cutoff.isoformat(),
            "partial_window_data": not self._soc_history.has_pre_window_sample(cutoff),
            "capacity_variant": self._capacity_variant,
            "capacity_kwh": self._capacity.current(),
            "capacity_source": self._capacity.describe(),
            "soc_consumed_percent": (
                round(consumed_pct, 2) if consumed_pct is not None else None
            ),
        }


class AverageEfficiencyWindowSensor(_WindowedSensor):
    """Average driving efficiency over a window.

        kWh consumed in window = capacity * soc_consumed_pct / 100
        kWh/100 km            = kWh_consumed / km_driven * 100
        km/kWh                = km_driven / kWh_consumed

    Reuses the same dual-unit logic as EfficiencySensor via `_efficiency_value`.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        entry: ConfigEntry,
        soc_history: SocHistory,
        mileage_history: MileageHistory,
        capacity: CapacitySource,
        capacity_variant: str,
        unit_variant: str,
        window_key: str,
        window_label: str,
    ) -> None:
        super().__init__(
            entry,
            window_key,
            window_label,
            listen_soc_history=True,
            listen_mileage_history=True,
            capacity_entity=capacity.source_entity,
        )
        self._soc_history = soc_history
        self._mileage_history = mileage_history
        self._capacity = capacity
        self._capacity_variant = capacity_variant
        self._unit_variant = unit_variant

        unit_label, icon, precision = _unit_variant_props(unit_variant)
        self._attr_native_unit_of_measurement = unit_label
        self._attr_icon = icon
        self._attr_suggested_display_precision = precision

        self._attr_unique_id = (
            f"{entry.entry_id}_avg_efficiency_"
            f"{window_key}_{capacity_variant}_{unit_variant}"
        )
        self._attr_translation_key = (
            f"avg_efficiency_{window_key}_{capacity_variant}_{unit_variant}"
        )
        self._attr_name = (
            f"Average efficiency ({window_label.lower()}, "
            f"{capacity_variant} capacity, {_human_unit(unit_variant)})"
        )

    @callback
    def _recalculate(self) -> None:
        cutoff = _window_cutoff(self.hass, self._window_key, dt_util.utcnow())
        consumed_pct = self._soc_history.consumed_since(cutoff)
        distance_km = self._mileage_history.distance_since(cutoff)
        capacity_kwh = self._capacity.current()

        if capacity_kwh is None:
            self._attr_available = False
            self._attr_native_value = None
            return

        value = _efficiency_value(
            capacity_kwh=capacity_kwh,
            soc_percent=consumed_pct,
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
        cutoff = _window_cutoff(self.hass, self._window_key, dt_util.utcnow())
        consumed_pct = self._soc_history.consumed_since(cutoff)
        distance_km = self._mileage_history.distance_since(cutoff)
        return {
            "window": self._window_key,
            "window_start": cutoff.isoformat(),
            "partial_window_data": (
                not self._soc_history.has_pre_window_sample(cutoff)
                or not self._mileage_history.has_pre_window_sample(cutoff)
            ),
            "capacity_variant": self._capacity_variant,
            "unit_variant": self._unit_variant,
            "capacity_kwh": self._capacity.current(),
            "capacity_source": self._capacity.describe(),
            "soc_consumed_percent": (
                round(consumed_pct, 2) if consumed_pct is not None else None
            ),
            "distance_km": (
                round(distance_km, 1) if distance_km is not None else None
            ),
        }
