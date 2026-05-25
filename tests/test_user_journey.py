"""End-to-end user-journey test.

Walks a single config entry through the full "day one to day two" arc:

    fresh install (no .storage)
        → setup (recorder backfill runs as no-op without crashing)
        → initial sensor population (full battery range, efficiency, SoH)
        → driving (SoC down, window sensors react)
        → full charge cycle (off → on → off; baseline + last_session captured)
        → post-charge driving (measured_full_range / measured_efficiency populate)
        → diagnostics dump (reflects live state, redactions in place)
        → unload + reload (state survives via persisted Store)
        → post-reload sensor sanity (no regressions)

Every individual piece is already covered by a focused unit test
(`test_sensors.py`, `test_tracker.py`, `test_diagnostics.py`, etc.).
This test exists to catch interaction bugs that only appear when the
pieces run in sequence — e.g. a Store-payload schema mismatch between
what `async_save` writes and what `async_load` accepts after a reload.
"""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bev_insights.const import (
    BASELINE_MILEAGE_KM,
    BASELINE_SOC_PERCENT,
    CONFIG_ENTRY_VERSION,
    DOMAIN,
    SESSION_END_SOC_PERCENT,
    SESSION_START_SOC_PERCENT,
)
from custom_components.bev_insights.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .common import (
    ACTUAL_CAPACITY_ENTITY,
    CHARGING_ENTITY,
    MILEAGE_ENTITY,
    RANGE_ENTITY,
    SOC_ENTITY,
    base_entry_data,
    make_entry,
)


def _find_state(hass: HomeAssistant, unique_id_suffix: str):
    """Locate an entity by stable unique-id suffix and return its state."""
    registry = hass.data["entity_registry"]
    for entity in registry.entities.values():
        if entity.unique_id.endswith(unique_id_suffix):
            return hass.states.get(entity.entity_id)
    raise AssertionError(f"No entity with unique_id ending {unique_id_suffix!r}")


