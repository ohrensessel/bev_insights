"""Tests for the IdleTimeSensor.

The sensor reports `(now - latest_mileage_sample_ts) / 3600` in hours.
MileageHistory dedupes consecutive identical values, so the latest
sample's timestamp is always when the odometer *last moved*. We test
both the formula and the "no history" path.
"""
from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from custom_components.bev_insights.const import DOMAIN, signal_mileage_history_updated

from .common import (
    ACTUAL_CAPACITY_ENTITY,
    CHARGING_ENTITY,
    MILEAGE_ENTITY,
    RANGE_ENTITY,
    SOC_ENTITY,
    make_entry,
)


async def _setup_full(hass: HomeAssistant):
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


def _find_state(hass: HomeAssistant, unique_id_suffix: str):
    registry = hass.data["entity_registry"]
    for entity in registry.entities.values():
        if entity.unique_id.endswith(unique_id_suffix):
            return hass.states.get(entity.entity_id)
    raise AssertionError(f"No entity with unique_id ending {unique_id_suffix!r}")


def _mileage_history(hass: HomeAssistant, entry):
    return hass.data[DOMAIN][entry.entry_id]["mileage_history"]


async def test_idle_time_reports_hours_since_last_change(
    hass: HomeAssistant,
) -> None:
    """Latest sample at -5h → sensor reports ~5.0 h."""
    entry = await _setup_full(hass)
    mileage_history = _mileage_history(hass, entry)
    mileage_history._samples.clear()
    mileage_history._samples.append(
        (dt_util.utcnow() - timedelta(hours=5), 10000.0)
    )
    async_dispatcher_send(hass, signal_mileage_history_updated(entry.entry_id))
    await hass.async_block_till_done()
    state = _find_state(hass, "_idle_time")
    assert state is not None
    assert abs(float(state.state) - 5.0) < 0.05


async def test_idle_time_reads_from_latest_when_multiple_samples(
    hass: HomeAssistant,
) -> None:
    """Multiple samples → the newest (= last movement) wins."""
    entry = await _setup_full(hass)
    mileage_history = _mileage_history(hass, entry)
    mileage_history._samples.clear()
    mileage_history._samples.extend(
        [
            (dt_util.utcnow() - timedelta(hours=72), 9000.0),
            (dt_util.utcnow() - timedelta(hours=24), 9500.0),
            (dt_util.utcnow() - timedelta(hours=2), 9700.0),  # latest
        ]
    )
    async_dispatcher_send(hass, signal_mileage_history_updated(entry.entry_id))
    await hass.async_block_till_done()
    state = _find_state(hass, "_idle_time")
    assert state is not None
    assert abs(float(state.state) - 2.0) < 0.05


async def test_idle_time_unavailable_when_history_empty(
    hass: HomeAssistant,
) -> None:
    """Empty deque → unavailable, no crash."""
    entry = await _setup_full(hass)
    mileage_history = _mileage_history(hass, entry)
    mileage_history._samples.clear()
    async_dispatcher_send(hass, signal_mileage_history_updated(entry.entry_id))
    await hass.async_block_till_done()
    state = _find_state(hass, "_idle_time")
    assert state is not None
    assert state.state in ("unavailable", "unknown")


async def test_idle_time_attributes_expose_last_movement(
    hass: HomeAssistant,
) -> None:
    entry = await _setup_full(hass)
    mileage_history = _mileage_history(hass, entry)
    mileage_history._samples.clear()
    ts = dt_util.utcnow() - timedelta(hours=10)
    mileage_history._samples.append((ts, 12345.0))
    async_dispatcher_send(hass, signal_mileage_history_updated(entry.entry_id))
    await hass.async_block_till_done()
    state = _find_state(hass, "_idle_time")
    assert state is not None
    assert state.attributes["last_movement_mileage_km"] == 12345.0
    assert state.attributes["last_movement_timestamp"] == ts.isoformat()


async def test_idle_time_clamps_negative_to_zero(hass: HomeAssistant) -> None:
    """A sample with a timestamp in the (very near) future shouldn't go negative.

    Could happen on a clock skew between recorder backfill and now.
    """
    entry = await _setup_full(hass)
    mileage_history = _mileage_history(hass, entry)
    mileage_history._samples.clear()
    mileage_history._samples.append(
        (dt_util.utcnow() + timedelta(minutes=5), 9000.0)
    )
    async_dispatcher_send(hass, signal_mileage_history_updated(entry.entry_id))
    await hass.async_block_till_done()
    state = _find_state(hass, "_idle_time")
    assert state is not None
    assert float(state.state) == 0.0
