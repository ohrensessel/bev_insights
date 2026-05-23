"""Window-bounded sensors (rolling 7 days, calendar week).

- _WindowedSensor: shared scaffolding (dispatcher + hourly tick wiring)
- EnergyConsumedWindowSensor: kWh consumed (× capacity × window)
- StandstillConsumptionWindowSensor: vampire-drain kWh (× capacity × window)
- StandstillRatioWindowSensor: standstill / total consumption (× window)
- ChargeCountWindowSensor: number of charging sessions (× window)
- AverageEfficiencyWindowSensor: efficiency (× capacity × unit × window)
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfEnergy
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_track_time_change
from homeassistant.util import dt as dt_util

from custom_components.bev_insights.capacity import CapacitySource
from custom_components.bev_insights.const import (
    CONF_STANDSTILL_MOVEMENT_THRESHOLD_KM,
    STANDSTILL_MOVEMENT_THRESHOLD_KM,
    signal_mileage_history_updated,
    signal_soc_history_updated,
)
from custom_components.bev_insights.tracker import MileageHistory, SocHistory

from .base import BevDerivedSensor, _TrackerLinkedMixin
from .formulas import (
    _efficiency_value,
    _human_unit,
    _unit_variant_props,
    _window_cutoff,
)


class _WindowedSensor(_TrackerLinkedMixin, BevDerivedSensor):
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

    LTS strategy differs by window shape:
    - `this_week` is a calendar-bound total that resets at local Monday
      00:00. Declared `device_class=ENERGY` + `state_class=TOTAL` with
      `_attr_last_reset` set to the current week's start on every recalc,
      so HA's Long-Term Statistics produces a clean per-week sum curve
      AND the sensor is eligible for the Energy Dashboard.
    - `rolling_7_days` slides continuously and can decrease as old
      samples roll out. HA rejects `ENERGY` + `MEASUREMENT` (only
      TOTAL/TOTAL_INCREASING are allowed for ENERGY) and a sliding TOTAL
      would lie to the Energy Dashboard, so we drop the device class
      entirely and use `MEASUREMENT`. LTS records min/max/mean of the
      rolling figure — useful for trending without polluting the
      energy-totals UI.
    """

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
        if window_key == "this_week":
            self._attr_device_class = SensorDeviceClass.ENERGY
            self._attr_state_class = SensorStateClass.TOTAL
        else:
            self._attr_state_class = SensorStateClass.MEASUREMENT
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
        if self._window_key == "this_week":
            # cutoff IS the last reset point for the calendar-week variant.
            self._attr_last_reset = cutoff
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


class StandstillConsumptionWindowSensor(_WindowedSensor):
    """kWh consumed while the car was parked (vampire / standby drain) over a window.

    For each downward SoC step in the window, checks the odometer over that
    interval. Steps where mileage did not move (< 0.1 km) are attributed to
    standstill drain. Steps that coincide with driving are attributed to driving
    and excluded. The result is the kWh bled away by the car's electronics while
    sitting parked.

    LTS strategy mirrors `EnergyConsumedWindowSensor`:
    - `this_week`: `device_class=ENERGY` + `state_class=TOTAL` with
      `_attr_last_reset` set to the week start → per-week sum LTS plus
      Energy Dashboard eligibility.
    - `rolling_7_days`: no device class + `state_class=MEASUREMENT`
      (ENERGY rejects MEASUREMENT, and ENERGY+TOTAL on a sliding window
      would mislead the Energy Dashboard) → min/max/mean LTS, no
      energy-totals role.
    """

    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_icon = "mdi:sleep"
    _attr_suggested_display_precision = 2

    def __init__(
        self,
        entry: ConfigEntry,
        soc_history: SocHistory,
        mileage_history: MileageHistory,
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
            listen_mileage_history=True,
            capacity_entity=capacity.source_entity,
        )
        self._soc_history = soc_history
        self._mileage_history = mileage_history
        self._capacity = capacity
        self._capacity_variant = capacity_variant
        self._threshold_km = float(
            entry.options.get(
                CONF_STANDSTILL_MOVEMENT_THRESHOLD_KM,
                STANDSTILL_MOVEMENT_THRESHOLD_KM,
            )
        )
        if window_key == "this_week":
            self._attr_device_class = SensorDeviceClass.ENERGY
            self._attr_state_class = SensorStateClass.TOTAL
        else:
            self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_unique_id = (
            f"{entry.entry_id}_standstill_consumption_"
            f"{window_key}_{capacity_variant}"
        )
        self._attr_translation_key = (
            f"standstill_consumption_{window_key}_{capacity_variant}"
        )
        self._attr_name = (
            f"Standstill consumption ({window_label.lower()}, "
            f"{capacity_variant} capacity)"
        )

    @callback
    def _recalculate(self) -> None:
        cutoff = _window_cutoff(self.hass, self._window_key, dt_util.utcnow())
        if self._window_key == "this_week":
            self._attr_last_reset = cutoff
        consumed_pct = self._soc_history.standstill_consumed_since(
            cutoff, self._mileage_history, self._threshold_km
        )
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
        consumed_pct = self._soc_history.standstill_consumed_since(
            cutoff, self._mileage_history, self._threshold_km
        )
        return {
            "window": self._window_key,
            "window_start": cutoff.isoformat(),
            "partial_window_data": (
                not self._soc_history.has_pre_window_sample(cutoff)
                or not self._mileage_history.has_pre_window_sample(cutoff)
            ),
            "capacity_variant": self._capacity_variant,
            "capacity_kwh": self._capacity.current(),
            "capacity_source": self._capacity.describe(),
            "soc_consumed_standstill_percent": (
                round(consumed_pct, 2) if consumed_pct is not None else None
            ),
        }


