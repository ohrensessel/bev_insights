"""Sensors that recompute on ChargeTracker baseline updates.

All listen to `signal_baseline_updated` via `_TrackerLinkedMixin`:

- MeasuredFullRangeSensor: range_at_100% measured from real driving
- LastChargedSensor: timestamp of the most recent charge end
- TimeSinceLastChargeSensor: hours since the most recent charge end
- SessionLogSensor: count + attribute list of completed sessions
- LastChargeAddedSensor: kWh delivered in the last session (× capacity)
- AverageChargingPowerSensor: average power of the last session
- MeasuredEfficiencySensor: efficiency from real driving (× capacity × unit)
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EntityCategory,
    UnitOfEnergy,
    UnitOfLength,
    UnitOfPower,
    UnitOfTime,
)
from homeassistant.core import callback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.util import dt as dt_util

from custom_components.bev_insights.capacity import CapacitySource
from custom_components.bev_insights.const import (
    BASELINE_MILEAGE_KM,
    BASELINE_SOC_PERCENT,
    BASELINE_TIMESTAMP,
    CONF_MIN_MEASURED_RANGE_KM,
    CONF_MIN_MEASURED_RANGE_SOC_PERCENT,
    MIN_MEASURED_RANGE_KM,
    MIN_MEASURED_RANGE_SOC_PERCENT,
    SESSION_END_SOC_PERCENT,
    SESSION_END_TIMESTAMP,
    SESSION_LOG_MAX,
    SESSION_START_SOC_PERCENT,
    SESSION_START_TIMESTAMP,
)
from custom_components.bev_insights.tracker import ChargeTracker
from custom_components.bev_insights.util import read_distance_km, read_float

from .base import BevDerivedSensor, _TrackerLinkedMixin
from .formulas import (
    _efficiency_value,
    _human_unit,
    _post_charge_window,
    _unit_variant_props,
)


class MeasuredFullRangeSensor(_TrackerLinkedMixin, BevDerivedSensor):
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
        self._min_distance_km = float(
            entry.options.get(
                CONF_MIN_MEASURED_RANGE_KM, MIN_MEASURED_RANGE_KM
            )
        )
        self._min_soc_percent = float(
            entry.options.get(
                CONF_MIN_MEASURED_RANGE_SOC_PERCENT,
                MIN_MEASURED_RANGE_SOC_PERCENT,
            )
        )
        self._attr_unique_id = f"{entry.entry_id}_measured_full_range"
        self._attr_name = "Measured full range"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._subscribe_baseline_updates()

    @callback
    def _recalculate(self) -> None:
        window = _post_charge_window(
            self._tracker,
            self.hass,
            self._mileage_entity,
            self._soc_entity,
            self._min_distance_km,
            self._min_soc_percent,
        )
        if window is None:
            self._attr_available = False
            self._attr_native_value = None
            return
        distance_km, soc_consumed = window
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


class LastChargedSensor(_TrackerLinkedMixin, BevDerivedSensor):
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


class TimeSinceLastChargeSensor(_TrackerLinkedMixin, BevDerivedSensor):
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


class SessionLogSensor(_TrackerLinkedMixin, BevDerivedSensor):
    """Diagnostic log of completed charging sessions.

    State = number of sessions retained (up to SESSION_LOG_MAX).
    Attributes contain the full list (newest first) with start/end SoC
    and timestamps so users can audit charging history without leaving HA.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:history"
    _attr_translation_key = "session_log"

    def __init__(self, entry: ConfigEntry, tracker: ChargeTracker) -> None:
        super().__init__(entry, source_entities=[])
        self._tracker = tracker
        self._attr_unique_id = f"{entry.entry_id}_session_log"
        self._attr_name = "Session log"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._subscribe_baseline_updates()

    @callback
    def _recalculate(self) -> None:
        self._attr_available = True
        self._attr_native_value = len(self._tracker.session_log)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "sessions": list(reversed(self._tracker.session_log)),
            "max_sessions": SESSION_LOG_MAX,
        }


class LastChargeAddedSensor(_TrackerLinkedMixin, BevDerivedSensor):
    """Energy added during the most recently completed charge session.

        kWh_added = capacity * (end_soc - start_soc) / 100

    Available once a full off→on→off cycle has been observed. Negative
    deltas (battery somehow dropping during charge — API quirks) clamp to 0.
    Instantiated once per capacity variant.

    Declared `TOTAL` with `_attr_last_reset` set to the session's start
    timestamp, so HA's Long-Term Statistics interprets the value as
    "energy delivered since session start" — exactly what it is.
    MEASUREMENT alongside `device_class=ENERGY` is rejected by HA.
    """

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL
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

        # last_reset advances to the start of each new session, so LTS
        # treats each session as its own accumulation window.
        start_ts = dt_util.parse_datetime(
            session.get(SESSION_START_TIMESTAMP) or ""
        )
        if start_ts is not None:
            self._attr_last_reset = start_ts

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