async def test_full_user_journey(hass: HomeAssistant) -> None:
    # ---------------------------------------------------------------- #
    # Day 0: fresh install, source entities reporting normal values    #
    # ---------------------------------------------------------------- #
    hass.states.async_set(SOC_ENTITY, "80")
    hass.states.async_set(RANGE_ENTITY, "320", {"unit_of_measurement": "km"})
    hass.states.async_set(
        MILEAGE_ENTITY, "10000", {"unit_of_measurement": "km"}
    )
    hass.states.async_set(CHARGING_ENTITY, "off")
    hass.states.async_set(ACTUAL_CAPACITY_ENTITY, "70.0")

    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    await hass.async_block_till_done()

    # Instantaneous sensors should already be populated — they only need
    # SoC + range (+ capacity helper for the State of Health one).
    full_range = _find_state(hass, "_full_battery_range")
    assert full_range is not None
    assert full_range.state not in ("unavailable", "unknown")
    # 320 km / 80 % × 100 = 400 km
    assert float(full_range.state) == 400.0

    soh = _find_state(hass, "_state_of_health")
    # 70 / 77 × 100 ≈ 90.91
    assert abs(float(soh.state) - 90.91) < 0.05

    # Tracker-linked sensors have no baseline yet → unavailable.
    last_charged = _find_state(hass, "_last_charged")
    assert last_charged.state in ("unavailable", "unknown")

    # ---------------------------------------------------------------- #
    # Day 1: driving — SoC drops, odometer climbs                      #
    # ---------------------------------------------------------------- #
    hass.states.async_set(SOC_ENTITY, "60")
    hass.states.async_set(RANGE_ENTITY, "240", {"unit_of_measurement": "km"})
    hass.states.async_set(
        MILEAGE_ENTITY, "10120", {"unit_of_measurement": "km"}
    )
    await hass.async_block_till_done()

    # Full-battery-range recomputes from the new live numbers.
    full_range = _find_state(hass, "_full_battery_range")
    assert float(full_range.state) == 400.0  # 240 / 60 × 100

    # Distance-this-week should reflect the 120 km driven.
    distance_this_week = _find_state(hass, "_distance_this_week")
    # On a fresh install the anchor falls back to the oldest sample
    # (10000); current 10120 → 120 km, possibly with partial_window_data.
    assert distance_this_week.state not in ("unavailable", "unknown")
    assert float(distance_this_week.state) >= 100.0

    # ---------------------------------------------------------------- #
    # Day 1 evening: plug in, charge from 60 % → 95 %                  #
    # ---------------------------------------------------------------- #
    hass.states.async_set(CHARGING_ENTITY, "on")
    await hass.async_block_till_done()
    hass.states.async_set(SOC_ENTITY, "95")
    await hass.async_block_till_done()
    hass.states.async_set(CHARGING_ENTITY, "off")
    await hass.async_block_till_done()

    # Tracker should now have a baseline AND a last_session.
    domain_data = hass.data[DOMAIN][entry.entry_id]
    tracker = domain_data["tracker"]
    assert tracker.baseline is not None
    assert tracker.baseline[BASELINE_SOC_PERCENT] == 95.0
    assert tracker.baseline[BASELINE_MILEAGE_KM] == 10120.0
    assert tracker.last_session is not None
    assert tracker.last_session[SESSION_START_SOC_PERCENT] == 60.0
    assert tracker.last_session[SESSION_END_SOC_PERCENT] == 95.0

    # last_charge_added should populate. capacity_actual=70, soc delta=35
    # → 70 × 0.35 = 24.5 kWh.
    last_added = _find_state(hass, "_last_charge_added_actual")
    assert last_added.state not in ("unavailable", "unknown")
    assert abs(float(last_added.state) - 24.5) < 0.1
    # Last-charge-added must publish last_reset so HA's LTS treats each
    # session as its own accumulation window — without it the sum statistic
    # would never reset and aggregations would silently keep growing.
    assert last_added.attributes.get("last_reset") is not None

    last_charged = _find_state(hass, "_last_charged")
    assert last_charged.state not in ("unavailable", "unknown")

    # ---------------------------------------------------------------- #
    # Day 2: post-charge driving — enough km/SoC to clear the noise    #
    # floors so measured_full_range populates.                         #
    # ---------------------------------------------------------------- #
    hass.states.async_set(SOC_ENTITY, "85")  # consumed 10 pp
    hass.states.async_set(
        MILEAGE_ENTITY, "10200", {"unit_of_measurement": "km"}
    )  # 80 km since baseline
    await hass.async_block_till_done()

    measured = _find_state(hass, "_measured_full_range")
    assert measured.state not in ("unavailable", "unknown")
    # 80 km on 10 pp → 800 km extrapolated full range.
    assert float(measured.state) == 800.0

    # ---------------------------------------------------------------- #
    # Diagnostics dump reflects the current state                      #
    # ---------------------------------------------------------------- #
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["version"] is not None
    assert diag["entry"]["title"] == "**REDACTED**"
    assert diag["entry"]["unique_id"] == "**REDACTED**"
    assert diag["sources"]["soc"]["state"] == "85"
    assert diag["sources"]["mileage"]["state"] == "10200"
    assert diag["capacities"]["actual"]["value_kwh"] == 70.0
    assert diag["tracker"]["baseline"][BASELINE_SOC_PERCENT] == 95.0
    assert diag["tracker"]["last_session"][SESSION_START_SOC_PERCENT] == 60.0
    assert diag["tracker"]["is_charging"] is False
    # The histories should report a few samples each.
    assert diag["histories"]["soc"]["sample_count"] >= 3
    assert diag["histories"]["mileage"]["sample_count"] >= 3

    # ---------------------------------------------------------------- #
    # Reload the entry: state should survive via persisted Store       #
    # ---------------------------------------------------------------- #
    assert await hass.config_entries.async_unload(entry.entry_id) is True
    await hass.async_block_till_done()

    assert await hass.config_entries.async_setup(entry.entry_id) is True
    await hass.async_block_till_done()

    domain_data = hass.data[DOMAIN][entry.entry_id]
    tracker_reloaded = domain_data["tracker"]
    assert tracker_reloaded.baseline is not None
    assert tracker_reloaded.baseline[BASELINE_SOC_PERCENT] == 95.0
    assert tracker_reloaded.baseline[BASELINE_MILEAGE_KM] == 10120.0
    assert tracker_reloaded.last_session is not None
    assert (
        tracker_reloaded.last_session[SESSION_START_SOC_PERCENT] == 60.0
    )

    # Histories also survive: at least the post-charge SoC + mileage
    # samples we set above should be back in the deques.
    soc_history = domain_data["soc_history"]
    mileage_history = domain_data["mileage_history"]
    assert soc_history.sample_count >= 2
    assert mileage_history.sample_count >= 2

    # Tracker-linked sensors are available again immediately after
    # reload — they read baseline state on the first _recalculate.
    last_added_after = _find_state(hass, "_last_charge_added_actual")
    assert last_added_after.state not in ("unavailable", "unknown")
    assert abs(float(last_added_after.state) - 24.5) < 0.1

    measured_after = _find_state(hass, "_measured_full_range")
    assert measured_after.state not in ("unavailable", "unknown")
    assert float(measured_after.state) == 800.0


async def test_full_user_journey_without_charging_or_mileage(
    hass: HomeAssistant,
) -> None:
    """Minimal config (SoC + range + capacity only) survives the full arc.

    The optional charging and mileage entities are common to omit — e.g.
    when the upstream integration doesn't expose them. The integration
    should still set up cleanly, skip building the tracker / history /
    window sensors, and produce the instantaneous sensors.
    """
    hass.states.async_set(SOC_ENTITY, "70")
    hass.states.async_set(RANGE_ENTITY, "280", {"unit_of_measurement": "km"})
    hass.states.async_set(ACTUAL_CAPACITY_ENTITY, "75.0")

    data = base_entry_data()
    data.pop("charging_sensor")
    data.pop("mileage_sensor")
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=CONFIG_ENTRY_VERSION,
        data=data,
        title=data["name"],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    await hass.async_block_till_done()

    # Instantaneous sensors work.
    full_range = _find_state(hass, "_full_battery_range")
    assert float(full_range.state) == 400.0

    # Tracker-dependent sensors are not even instantiated.
    registry = hass.data["entity_registry"]
    suffixes = {
        e.unique_id[len(entry.entry_id):]
        for e in registry.entities.values()
        if e.config_entry_id == entry.entry_id
    }
    assert not any(s.startswith("_measured_") for s in suffixes)
    assert not any(s.startswith("_last_charge_added_") for s in suffixes)
    assert not any(s.startswith("_distance_") for s in suffixes)

    # Diagnostics still succeeds.
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["tracker"]["baseline"] is None
    assert diag["histories"]["mileage"] is None

    # Reload cleanly.
    assert await hass.config_entries.async_unload(entry.entry_id) is True
    await hass.async_block_till_done()
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    await hass.async_block_till_done()
    full_range_after = _find_state(hass, "_full_battery_range")
    assert float(full_range_after.state) == 400.0
