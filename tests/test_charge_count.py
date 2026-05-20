"""Tests for ChargeCountWindowSensor and SocHistory.charge_count_since."""
from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from custom_components.bev_insights.const import DOMAIN, signal_soc_history_updated

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


def _entity_id(hass: HomeAssistant, entry_id: str, suffix: str) -> str:
    return next(
        e.entity_id
        for e in hass.data["entity_registry"].entities.values()
        if e.config_entry_id == entry_id and e.unique_id.endswith(suffix)
    )


async def test_charge_count_zero_when_no_charges(hass: HomeAssistant) -> None:
    """With only downward SoC steps the count is 0."""
    entry = await _setup(hass)

    soc_history = hass.data[DOMAIN][entry.entry_id]["soc_history"]
    now = dt_util.utcnow()
    soc_history._samples.clear()
    soc_history._samples.append((now - timedelta(days=5), 80.0))
    soc_history._samples.append((now - timedelta(days=4), 70.0))
    soc_history._samples.append((now, 60.0))

    async_dispatcher_send(hass, signal_soc_history_updated(entry.entry_id))
    await hass.async_block_till_done()

    state = hass.states.get(
        _entity_id(hass, entry.entry_id, "_charge_count_rolling_7_days")
    )
    assert state is not None
    assert state.state == "0"


async def test_charge_count_one_session(hass: HomeAssistant) -> None:
    """One upward run ≥ 5 % counts as one charge."""
    entry = await _setup(hass)

    soc_history = hass.data[DOMAIN][entry.entry_id]["soc_history"]
    now = dt_util.utcnow()
    soc_history._samples.clear()
    # Discharge → charge → discharge
    soc_history._samples.append((now - timedelta(days=6), 80.0))
    soc_history._samples.append((now - timedelta(days=5), 50.0))
    soc_history._samples.append((now - timedelta(days=4), 90.0))  # +40%: one session
    soc_history._samples.append((now, 60.0))

    async_dispatcher_send(hass, signal_soc_history_updated(entry.entry_id))
    await hass.async_block_till_done()

    state = hass.states.get(
        _entity_id(hass, entry.entry_id, "_charge_count_rolling_7_days")
    )
    assert state is not None
    assert state.state == "1"


async def test_charge_count_two_sessions(hass: HomeAssistant) -> None:
    """Two distinct upward runs count as two charges."""
    entry = await _setup(hass)

    soc_history = hass.data[DOMAIN][entry.entry_id]["soc_history"]
    now = dt_util.utcnow()
    soc_history._samples.clear()
    soc_history._samples.append((now - timedelta(days=6), 80.0))
    soc_history._samples.append((now - timedelta(days=5), 40.0))
    soc_history._samples.append((now - timedelta(days=4), 85.0))  # first charge
    soc_history._samples.append((now - timedelta(days=3), 55.0))
    soc_history._samples.append((now - timedelta(days=2), 90.0))  # second charge
    soc_history._samples.append((now, 70.0))

    async_dispatcher_send(hass, signal_soc_history_updated(entry.entry_id))
    await hass.async_block_till_done()

    state = hass.states.get(
        _entity_id(hass, entry.entry_id, "_charge_count_rolling_7_days")
    )
    assert state is not None
    assert state.state == "2"


async def test_charge_count_noise_ignored(hass: HomeAssistant) -> None:
    """Upward ticks smaller than 5 % are ignored."""
    entry = await _setup(hass)

    soc_history = hass.data[DOMAIN][entry.entry_id]["soc_history"]
    now = dt_util.utcnow()
    soc_history._samples.clear()
    soc_history._samples.append((now - timedelta(days=3), 60.0))
    soc_history._samples.append((now - timedelta(days=2), 62.0))  # +2 %: noise
    soc_history._samples.append((now, 58.0))

    async_dispatcher_send(hass, signal_soc_history_updated(entry.entry_id))
    await hass.async_block_till_done()

    state = hass.states.get(
        _entity_id(hass, entry.entry_id, "_charge_count_rolling_7_days")
    )
    assert state is not None
    assert state.state == "0"


async def test_charge_count_this_week_shape(hass: HomeAssistant) -> None:
    """this_week variant has state_class TOTAL and last_reset set."""
    entry = await _setup(hass)

    entity_id = _entity_id(hass, entry.entry_id, "_charge_count_this_week")
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.attributes.get("state_class") == "total"
    assert state.attributes.get("last_reset") is not None
