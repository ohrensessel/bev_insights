"""Formula-correctness tests for the derived sensors.

Each test sets up a real config entry against `hass`, drives the source
states to known values, and asserts the derived sensor reflects the
expected formula output.
"""
from __future__ import annotations

import math

from homeassistant.core import HomeAssistant
import pytest

from .common import (
    ACTUAL_CAPACITY_ENTITY,
    CHARGING_ENTITY,
    MILEAGE_ENTITY,
    RANGE_ENTITY,
    SOC_ENTITY,
    make_entry,
)


async def _setup_full(hass: HomeAssistant):
    """Configure an entry with all sources primed; return entry."""
    hass.states.async_set(SOC_ENTITY, "50")
    hass.states.async_set(RANGE_ENTITY, "200", {"unit_of_measurement": "km"})
    hass.states.async_set(MILEAGE_ENTITY, "10000", {"unit_of_measurement": "km"})
    hass.states.async_set(CHARGING_ENTITY, "off")
    hass.states.async_set(ACTUAL_CAPACITY_ENTITY, "70.0")
    entry = make_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_full_battery_range_formula(hass: HomeAssistant) -> None:
    entry = await _setup_full(hass)
    state = hass.states.get(f"sensor.{entry.title.lower().replace(' ', '_')}_full_battery_range")
    # 200 km / 50% * 100 = 400 km
    assert state is not None
    assert float(state.state) == 400.0


async def test_efficiency_factory_kwh_per_100km(hass: HomeAssistant) -> None:
    entry = await _setup_full(hass)
    slug = entry.title.lower().replace(" ", "_")
    state = hass.states.get(
        f"sensor.{slug}_efficiency_factory_capacity_k_wh_100_km"
    )
    # Fallback: locate by unique_id via the entity registry if the slug differs.
    if state is None:
        state = _find_state(hass, "_efficiency_factory_kwh_per_100km")
    # capacity * soc / range = 77 * 50 / 200 = 19.25 kWh/100 km
    assert state is not None
    assert float(state.state) == pytest.approx(19.25)


async def test_efficiency_factory_km_per_kwh(hass: HomeAssistant) -> None:
    await _setup_full(hass)
    state = _find_state(hass, "_efficiency_factory_km_per_kwh")
    # range / (capacity * soc / 100) = 200 / (77 * 50 / 100) = 200 / 38.5
    assert state is not None
    assert float(state.state) == pytest.approx(200 / 38.5, rel=1e-3)


async def test_efficiency_actual_uses_actual_capacity(hass: HomeAssistant) -> None:
    await _setup_full(hass)
    state = _find_state(hass, "_efficiency_actual_kwh_per_100km")
    # capacity * soc / range = 70 * 50 / 200 = 17.5 kWh/100 km
    assert state is not None
    assert float(state.state) == pytest.approx(17.5)


async def test_actual_capacity_change_propagates_live(hass: HomeAssistant) -> None:
    """Moving the input_number recomputes actual-capacity sensors live."""
    await _setup_full(hass)
    hass.states.async_set(ACTUAL_CAPACITY_ENTITY, "60.0")
    await hass.async_block_till_done()

    state = _find_state(hass, "_efficiency_actual_kwh_per_100km")
    # 60 * 50 / 200 = 15
    assert float(state.state) == pytest.approx(15.0)


async def test_actual_capacity_unavailable_makes_sensor_unavailable(
    hass: HomeAssistant,
) -> None:
    await _setup_full(hass)
    hass.states.async_set(ACTUAL_CAPACITY_ENTITY, "unavailable")
    await hass.async_block_till_done()
    state = _find_state(hass, "_efficiency_actual_kwh_per_100km")
    assert state is not None
    assert state.state in ("unavailable", "unknown")


async def test_efficiency_unit_variants_are_reciprocal(hass: HomeAssistant) -> None:
    """kWh/100 km × km/kWh should be ~100."""
    await _setup_full(hass)
    for variant in ("factory", "actual"):
        kwh100 = float(_find_state(hass, f"_efficiency_{variant}_kwh_per_100km").state)
        kmkwh = float(_find_state(hass, f"_efficiency_{variant}_km_per_kwh").state)
        assert math.isclose(kwh100 * kmkwh, 100.0, rel_tol=1e-2)


