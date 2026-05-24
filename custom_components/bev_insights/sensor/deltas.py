"""Week-over-week comparison sensors.

Both compare *this week so far* against *last week up to the same elapsed
time* — so the value is meaningful from Monday morning onward, not just
once last week's total is locked in.

    this_week_so_far    = value in [this_week_start, now]
    last_week_same_pt   = value in [this_week_start - 7d, now - 7d]
    delta               = this_week_so_far - last_week_same_pt

Positive deltas mean "more than last week at the same point", negative
means "less". The shape is a single chip suitable for dashboards.

Both sensors need ≥ 7 days + elapsed-into-this-week of history (worst
case ~14 days on a Sunday evening). The default history retention was
bumped to 15 days in v1.6 to cover that worst case; users with a
shorter `history_days` option see `partial_window_data: true` in
attributes when last week's start sample is outside the deque.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfLength
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_track_time_change
from homeassistant.util import dt as dt_util

from custom_components.bev_insights.capacity import CapacitySource
from custom_components.bev_insights.const import signal_soc_history_updated
from custom_components.bev_insights.tracker import MileageHistory, SocHistory
from custom_components.bev_insights.util import read_distance_km

from .base import BevDerivedSensor
from .formulas import _local_week_start


def _week_windows(
    hass: Any, now_utc: datetime
) -> tuple[datetime, datetime, datetime]:
    """Return (this_week_start, last_week_start, last_week_equivalent_end).

    `last_week_equivalent_end = now_utc - 7d` — the point in last week
    that matches the current elapsed time into this week.
    """
    this_week_start = _local_week_start(now_utc, hass)
    last_week_start = this_week_start - timedelta(days=7)
    last_week_end = now_utc - timedelta(days=7)
    return this_week_start, last_week_start, last_week_end


class DistanceWeekDeltaSensor(BevDerivedSensor):
    """Δkm = this week so far − last week up to same elapsed time.

    Listens to the mileage entity directly so the chip updates as soon
    as the odometer ticks, plus an hourly time tick so the windows roll
    forward even when nothing else changes.
    """

    _attr_device_class = SensorDeviceClass.DISTANCE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfLength.KILOMETERS
    _attr_icon = "mdi:trending-up"
    _attr_suggested_display_precision = 1
    _attr_translation_key = "distance_week_delta"

    def __init__(
        self,
        entry: ConfigEntry,
        mileage_history: MileageHistory,
        mileage_entity: str,
    ) -> None:
        # Listen to the odometer directly: the dispatcher only fires
        # when the deque updates, which skips no-op state changes.
        super().__init__(entry, source_entities=[mileage_entity])
        self._mileage_history = mileage_history
        self._mileage_entity = mileage_entity
        self._attr_unique_id = f"{entry.entry_id}_distance_week_delta"
        self._attr_name = "Distance driven (vs. last week)"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        @callback
        def _on_tick(_now: datetime) -> None:
            self._recalculate()
            self.async_write_ha_state()

        # Hourly so the comparison rolls forward even with the car idle.
        self.async_on_remove(
            async_track_time_change(self.hass, _on_tick, minute=0, second=0)
        )

    @callback
    def _recalculate(self) -> None:
        now = dt_util.utcnow()
        this_week_start, last_week_start, last_week_end = _week_windows(
            self.hass, now
        )
        current = read_distance_km(self.hass, self._mileage_entity)
        # this_week_so_far = current - mileage_at(this_week_start).
        # Use distance_between to get clamping + None-handling for free.
        this_week_so_far = self._mileage_history.distance_between(
            this_week_start, now
        )
        # Reuse distance_between against now for the latest value because
        # the deque's `latest_sample` might lag a live state change by a
        # few seconds; preferring the live state keeps the chip snappy
        # when the odometer just ticked.
        if (
            current is not None
            and this_week_so_far is not None
        ):
            anchor = self._mileage_history.value_at(this_week_start)
            if anchor is not None:
                this_week_so_far = max(0.0, current - anchor)
        last_week_same_pt = self._mileage_history.distance_between(
            last_week_start, last_week_end
        )
        if this_week_so_far is None or last_week_same_pt is None:
            self._attr_available = False
            self._attr_native_value = None
            return
        self._attr_available = True
        self._attr_native_value = round(this_week_so_far - last_week_same_pt, 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        now = dt_util.utcnow()
        this_week_start, last_week_start, last_week_end = _week_windows(
            self.hass, now
        )
        attrs: dict[str, Any] = {
            "this_week_start": this_week_start.isoformat(),
            "last_week_start": last_week_start.isoformat(),
            "last_week_equivalent_end": last_week_end.isoformat(),
            "partial_window_data": not self._mileage_history.has_pre_window_sample(
                last_week_start
            ),
        }
        this = self._mileage_history.distance_between(this_week_start, now)
        last = self._mileage_history.distance_between(last_week_start, last_week_end)
        attrs["this_week_distance_km"] = (
            round(this, 1) if this is not None else None
        )
        attrs["last_week_distance_km"] = (
            round(last, 1) if last is not None else None
        )
        return attrs


class EnergyConsumedWeekDeltaSensor(BevDerivedSensor):
    """ΔkWh = this week so far − last week up to same elapsed time.

    Per capacity variant (factory / actual), same as the existing
    EnergyConsumedWindowSensor. No `device_class=ENERGY` because HA only
    accepts TOTAL / TOTAL_INCREASING for that class and the delta can
    be negative — `state_class=MEASUREMENT` with `kWh` as the unit keeps
    LTS happy (min/max/mean curves) without lying to the Energy Dashboard.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_icon = "mdi:trending-up"
    _attr_suggested_display_precision = 2

    def __init__(
        self,
        entry: ConfigEntry,
        soc_history: SocHistory,
        capacity: CapacitySource,
        capacity_variant: str,
    ) -> None:
        sources = [capacity.source_entity] if capacity.source_entity else []
        super().__init__(entry, sources)
        self._soc_history = soc_history
        self._capacity = capacity
        self._capacity_variant = capacity_variant
        self._attr_unique_id = (
            f"{entry.entry_id}_energy_consumed_week_delta_{capacity_variant}"
        )
        self._attr_translation_key = (
            f"energy_consumed_week_delta_{capacity_variant}"
        )
        self._attr_name = (
            f"Energy consumed (vs. last week, {capacity_variant} capacity)"
        )

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

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_soc_history_updated(self._entry.entry_id),
                _tick_dispatcher,
            )
        )
        self.async_on_remove(
            async_track_time_change(self.hass, _tick_time, minute=0, second=0)
        )

    def _consumed_kwh(self, start: datetime, end: datetime) -> float | None:
        pct = self._soc_history.consumed_between(start, end)
        capacity = self._capacity.current()
        if pct is None or capacity is None:
            return None
        return capacity * pct / 100.0

    @callback
    def _recalculate(self) -> None:
        now = dt_util.utcnow()
        this_week_start, last_week_start, last_week_end = _week_windows(
            self.hass, now
        )
        this_week = self._consumed_kwh(this_week_start, now)
        last_week = self._consumed_kwh(last_week_start, last_week_end)
        if this_week is None or last_week is None:
            self._attr_available = False
            self._attr_native_value = None
            return
        self._attr_available = True
        self._attr_native_value = round(this_week - last_week, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        now = dt_util.utcnow()
        this_week_start, last_week_start, last_week_end = _week_windows(
            self.hass, now
        )
        this_pct = self._soc_history.consumed_between(this_week_start, now)
        last_pct = self._soc_history.consumed_between(last_week_start, last_week_end)
        return {
            "this_week_start": this_week_start.isoformat(),
            "last_week_start": last_week_start.isoformat(),
            "last_week_equivalent_end": last_week_end.isoformat(),
            "partial_window_data": not self._soc_history.has_pre_window_sample(
                last_week_start
            ),
            "capacity_variant": self._capacity_variant,
            "capacity_kwh": self._capacity.current(),
            "capacity_source": self._capacity.describe(),
            "this_week_soc_consumed_percent": (
                round(this_pct, 2) if this_pct is not None else None
            ),
            "last_week_soc_consumed_percent": (
                round(last_pct, 2) if last_pct is not None else None
            ),
        }


__all__ = ["DistanceWeekDeltaSensor", "EnergyConsumedWeekDeltaSensor"]
