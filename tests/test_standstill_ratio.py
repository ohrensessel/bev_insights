"""Tests for StandstillRatioWindowSensor."""
from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from custom_components.bev_insights.const import (
    DOMAIN,
    signal_mileage_history_updated,
    signal_soc_history_updated,
)

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


async def _pump(hass: HomeAssistant, entry_id: str) -> None:
    async_dispatcher_send(hass, signal_soc_history_updated(entry_id))
    async_dispatcher_send(hass, signal_mileage_history_updated(entry_id))
    await hass.async_block_till_done()


async def test_standstill_ratio_unavailable_no_history(hass: HomeAssistant) -> None:
    """Unavailable when histories are empty."""
    entry = await _setup(hass)
    state = hass.states.get(
        _entity_id(hass, entry.entry_id, "_standstill_ratio_rolling_7_days")
    )
    assert state is not None
    assert state.state == "unavailable"


async def test_standstill_ratio_all_parked(hass: HomeAssistant) -> None:
    """100 % ratio when every SoC drop occurred while parked."""
    entry = await _setup(hass)
    soc_h = hass.data[DOMAIN][entry.entry_id]["soc_history"]
    mil_h = hass.data[DOMAIN][entry.entry_id]["mileage_history"]
    now = dt_util.utcnow()

    soc_h._samples.clear()
    soc_h._samples.append((now - timedelta(days=3), 80.0))
    soc_h._samples.append((now, 60.0))  # 20 % drop

    mil_h._samples.clear()
    mil_h._samples.append((now - timedelta(days=3), 10000.0))
    mil_h._samples.append((now, 10000.0))  # no movement

    await _pump(hass, entry.entry_id)

    state = hass.states.get(
        _entity_id(hass, entry.entry_id, "_standstill_ratio_rolling_7_days")
    )
    assert state is not None
    assert state.state != "unavailable"
    assert float(state.state) == 100.0


async def test_standstill_ratio_all_driving(hass: HomeAssistant) -> None:
    """0 % ratio when every SoC drop occurred while driving."""
    entry = await _setup(hass)
    soc_h = hass.data[DOMAIN][entry.entry_id]["soc_history"]
    mil_h = hass.data[DOMAIN][entry.entry_id]["mileage_history"]
    now = dt_util.utcnow()

    soc_h._samples.clear()
    soc_h._samples.append((now - timedelta(days=3), 80.0))
    soc_h._samples.append((now, 60.0))  # 20 % drop while driving

    mil_h._samples.clear()
    mil_h._samples.append((now - timedelta(days=3), 10000.0))
    mil_h._samples.append((now, 10200.0))  # 200 km driven

    await _pump(hass, entry.entry_id)

    state = hass.states.get(
        _entity_id(hass, entry.entry_id, "_standstill_ratio_rolling_7_days")
    )
    assert state is not None
    assert state.state != "unavailable"
    assert float(state.state) == 0.0


async def test_standstill_ratio_mixed(hass: HomeAssistant) -> None:
    """Ratio is correct for mixed driving and parked intervals."""
    entry = await _setup(hass)
    soc_h = hass.data[DOMAIN][entry.entry_id]["soc_history"]
    mil_h = hass.data[DOMAIN][entry.entry_id]["mileage_history"]
    now = dt_util.utcnow()

    # 10 % parked drop, 10 % driving drop → ratio = 50 %
    t0 = now - timedelta(days=5)
    t1 = now - timedelta(days=4)
    t2 = now - timedelta(days=3)
    t3 = now - timedelta(days=2)

    soc_h._samples.clear()
    soc_h._samples.append((t0, 80.0))
    soc_h._samples.append((t1, 70.0))  # −10 % parked
    soc_h._samples.append((t2, 70.0))  # flat
    soc_h._samples.append((t3, 60.0))  # −10 % driving

    mil_h._samples.clear()
    mil_h._samples.append((t0, 10000.0))
    mil_h._samples.append((t1, 10000.0))  # still parked
    mil_h._samples.append((t2, 10000.0))
    mil_h._samples.append((t3, 10100.0))  # 100 km driven

    await _pump(hass, entry.entry_id)

    state = hass.states.get(
        _entity_id(hass, entry.entry_id, "_standstill_ratio_rolling_7_days")
    )
    assert state is not None
    assert state.state != "unavailable"
    assert float(state.state) == 50.0