async def test_full_battery_range_unavailable_at_zero_soc(hass: HomeAssistant) -> None:
    await _setup_full(hass)
    hass.states.async_set(SOC_ENTITY, "0")
    await hass.async_block_till_done()
    state = _find_state(hass, "_full_battery_range")
    assert state.state in ("unavailable", "unknown")


async def test_measured_full_range_unavailable_without_baseline(
    hass: HomeAssistant,
) -> None:
    await _setup_full(hass)
    state = _find_state(hass, "_measured_full_range")
    # No charge has ended yet → no baseline → unavailable.
    assert state.state in ("unavailable", "unknown")


async def test_measured_full_range_after_charge_end(hass: HomeAssistant) -> None:
    await _setup_full(hass)
    # Simulate a charge: on → off captures baseline at the current state.
    hass.states.async_set(SOC_ENTITY, "100")
    hass.states.async_set(MILEAGE_ENTITY, "10000")
    hass.states.async_set(CHARGING_ENTITY, "on")
    await hass.async_block_till_done()
    hass.states.async_set(CHARGING_ENTITY, "off")
    await hass.async_block_till_done()

    # Drive: SoC 100 → 60 over 200 km.
    hass.states.async_set(MILEAGE_ENTITY, "10200")
    hass.states.async_set(SOC_ENTITY, "60")
    await hass.async_block_till_done()

    state = _find_state(hass, "_measured_full_range")
    # 200 km / 40 % * 100 = 500 km
    assert float(state.state) == pytest.approx(500.0, rel=1e-3)
    # And measured efficiency factory kWh/100 km: 77 * 40 / 200 = 15.4
    eff = _find_state(hass, "_measured_efficiency_factory_kwh_per_100km")
    assert float(eff.state) == pytest.approx(15.4, rel=1e-3)


async def test_measured_full_range_unavailable_while_charging(
    hass: HomeAssistant,
) -> None:
    """Even with a healthy baseline and a valid drive, the sensor must be
    unavailable while the car is plugged in and SoC is rising back."""
    await _setup_full(hass)
    # Establish a baseline at 100% SoC, 10000 km.
    hass.states.async_set(SOC_ENTITY, "100")
    hass.states.async_set(MILEAGE_ENTITY, "10000")
    hass.states.async_set(CHARGING_ENTITY, "on")
    await hass.async_block_till_done()
    hass.states.async_set(CHARGING_ENTITY, "off")
    await hass.async_block_till_done()

    # Drive 200 km, SoC down to 60% → sensor would normally show 500 km.
    hass.states.async_set(MILEAGE_ENTITY, "10200")
    hass.states.async_set(SOC_ENTITY, "60")
    await hass.async_block_till_done()
    state = _find_state(hass, "_measured_full_range")
    assert float(state.state) == pytest.approx(500.0, rel=1e-3)

    # Now plug in again. SoC starts climbing; the sensor must suppress.
    hass.states.async_set(CHARGING_ENTITY, "on")
    await hass.async_block_till_done()
    state = _find_state(hass, "_measured_full_range")
    assert state.state in ("unavailable", "unknown")


async def test_measured_full_range_below_distance_threshold(
    hass: HomeAssistant,
) -> None:
    """A short drive (< MIN_MEASURED_RANGE_KM) must not produce a value."""
    await _setup_full(hass)
    hass.states.async_set(SOC_ENTITY, "100")
    hass.states.async_set(MILEAGE_ENTITY, "10000")
    hass.states.async_set(CHARGING_ENTITY, "on")
    await hass.async_block_till_done()
    hass.states.async_set(CHARGING_ENTITY, "off")
    await hass.async_block_till_done()

    # Only 10 km driven (below the 20 km floor), even though SoC consumed
    # is well above the percent threshold.
    hass.states.async_set(MILEAGE_ENTITY, "10010")
    hass.states.async_set(SOC_ENTITY, "95")
    await hass.async_block_till_done()
    state = _find_state(hass, "_measured_full_range")
    assert state.state in ("unavailable", "unknown")


