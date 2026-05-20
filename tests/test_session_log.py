"""Tests for SessionLogSensor and ChargeTracker session log."""
from __future__ import annotations

from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant

from custom_components.bev_insights.const import SESSION_LOG_MAX

from .common import (
    ACTUAL_CAPACITY_ENTITY,
    CHARGING_ENTITY,
    MILEAGE_ENTITY,
    RANGE_ENTITY,
    SOC_ENTITY,
    make_entry,
)


async def _setup(hass: HomeAssistant):
    hass.states.async_set(SOC_ENTITY, "50")
    hass.states.async_set(RANGE_ENTITY, "200")
    hass.states.async_set(MILEAGE_ENTITY, "10000")
    hass.states.async_set(CHARGING_ENTITY, "off")
    hass.states.async_set(ACTUAL_CAPACITY_ENTITY, "70")
    entry = make_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _charge_cycle(hass: HomeAssistant, start_soc: str, end_soc: str) -> None:
    """Simulate one complete off→on→off charging cycle."""
    hass.states.async_set(SOC_ENTITY, start_soc)
    hass.states.async_set(CHARGING_ENTITY, "on")
    await hass.async_block_till_done()
    hass.states.async_set(SOC_ENTITY, end_soc)
    hass.states.async_set(CHARGING_ENTITY, "off")
    await hass.async_block_till_done()


def _entity_id(hass: HomeAssistant, entry_id: str) -> str:
    return next(
        e.entity_id
        for e in hass.data["entity_registry"].entities.values()
        if e.config_entry_id == entry_id and e.unique_id.endswith("_session_log")
    )


async def test_session_log_empty_on_fresh_entry(hass: HomeAssistant) -> None:
    """Before any charges the session count is 0."""
    entry = await _setup(hass)
    state = hass.states.get(_entity_id(hass, entry.entry_id))
    assert state is not None
    assert state.state == "0"
    assert state.attributes["sessions"] == []
    assert state.attributes["max_sessions"] == SESSION_LOG_MAX


async def test_session_log_grows_on_charge_cycle(hass: HomeAssistant) -> None:
    """Completing a charge cycle appends a session to the log."""
    entry = await _setup(hass)

    await _charge_cycle(hass, "30", "80")

    state = hass.states.get(_entity_id(hass, entry.entry_id))
    assert state is not None
    assert state.state == "1"
    sessions = state.attributes["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["start_soc_percent"] == 30.0
    assert sessions[0]["end_soc_percent"] == 80.0


async def test_session_log_newest_first(hass: HomeAssistant) -> None:
    """Sessions in the attributes list are ordered newest first."""
    entry = await _setup(hass)

    await _charge_cycle(hass, "20", "80")
    await _charge_cycle(hass, "30", "85")
    await _charge_cycle(hass, "40", "90")

    state = hass.states.get(_entity_id(hass, entry.entry_id))
    assert state is not None
    assert int(state.state) == 3
    sessions = state.attributes["sessions"]
    # Newest-first: last charge had start_soc=40 → should be at index 0.
    assert sessions[0]["start_soc_percent"] == 40.0
    assert sessions[-1]["start_soc_percent"] == 20.0


async def test_session_log_diagnostic_category(hass: HomeAssistant) -> None:
    """SessionLogSensor is in the diagnostic entity category."""
    entry = await _setup(hass)

    registry_entry = next(
        e
        for e in hass.data["entity_registry"].entities.values()
        if e.config_entry_id == entry.entry_id and e.unique_id.endswith("_session_log")
    )
    assert registry_entry.entity_category == EntityCategory.DIAGNOSTIC