class AverageChargingPowerSensor(_TrackerLinkedMixin, BevDerivedSensor):
    """Average power of the most recently completed charging session.

        avg_kW = kWh_added / duration_hours
               = capacity * (end_soc - start_soc) / 100 / duration_hours

    Reflects the average — not instantaneous — power across the entire
    last session, so it lumps the high-power ramp-up, the steady plateau,
    and the tapered top-off into one figure. Useful for spotting whether
    a session ran on AC (~3-11 kW) vs. DC fast charging (50+ kW), or for
    flagging a charger that's throttling.

    Instantiated once per capacity variant — same pattern as
    `LastChargeAddedSensor`.
    """

    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_icon = "mdi:ev-station"
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
            f"{entry.entry_id}_avg_charging_power_{capacity_variant}"
        )
        self._attr_translation_key = f"avg_charging_power_{capacity_variant}"
        self._attr_name = (
            f"Average charging power ({capacity_variant} capacity)"
        )

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
        start_ts = dt_util.parse_datetime(
            session.get(SESSION_START_TIMESTAMP) or ""
        )
        end_ts = dt_util.parse_datetime(
            session.get(SESSION_END_TIMESTAMP) or ""
        )
        if (
            start_soc is None
            or end_soc is None
            or start_ts is None
            or end_ts is None
        ):
            self._attr_available = False
            self._attr_native_value = None
            return

        duration_hours = (end_ts - start_ts).total_seconds() / 3600.0
        soc_delta = max(0.0, end_soc - start_soc)
        # A "session" with no duration or no SoC gain isn't a real charging
        # event for the purposes of this sensor (could be a momentary plug
        # cycle, or a glitch in the source entity).
        if duration_hours <= 0 or soc_delta <= 0:
            self._attr_available = False
            self._attr_native_value = None
            return

        kwh_added = capacity_kwh * soc_delta / 100.0
        self._attr_available = True
        self._attr_native_value = round(kwh_added / duration_hours, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        session = self._tracker.last_session or {}
        start_ts = dt_util.parse_datetime(
            session.get(SESSION_START_TIMESTAMP) or ""
        )
        end_ts = dt_util.parse_datetime(
            session.get(SESSION_END_TIMESTAMP) or ""
        )
        duration_hours: float | None = None
        if start_ts is not None and end_ts is not None:
            duration_hours = round(
                (end_ts - start_ts).total_seconds() / 3600.0, 3
            )
        return {
            "capacity_variant": self._capacity_variant,
            "capacity_kwh": self._capacity.current(),
            "capacity_source": self._capacity.describe(),
            "start_soc_percent": session.get(SESSION_START_SOC_PERCENT),
            "end_soc_percent": session.get(SESSION_END_SOC_PERCENT),
            "start_timestamp": session.get(SESSION_START_TIMESTAMP),
            "end_timestamp": session.get(SESSION_END_TIMESTAMP),
            "duration_hours": duration_hours,
        }


class MeasuredEfficiencySensor(_TrackerLinkedMixin, BevDerivedSensor):
    """Implied efficiency from real driving since the last charge end.

    Uses the same `_efficiency_value` math as `EfficiencySensor`, but
    sourced from the tracker baseline:
        soc_consumed = baseline_soc - current_soc        [%]
        distance     = current_mileage - baseline_mileage [km]

    Like the car-prediction efficiency, instantiated four times per
    config entry: {factory, actual} capacity × {kWh/100 km, km/kWh}.

    Shares the same suppression rules as `MeasuredFullRangeSensor`:
    unavailable while charging, and unavailable until the post-charge
    drive crosses both the distance and SoC-consumed floors.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        entry: ConfigEntry,
        tracker: ChargeTracker,
        soc_entity: str,
        mileage_entity: str,
        charging_entity: str,
        capacity: CapacitySource,
        capacity_variant: str,
        unit_variant: str,
    ) -> None:
        sources = [soc_entity, mileage_entity, charging_entity]
        if capacity.source_entity:
            sources.append(capacity.source_entity)
        super().__init__(entry, sources)
        self._tracker = tracker
        self._soc_entity = soc_entity
        self._mileage_entity = mileage_entity
        self._capacity = capacity
        self._capacity_variant = capacity_variant
        self._unit_variant = unit_variant
        self._min_distance_km = float(
            entry.options.get(
                CONF_MIN_MEASURED_RANGE_KM, MIN_MEASURED_RANGE_KM
            )
        )
        self._min_soc_percent = float(
            entry.options.get(
                CONF_MIN_MEASURED_RANGE_SOC_PERCENT,
                MIN_MEASURED_RANGE_SOC_PERCENT,
            )
        )

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
        capacity_kwh = self._capacity.current()
        window = _post_charge_window(
            self._tracker,
            self.hass,
            self._mileage_entity,
            self._soc_entity,
            self._min_distance_km,
            self._min_soc_percent,
        )
        if capacity_kwh is None or window is None:
            self._attr_available = False
            self._attr_native_value = None
            return
        distance_km, soc_consumed = window

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