async def test_measured_full_range_below_soc_threshold(
    hass: HomeAssistant,
) -> None:
    """A long drive with tiny SoC delta (< MIN_MEASURED_RANGE_SOC_PERCENT)
    must not produce a value — typical of noisy 1% SoC quantization."""
    await _setup_full(hass)
    hass.states.async_set(SOC_ENTITY, "100")
    hass.states.async_set(MILEAGE_ENTITY, "10000")
    hass.states.async_set(CHARGING_ENTITY, "on")
    await hass.async_block_till_done()
    hass.states.async_set(CHARGING_ENTITY, "off")
    await hass.async_block_till_done()

    # 50 km driven (above the distance floor), but only 1% SoC consumed.
    hass.states.async_set(MILEAGE_ENTITY, "10050")
    hass.states.async_set(SOC_ENTITY, "99")
    await hass.async_block_till_done()
    state = _find_state(hass, "_measured_full_range")
    assert state.state in ("unavailable", "unknown")


async def test_measured_full_range_becomes_available_once_thresholds_met(
    hass: HomeAssistant,
) -> None:
    """Just below the floor → unavailable; just above → available."""
    await _setup_full(hass)
    hass.states.async_set(SOC_ENTITY, "100")
    hass.states.async_set(MILEAGE_ENTITY, "10000")
    hass.states.async_set(CHARGING_ENTITY, "on")
    await hass.async_block_till_done()
    hass.states.async_set(CHARGING_ENTITY, "off")
    await hass.async_block_till_done()

    # Below the distance threshold: 19 km driven, 5% SoC consumed.
    hass.states.async_set(MILEAGE_ENTITY, "10019")
    hass.states.async_set(SOC_ENTITY, "95")
    await hass.async_block_till_done()
    assert _find_state(hass, "_measured_full_range").state in (
        "unavailable",
        "unknown",
    )

    # Cross both thresholds: 25 km driven, 5% SoC consumed → 500 km.
    hass.states.async_set(MILEAGE_ENTITY, "10025")
    await hass.async_block_till_done()
    state = _find_state(hass, "_measured_full_range")
    assert float(state.state) == pytest.approx(500.0, rel=1e-3)


async def test_measured_efficiency_all_four_variants(hass: HomeAssistant) -> None:
    """Cover every (capacity × unit) combination in one realistic drive."""
    await _setup_full(hass)
    hass.states.async_set(SOC_ENTITY, "100")
    hass.states.async_set(MILEAGE_ENTITY, "10000")
    hass.states.async_set(CHARGING_ENTITY, "on")
    await hass.async_block_till_done()
    hass.states.async_set(CHARGING_ENTITY, "off")
    await hass.async_block_till_done()

    # Drive 100 → 60 SoC over 200 km.
    hass.states.async_set(MILEAGE_ENTITY, "10200")
    hass.states.async_set(SOC_ENTITY, "60")
    await hass.async_block_till_done()

    # Factory kWh/100km: 77 × 40 / 200 = 15.4
    s = _find_state(hass, "_measured_efficiency_factory_kwh_per_100km")
    assert float(s.state) == pytest.approx(15.4)
    # Factory km/kWh: 200 / (77 × 40 / 100) = 200 / 30.8 ≈ 6.493
    s = _find_state(hass, "_measured_efficiency_factory_km_per_kwh")
    assert float(s.state) == pytest.approx(200 / 30.8, rel=1e-3)
    # Actual kWh/100km: 70 × 40 / 200 = 14.0
    s = _find_state(hass, "_measured_efficiency_actual_kwh_per_100km")
    assert float(s.state) == pytest.approx(14.0)
    # Actual km/kWh: 200 / 28 ≈ 7.143
    s = _find_state(hass, "_measured_efficiency_actual_km_per_kwh")
    assert float(s.state) == pytest.approx(200 / 28.0, rel=1e-3)


async def test_full_battery_range_unavailable_on_missing_range(
    hass: HomeAssistant,
) -> None:
    await _setup_full(hass)
    hass.states.async_set(RANGE_ENTITY, "unavailable")
    await hass.async_block_till_done()
    state = _find_state(hass, "_full_battery_range")
    assert state.state in ("unavailable", "unknown")


