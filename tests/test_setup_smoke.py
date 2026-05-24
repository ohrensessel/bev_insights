"""Smoke test: set up an entry with all features wired up and check the
expected number of entities are created with the right unique-id suffixes.

This is a wiring test, not a math test — `test_sensors.py` covers the
formulas. The point here is to catch regressions where a sensor class
silently stops being instantiated.
"""
from __future__ import annotations

from homeassistant.core import HomeAssistant

from .common import (
    ACTUAL_CAPACITY_ENTITY,
    CHARGING_ENTITY,
    MILEAGE_ENTITY,
    RANGE_ENTITY,
    SOC_ENTITY,
    make_entry,
)

# Every unique-id suffix the integration emits with a fully-wired entry.
# Updating this set is intentional: it forces the test to be revised when
# a sensor is added or removed.
EXPECTED_SUFFIXES = {
    "_full_battery_range",
    "_state_of_health",
    "_time_since_last_charge",
    "_efficiency_factory_kwh_per_100km",
    "_efficiency_factory_km_per_kwh",
    "_efficiency_actual_kwh_per_100km",
    "_efficiency_actual_km_per_kwh",
    "_measured_full_range",
    "_measured_efficiency_factory_kwh_per_100km",
    "_measured_efficiency_factory_km_per_kwh",
    "_measured_efficiency_actual_kwh_per_100km",
    "_measured_efficiency_actual_km_per_kwh",
    "_last_charged",
    "_last_charge_added_factory",
    "_last_charge_added_actual",
    "_avg_charging_power_factory",
    "_avg_charging_power_actual",
    "_distance_rolling_7_days",
    "_distance_this_week",
    "_distance_this_month",
    "_distance_this_year",
    "_distance_week_delta",
    "_energy_consumed_rolling_7_days_factory",
    "_energy_consumed_rolling_7_days_actual",
    "_energy_consumed_this_week_factory",
    "_energy_consumed_this_week_actual",
    "_energy_consumed_week_delta_factory",
    "_energy_consumed_week_delta_actual",
    "_avg_efficiency_rolling_7_days_factory_kwh_per_100km",
    "_avg_efficiency_rolling_7_days_factory_km_per_kwh",
    "_avg_efficiency_rolling_7_days_actual_kwh_per_100km",
    "_avg_efficiency_rolling_7_days_actual_km_per_kwh",
    "_avg_efficiency_this_week_factory_kwh_per_100km",
    "_avg_efficiency_this_week_factory_km_per_kwh",
    "_avg_efficiency_this_week_actual_kwh_per_100km",
    "_avg_efficiency_this_week_actual_km_per_kwh",
    "_standstill_consumption_rolling_7_days_factory",
    "_standstill_consumption_rolling_7_days_actual",
    "_standstill_consumption_this_week_factory",
    "_standstill_consumption_this_week_actual",
    "_days_to_low_soc",
    "_idle_time",
    "_charge_count_rolling_7_days",
    "_charge_count_this_week",
    "_session_log",
    "_standstill_ratio_rolling_7_days",
    "_standstill_ratio_this_week",
}


async def test_full_entry_creates_all_47_entities(hass: HomeAssistant) -> None:
    hass.states.async_set(SOC_ENTITY, "50")
    hass.states.async_set(RANGE_ENTITY, "200")
    hass.states.async_set(MILEAGE_ENTITY, "10000")
    hass.states.async_set(CHARGING_ENTITY, "off")
    hass.states.async_set(ACTUAL_CAPACITY_ENTITY, "70")

    entry = make_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    registry = hass.data["entity_registry"]
    suffixes = {
        e.unique_id[len(entry.entry_id):]
        for e in registry.entities.values()
        if e.config_entry_id == entry.entry_id
    }
    assert suffixes == EXPECTED_SUFFIXES
    assert len(suffixes) == 47
