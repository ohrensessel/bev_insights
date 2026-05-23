"""Distance-driven and days-to-low-SoC projection sensors.

- DistanceRolling7DaysSensor: km driven in the trailing 7 days
- DistanceThisWeekSensor: km driven since local Monday 00:00
- DaysToLowSocSensor: estimated days until SoC hits the configured floor
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfLength, UnitOfTime
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_track_time_change
from homeassistant.util import dt as dt_util

from custom_components.bev_insights.const import (
    CONF_LOW_SOC_THRESHOLD_PERCENT,
    LOW_SOC_THRESHOLD_PERCENT,
    signal_mileage_history_updated,
    signal_soc_history_updated,
)
from custom_components.bev_insights.tracker import MileageHistory, SocHistory
from custom_components.bev_insights.util import read_distance_km, read_float

from .base import BevDerivedSensor, _TrackerLinkedMixin
from .formulas import _local_week_start


class DistanceRolling7DaysSensor(_TrackerLinkedMixin, BevDerivedSensor):
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


class DistanceThisWeekSensor(BevDerivedSensor):
    """Kilometres driven since local Monday 00:00 (calendar week).

    Resets to 0 every Monday at midnight in the HA-configured timezone.
    Declared `TOTAL` with `_attr_last_reset` set to the current week's
    start on each recalc, so HA's Long-Term Statistics produces a clean
    per-week distance curve.
    """

    _attr_device_class = SensorDeviceClass.DISTANCE
    _attr_state_class = SensorStateClass.TOTAL
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
        self._attr_last_reset = week_start
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


class DaysToLowSocSensor(BevDerivedSensor):
    """Estimated days until SoC drops to the configured low-SoC threshold.

        daily_avg_soc_pct = soc_consumed_past_7_days / 7
        days_remaining    = (current_soc - low_threshold) / daily_avg_soc_pct

    Uses the rolling-7-day average consumption rate as the projection basis.
    Unavailable when current SoC is at or below the threshold, when there is
    no consumption history, or when the 7-day average is zero.
    """

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTime.DAYS
    _attr_icon = "mdi:battery-clock"
    _attr_suggested_display_precision = 1
    _attr_translation_key = "days_to_low_soc"

    def __init__(
        self,
        entry: ConfigEntry,
        soc_history: SocHistory,
        soc_entity: str,
    ) -> None:
        super().__init__(entry, source_entities=[soc_entity])
        self._soc_history = soc_history
        self._soc_entity = soc_entity
        self._threshold = float(
            entry.options.get(CONF_LOW_SOC_THRESHOLD_PERCENT, LOW_SOC_THRESHOLD_PERCENT)
        )
        self._attr_unique_id = f"{entry.entry_id}_days_to_low_soc"
        self._attr_name = "Days to low SoC"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

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
                signal_soc_history_updated(self._entry.entry_id),
                _on_history_update,
            )
        )
        self.async_on_remove(
            async_track_time_change(self.hass, _on_tick, minute=0, second=0)
        )

    @callback
    def _recalculate(self) -> None:
        current_soc = read_float(self.hass, self._soc_entity)
        if current_soc is None:
            self._attr_available = False
            self._attr_native_value = None
            return
        cutoff = dt_util.utcnow() - timedelta(days=7)
        consumed_7d = self._soc_history.consumed_since(cutoff)
        if consumed_7d is None or consumed_7d <= 0:
            self._attr_available = False
            self._attr_native_value = None
            return
        usable_soc = current_soc - self._threshold
        if usable_soc <= 0:
            self._attr_available = False
            self._attr_native_value = None
            return
        self._attr_available = True
        self._attr_native_value = round(usable_soc / (consumed_7d / 7.0), 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        cutoff = dt_util.utcnow() - timedelta(days=7)
        consumed_7d = self._soc_history.consumed_since(cutoff)
        return {
            "current_soc_percent": read_float(self.hass, self._soc_entity),
            "low_soc_threshold_percent": self._threshold,
            "daily_avg_soc_consumed_percent": (
                round(consumed_7d / 7.0, 2) if consumed_7d is not None else None
            ),
            "window_days": 7,
        }