class StandstillRatioWindowSensor(_WindowedSensor):
    """Fraction of total SoC consumption attributable to standstill (vampire) drain.

        standstill_ratio = standstill_consumed / total_consumed * 100  [%]

    Gives a quick answer to "how much of my battery is the car bleeding
    away while parked vs. how much am I actually using for driving?"
    A value near 0 % is good; higher values indicate parasitic drain.

    Unavailable when either history is absent, total_consumed is zero
    (no driving or charging data in the window), or when the car was
    never parked during the window.

    No capacity variants — the ratio is dimensionless.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_icon = "mdi:sleep"
    _attr_suggested_display_precision = 1

    def __init__(
        self,
        entry: ConfigEntry,
        soc_history: SocHistory,
        mileage_history: MileageHistory,
        window_key: str,
        window_label: str,
    ) -> None:
        super().__init__(
            entry,
            window_key,
            window_label,
            listen_soc_history=True,
            listen_mileage_history=True,
        )
        self._soc_history = soc_history
        self._mileage_history = mileage_history
        self._threshold_km = float(
            entry.options.get(
                CONF_STANDSTILL_MOVEMENT_THRESHOLD_KM,
                STANDSTILL_MOVEMENT_THRESHOLD_KM,
            )
        )
        self._attr_unique_id = f"{entry.entry_id}_standstill_ratio_{window_key}"
        self._attr_translation_key = f"standstill_ratio_{window_key}"
        self._attr_name = f"Standstill ratio ({window_label.lower()})"

    @callback
    def _recalculate(self) -> None:
        cutoff = _window_cutoff(self.hass, self._window_key, dt_util.utcnow())
        total = self._soc_history.consumed_since(cutoff)
        standstill = self._soc_history.standstill_consumed_since(
            cutoff, self._mileage_history, self._threshold_km
        )
        if total is None or standstill is None or total <= 0:
            self._attr_available = False
            self._attr_native_value = None
            return
        self._attr_available = True
        self._attr_native_value = round(standstill / total * 100.0, 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        cutoff = _window_cutoff(self.hass, self._window_key, dt_util.utcnow())
        total = self._soc_history.consumed_since(cutoff)
        standstill = self._soc_history.standstill_consumed_since(
            cutoff, self._mileage_history, self._threshold_km
        )
        return {
            "window": self._window_key,
            "window_start": cutoff.isoformat(),
            "partial_window_data": (
                not self._soc_history.has_pre_window_sample(cutoff)
                or not self._mileage_history.has_pre_window_sample(cutoff)
            ),
            "total_soc_consumed_percent": (
                round(total, 2) if total is not None else None
            ),
            "standstill_soc_consumed_percent": (
                round(standstill, 2) if standstill is not None else None
            ),
        }


class ChargeCountWindowSensor(_WindowedSensor):
    """Number of charging sessions completed in a window.

    A session is a contiguous run of upward SoC steps totalling ≥ 5 %,
    which filters out quantization noise while catching every real charge.

    No capacity variants — count is independent of battery size. No
    device class either (HA has no "count" device class), which means
    MEASUREMENT is unconstrained.

    LTS strategy:
    - `this_week`: `state_class=TOTAL` + `_attr_last_reset` at the week
      start → per-week sum curve.
    - `rolling_7_days`: `state_class=MEASUREMENT` → min/max/mean LTS,
      letting users trend "average rolling-7-day charge count" without
      the sliding window pretending to be an accumulator.
    """

    _attr_native_unit_of_measurement = "charges"
    _attr_icon = "mdi:battery-charging-100"
    _attr_suggested_display_precision = 0

    def __init__(
        self,
        entry: ConfigEntry,
        soc_history: SocHistory,
        window_key: str,
        window_label: str,
    ) -> None:
        super().__init__(
            entry,
            window_key,
            window_label,
            listen_soc_history=True,
            listen_mileage_history=False,
        )
        self._soc_history = soc_history
        if window_key == "this_week":
            self._attr_state_class = SensorStateClass.TOTAL
        else:
            self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_unique_id = f"{entry.entry_id}_charge_count_{window_key}"
        self._attr_translation_key = f"charge_count_{window_key}"
        self._attr_name = f"Charge count ({window_label.lower()})"

    @callback
    def _recalculate(self) -> None:
        cutoff = _window_cutoff(self.hass, self._window_key, dt_util.utcnow())
        if self._window_key == "this_week":
            self._attr_last_reset = cutoff
        self._attr_available = True
        self._attr_native_value = self._soc_history.charge_count_since(cutoff)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        cutoff = _window_cutoff(self.hass, self._window_key, dt_util.utcnow())
        return {
            "window": self._window_key,
            "window_start": cutoff.isoformat(),
            "partial_window_data": not self._soc_history.has_pre_window_sample(cutoff),
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
