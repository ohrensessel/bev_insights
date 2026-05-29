"""Tests for the DaysToLowSocSensor."""
from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from custom_components.bev_insights.const import (
    CONF_LOW_SOC_THRESHOLD_PERCENT,
    DOMAIN,
    signal_soc_history_updated,
)

from .common import (
    ACTUAL_CAPACITY_ENTITY,
    CHARGING_ENTITY,
    MILEAGE_ENTITY,
    RANGE_ENTITY,
    SOC_ENTITY,
    make_entry,
    seed_history,
)


async def _setup(hass: HomeAssistant, soc: str = "60", options: dict | None = None):
    hass.states.async_set(SOC_ENTITY, soc)
    hass.states.async_set(RANGE_ENTITY, "200")
    hass.states.async_set(MILEAGE_ENTITY, "10000")
    hass.states.async_set(CHARGING_ENTITY, "off")
    hass.states.async_set(ACTUAL_CAPACITY_ENTITY, "70")
    entry = make_entry(options=options)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _entity_id(hass: HomeAssistant, entry_id: str) -> str:
    return next(
        e.entity_id
        for e in hass.data["entity_registry"].entities.values()
        if e.config_entry_id == entry_id
        and e.unique_id.endswith("_days_to_low_soc")
    )


async def test_days_to_low_soc_unavailable_no_history(hass: HomeAssistant) -> None:
    """Sensor is unavailable when SoC history has no consumption yet."""
    entry = await _setup(hass)
    state = hass.states.get(_entity_id(hass, entry.entry_id))
    # No SoC drops yet → consumed_since returns 0 → unavailable
    assert state is not None
    assert state.state == "unavailable"


async def test_days_to_low_soc_basic(hass: HomeAssistant) -> None:
    """Formula: (current_soc - threshold) / (consumed_7d / 7)."""
    entry = await _setup(hass, soc="60")

    # Inject 14% SoC consumption over the past 7 days (2%/day average).
    soc_history = hass.data[DOMAIN][entry.entry_id]["soc_history"]
    now = dt_util.utcnow()
    seed_history(soc_history, [
        (now - timedelta(days=7), 74.0),
        (now, 60.0),
    ])

    # Fire the history dispatcher so the sensor recomputes.
    async_dispatcher_send(hass, signal_soc_history_updated(entry.entry_id))
    await hass.async_block_till_done()

    state = hass.states.get(_entity_id(hass, entry.entry_id))
    assert state is not None
    assert state.state != "unavailable"
    # (60 - 20) / (14 / 7) = 40 / 2 = 20.0 days
    assert float(state.state) == 20.0


async def test_days_to_low_soc_at_threshold_is_unavailable(hass: HomeAssistant) -> None:
    """Sensor is unavailable when current SoC is at or below the threshold."""
    entry = await _setup(hass, soc="20")

    soc_history = hass.data[DOMAIN][entry.entry_id]["soc_history"]
    now = dt_util.utcnow()
    seed_history(soc_history, [
        (now - timedelta(days=7), 34.0),
        (now, 20.0),
    ])

    async_dispatcher_send(hass, signal_soc_history_updated(entry.entry_id))
    await hass.async_block_till_done()

    state = hass.states.get(_entity_id(hass, entry.entry_id))
    assert state is not None
    assert state.state == "unavailable"


async def test_days_to_low_soc_custom_threshold(hass: HomeAssistant) -> None:
    """Custom low_soc_threshold_percent option is respected."""
    entry = await _setup(
        hass,
        soc="50",
        options={CONF_LOW_SOC_THRESHOLD_PERCENT: 10.0},
    )

    soc_history = hass.data[DOMAIN][entry.entry_id]["soc_history"]
    now = dt_util.utcnow()
    seed_history(soc_history, [
        (now - timedelta(days=7), 57.0),
        (now, 50.0),  # 7% drop → 1%/day
    ])

    async_dispatcher_send(hass, signal_soc_history_updated(entry.entry_id))
    await hass.async_block_till_done()

    state = hass.states.get(_entity_id(hass, entry.entry_id))
    assert state is not None
    assert state.state != "unavailable"
    # (50 - 10) / (7 / 7) = 40 / 1 = 40.0 days
    assert float(state.state) == 40.0