async def test_full_battery_range_handles_soc_above_100(
    hass: HomeAssistant,
) -> None:
    """A glitched SoC > 100 should be clamped (not produce a too-low figure)."""
    await _setup_full(hass)
    hass.states.async_set(SOC_ENTITY, "105")
    await hass.async_block_till_done()
    state = _find_state(hass, "_full_battery_range")
    # Clamped to 100 → 200 km / 100 × 100 = 200 km
    assert float(state.state) == pytest.approx(200.0)


async def test_state_of_health_formula(hass: HomeAssistant) -> None:
    """SoH = actual / factory × 100. With factory=77, actual=70 → 90.9%."""
    await _setup_full(hass)
    state = _find_state(hass, "_state_of_health")
    assert float(state.state) == pytest.approx(70.0 / 77.0 * 100.0, rel=1e-3)


async def test_state_of_health_updates_live(hass: HomeAssistant) -> None:
    await _setup_full(hass)
    hass.states.async_set(ACTUAL_CAPACITY_ENTITY, "60.0")
    await hass.async_block_till_done()
    state = _find_state(hass, "_state_of_health")
    # 60 / 77 ≈ 77.9 %
    assert float(state.state) == pytest.approx(60.0 / 77.0 * 100.0, rel=1e-3)


async def test_state_of_health_unavailable_when_capacity_missing(
    hass: HomeAssistant,
) -> None:
    await _setup_full(hass)
    hass.states.async_set(ACTUAL_CAPACITY_ENTITY, "unavailable")
    await hass.async_block_till_done()
    state = _find_state(hass, "_state_of_health")
    assert state.state in ("unavailable", "unknown")


async def test_time_since_last_charge_unavailable_without_baseline(
    hass: HomeAssistant,
) -> None:
    await _setup_full(hass)
    state = _find_state(hass, "_time_since_last_charge")
    assert state.state in ("unavailable", "unknown")


async def test_time_since_last_charge_after_charge_end(hass: HomeAssistant) -> None:
    """Right after a charge end the sensor reads ~0 hours."""
    await _setup_full(hass)
    hass.states.async_set(CHARGING_ENTITY, "on")
    await hass.async_block_till_done()
    hass.states.async_set(CHARGING_ENTITY, "off")
    await hass.async_block_till_done()

    state = _find_state(hass, "_time_since_last_charge")
    # Just charged → elapsed ≈ 0 hours.
    assert float(state.state) == pytest.approx(0.0, abs=0.01)


async def test_last_charge_added_unavailable_without_session(
    hass: HomeAssistant,
) -> None:
    await _setup_full(hass)
    state = _find_state(hass, "_last_charge_added_factory")
    assert state.state in ("unavailable", "unknown")


async def test_last_charge_added_after_cycle(hass: HomeAssistant) -> None:
    """Drive a full off→on→off cycle and check the kWh formula."""
    await _setup_full(hass)
    # Start of session at 30% SoC.
    hass.states.async_set(SOC_ENTITY, "30")
    hass.states.async_set(CHARGING_ENTITY, "on")
    await hass.async_block_till_done()
    # End of session at 80% SoC.
    hass.states.async_set(SOC_ENTITY, "80")
    await hass.async_block_till_done()
    hass.states.async_set(CHARGING_ENTITY, "off")
    await hass.async_block_till_done()

    # Factory: 77 kWh × 50% = 38.5 kWh
    factory = _find_state(hass, "_last_charge_added_factory")
    assert float(factory.state) == pytest.approx(38.5)
    # Actual: 70 kWh × 50% = 35.0 kWh
    actual = _find_state(hass, "_last_charge_added_actual")
    assert float(actual.state) == pytest.approx(35.0)


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _find_state(hass: HomeAssistant, unique_id_suffix: str):
    """Locate an entity's state by unique-id suffix via the entity registry.

    Entity-id slugs depend on the entry title + translations; matching on
    unique-id keeps these tests robust to that slugification.
    """
    registry = hass.data["entity_registry"]
    for entity in registry.entities.values():
        if entity.unique_id.endswith(unique_id_suffix):
            return hass.states.get(entity.entity_id)
    raise AssertionError(f"No entity with unique_id ending {unique_id_suffix!r}")
