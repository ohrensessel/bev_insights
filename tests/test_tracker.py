"""Tests for the `ChargeTracker` class."""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.myskoda_insights.const import (
    BASELINE_MILEAGE_KM,
    BASELINE_SOC_PERCENT,
    BASELINE_TIMESTAMP,
    DOMAIN,
)
from custom_components.myskoda_insights.tracker import ChargeTracker

CHARGING = "binary_sensor.car_charging"
MILEAGE = "sensor.car_mileage"
SOC = "sensor.car_soc"


def _entry() -> MockConfigEntry:
    return MockConfigEntry(domain=DOMAIN, data={}, entry_id="tracker_test")


async def _make_tracker(hass: HomeAssistant) -> ChargeTracker:
    tracker = ChargeTracker(
        hass,
        _entry(),
        charging_entity=CHARGING,
        mileage_entity=MILEAGE,
        soc_entity=SOC,
    )
    await tracker.async_load()
    tracker.async_start()
    return tracker


async def test_baseline_is_none_when_never_charged(hass: HomeAssistant) -> None:
    tracker = await _make_tracker(hass)
    assert tracker.baseline is None
    await tracker.async_stop()


async def test_charge_end_captures_baseline(hass: HomeAssistant) -> None:
    hass.states.async_set(MILEAGE, "12345.6")
    hass.states.async_set(SOC, "82.0")
    hass.states.async_set(CHARGING, "on")
    tracker = await _make_tracker(hass)
    await hass.async_block_till_done()

    # Trailing edge: charging → off.
    hass.states.async_set(CHARGING, "off")
    await hass.async_block_till_done()

    assert tracker.baseline is not None
    assert tracker.baseline[BASELINE_MILEAGE_KM] == 12345.6
    assert tracker.baseline[BASELINE_SOC_PERCENT] == 82.0
    assert BASELINE_TIMESTAMP in tracker.baseline
    await tracker.async_stop()


async def test_not_charging_to_not_charging_does_not_capture(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set(MILEAGE, "1000")
    hass.states.async_set(SOC, "50")
    hass.states.async_set(CHARGING, "off")
    tracker = await _make_tracker(hass)
    await hass.async_block_till_done()

    hass.states.async_set(CHARGING, "off")  # no-op transition
    await hass.async_block_till_done()
    assert tracker.baseline is None
    await tracker.async_stop()


async def test_charging_to_charging_does_not_capture(hass: HomeAssistant) -> None:
    """An intermediate 'charging' → 'charging' state echo must not fire."""
    hass.states.async_set(MILEAGE, "1000")
    hass.states.async_set(SOC, "50")
    hass.states.async_set(CHARGING, "on")
    tracker = await _make_tracker(hass)
    await hass.async_block_till_done()

    hass.states.async_set(CHARGING, "charging")
    await hass.async_block_till_done()
    assert tracker.baseline is None
    await tracker.async_stop()


async def test_charge_end_with_missing_sources_does_not_capture(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set(CHARGING, "on")
    tracker = await _make_tracker(hass)
    # mileage + soc states never set.

    hass.states.async_set(CHARGING, "off")
    await hass.async_block_till_done()
    assert tracker.baseline is None
    await tracker.async_stop()


async def test_baseline_persists_across_reloads(hass: HomeAssistant) -> None:
    """A second tracker over the same entry sees the persisted baseline."""
    entry = _entry()
    hass.states.async_set(MILEAGE, "1000")
    hass.states.async_set(SOC, "60")
    hass.states.async_set(CHARGING, "on")

    tracker_a = ChargeTracker(
        hass,
        entry,
        charging_entity=CHARGING,
        mileage_entity=MILEAGE,
        soc_entity=SOC,
    )
    await tracker_a.async_load()
    tracker_a.async_start()
    hass.states.async_set(CHARGING, "off")
    await hass.async_block_till_done()
    assert tracker_a.baseline is not None
    await tracker_a.async_stop()

    # Recreate over same entry_id → should restore baseline from disk.
    tracker_b = ChargeTracker(
        hass,
        entry,
        charging_entity=CHARGING,
        mileage_entity=MILEAGE,
        soc_entity=SOC,
    )
    await tracker_b.async_load()
    assert tracker_b.baseline is not None
    assert tracker_b.baseline[BASELINE_MILEAGE_KM] == 1000.0
    assert tracker_b.baseline[BASELINE_SOC_PERCENT] == 60.0
