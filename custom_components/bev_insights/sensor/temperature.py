"""Efficiency-vs-outside-temperature correlation sensor.

Surfaces cold-weather range loss by grouping each local day's driving
efficiency into outside-temperature bands, using the day's time-weighted
average temperature to pick the band.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_track_time_change
from homeassistant.util import dt as dt_util

from custom_components.bev_insights.capacity import CapacitySource
from custom_components.bev_insights.const import (
    TEMPERATURE_BANDS,
    UNIT_VARIANT_KM_PER_KWH,
    UNIT_VARIANT_KWH_PER_100KM,
    signal_mileage_history_updated,
    signal_soc_history_updated,
    signal_temperature_history_updated,
)
from custom_components.bev_insights.tracker import (
    MileageHistory,
    SocHistory,
    TemperatureHistory,
)

from .base import BevDerivedSensor
from .formulas import _efficiency_value, _local_day_windows, _temperature_band


def _band_label(lower: float | None, upper: float | None) -> str:
    """Human-readable label for a temperature band, e.g. ``"0–10 °C"``."""
    if lower is None:
        return f"< {upper:g} °C"
    if upper is None:
        return f"≥ {lower:g} °C"
    return f"{lower:g}–{upper:g} °C"


class EfficiencyVsTemperatureSensor(BevDerivedSensor):
    """Daily-average outside temperature, with per-band efficiency in attrs.

    The state is today's time-weighted average outside temperature. The
    attributes carry a per-band breakdown of driving efficiency over the
    retained history — each local day's driving is attributed to the band
    its average temperature falls in — plus a `range_loss_percent` figure
    comparing the coldest and warmest populated bands.
    """

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:thermometer-lines"
    _attr_suggested_display_precision = 1

    def __init__(
        self,
        entry: ConfigEntry,
        temperature_history: TemperatureHistory,
        soc_history: SocHistory,
        mileage_history: MileageHistory,
        capacity_factory: CapacitySource,
        capacity_actual: CapacitySource,
        window_days: int,
    ) -> None:
        super().__init__(entry, source_entities=[])
        self._temperature_history = temperature_history
        self._soc_history = soc_history
        self._mileage_history = mileage_history
        self._capacity_factory = capacity_factory
        self._capacity_actual = capacity_actual
        self._window_days = window_days

        self._attr_unique_id = f"{entry.entry_id}_efficiency_vs_temperature"
        self._attr_translation_key = "efficiency_vs_temperature"
        self._attr_name = "Efficiency vs temperature"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        @callback
        def _tick() -> None:
            self._recalculate()
            self.async_write_ha_state()

        for signal in (
            signal_temperature_history_updated(self._entry.entry_id),
            signal_soc_history_updated(self._entry.entry_id),
            signal_mileage_history_updated(self._entry.entry_id),
        ):
            self.async_on_remove(
                async_dispatcher_connect(self.hass, signal, _tick)
            )

        # Hourly tick keeps "today's average" and the rolling window current
        # even when no source entity changes.
        self.async_on_remove(
            async_track_time_change(self.hass, lambda _now: _tick(), minute=0, second=0)
        )

    def _today_start(self, now: datetime) -> datetime:
        local_tz = dt_util.get_time_zone(self.hass.config.time_zone) or dt_util.UTC
        local_midnight = now.astimezone(local_tz).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return local_midnight.astimezone(dt_util.UTC)

    @callback
    def _recalculate(self) -> None:
        now = dt_util.utcnow()
        today_avg = self._temperature_history.daily_average(
            self._today_start(now), now
        )
        if today_avg is None:
            self._attr_available = False
            self._attr_native_value = None
            return
        self._attr_available = True
        self._attr_native_value = round(today_avg, 1)

    def _band_accumulators(
        self, now: datetime
    ) -> dict[str, dict[str, float]]:
        """Accumulate driving distance / SoC consumed per temperature band.

        Walks each local day in the retained window, takes that day's
        average temperature to choose a band, and folds the day's distance
        and SoC consumption into that band. Days with no driving are
        ignored so they don't dilute the efficiency figures.
        """
        accumulators: dict[str, dict[str, float]] = {
            key: {"distance_km": 0.0, "soc_consumed_percent": 0.0, "days": 0.0}
            for key, _, _ in TEMPERATURE_BANDS
        }
        window_start = now - timedelta(days=self._window_days)
        for day_start, day_end in _local_day_windows(window_start, now, self.hass):
            avg_temp = self._temperature_history.daily_average(day_start, day_end)
            if avg_temp is None:
                continue
            distance_km = self._mileage_history.distance_between(day_start, day_end)
            consumed = self._soc_history.consumed_between(day_start, day_end)
            if not distance_km or not consumed:
                continue
            bucket = accumulators[_temperature_band(avg_temp)]
            bucket["distance_km"] += distance_km
            bucket["soc_consumed_percent"] += consumed
            bucket["days"] += 1
        return accumulators

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        now = dt_util.utcnow()
        accumulators = self._band_accumulators(now)
        factory_kwh = self._capacity_factory.current()
        actual_kwh = self._capacity_actual.current()

        bands: list[dict[str, Any]] = []
        for key, lower, upper in TEMPERATURE_BANDS:
            bucket = accumulators[key]
            km = bucket["distance_km"]
            consumed = bucket["soc_consumed_percent"]
            bands.append(
                {
                    "band": key,
                    "label": _band_label(lower, upper),
                    "min_c": lower,
                    "max_c": upper,
                    "days": int(bucket["days"]),
                    "distance_km": round(km, 1),
                    "soc_consumed_percent": round(consumed, 2),
                    "factory_kwh_per_100km": _efficiency_value(
                        factory_kwh, consumed, km, UNIT_VARIANT_KWH_PER_100KM
                    )
                    if factory_kwh is not None
                    else None,
                    "factory_km_per_kwh": _efficiency_value(
                        factory_kwh, consumed, km, UNIT_VARIANT_KM_PER_KWH
                    )
                    if factory_kwh is not None
                    else None,
                    "actual_kwh_per_100km": _efficiency_value(
                        actual_kwh, consumed, km, UNIT_VARIANT_KWH_PER_100KM
                    )
                    if actual_kwh is not None
                    else None,
                    "actual_km_per_kwh": _efficiency_value(
                        actual_kwh, consumed, km, UNIT_VARIANT_KM_PER_KWH
                    )
                    if actual_kwh is not None
                    else None,
                }
            )

        return {
            "window_days": self._window_days,
            "bands": bands,
            "range_loss_percent": _range_loss_percent(bands),
            "partial_window_data": not self._temperature_history.has_pre_window_sample(
                now - timedelta(days=self._window_days)
            ),
        }


def _range_loss_percent(bands: list[dict[str, Any]]) -> float | None:
    """Efficiency penalty of the coldest vs. warmest populated band.

    Compares ``factory_kwh_per_100km`` (higher = worse) of the coldest
    populated band against the warmest. `bands` is ordered cold→warm, so
    the first populated entry is the coldest and the last is the warmest.
    Returns None unless two distinct bands both have a figure.
    """
    populated = [
        b for b in bands if b["factory_kwh_per_100km"] is not None
    ]
    if len(populated) < 2:
        return None
    coldest: float = populated[0]["factory_kwh_per_100km"]
    warmest: float = populated[-1]["factory_kwh_per_100km"]
    if warmest <= 0:
        return None
    return round((coldest - warmest) / warmest * 100.0, 1)


__all__ = ["EfficiencyVsTemperatureSensor"]
