"""Formula tests for the week-over-week delta sensors.

Place samples *relative to the current week boundaries* so the test is
robust to whatever day-of-week it actually runs on. The math the sensor
performs:

    this_week_so_far  = mileage(now) - mileage(this_week_start)
    last_week_same_pt = mileage(last_week_end) - mileage(last_week_start)
    delta             = this_week_so_far - last_week_same_pt

We seed three samples — one a touch before `last_week_start`, one a
touch before `last_week_end` (which is also before `this_week_start`),
and the live state for `now` — and verify the resulting delta.
"""
from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from custom_components.bev_insights.const import DOMAIN, signal_soc_history_updated
from custom_components.bev_insights.sensor.formulas import _local_week_start

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


def _histories(hass: HomeAssistant, entry):
    domain = hass.data[DOMAIN][entry.entry_id]
    return domain["soc_history"], domain["mileage_history"]


def _week_anchors(hass: HomeAssistant):
    """Return (this_week_start, last_week_start, last_week_end, now)."""
    now = dt_util.utcnow()
    this_week_start = _local_week_start(now, hass)
    last_week_start = this_week_start - timedelta(days=7)
    last_week_end = now - timedelta(days=7)
    return this_week_start, last_week_start, last_week_end, now


# --------------------------------------------------------------------------- #
# DistanceWeekDeltaSensor                                                     #
# --------------------------------------------------------------------------- #


async def test_distance_week_delta_positive_when_drove_more(
    hass: HomeAssistant,
) -> None:
    """Drove 400 km this week, 300 km in the equivalent last-week window → +100."""
    entry = await _setup_full(hass)
    _, mileage_history = _histories(hass, entry)
    _, lws, lwe, _ = _week_anchors(hass)

    mileage_history._samples.clear()
    # X at last_week_start - 2h, Y at last_week_end - 2h, current = W.
    # Sensor formula: delta = (W - Y) - (Y - X) = W - 2Y + X
    # With X=10000, Y=10300, W=10700 → delta = 100.
    mileage_history._samples.extend(
        [
            (lws - timedelta(hours=2), 10000.0),
            (lwe - timedelta(hours=2), 10300.0),
        ]
    )
    hass.states.async_set(
        MILEAGE_ENTITY, "10700", {"unit_of_measurement": "km"}
    )
    await hass.async_block_till_done()
    state = _find_state(hass, "_distance_week_delta")
    assert state is not None
    assert abs(float(state.state) - 100.0) < 0.2


async def test_distance_week_delta_negative_when_drove_less(
    hass: HomeAssistant,
) -> None:
    entry = await _setup_full(hass)
    _, mileage_history = _histories(hass, entry)
    _, lws, lwe, _ = _week_anchors(hass)
    mileage_history._samples.clear()
    # X=10000, Y=10500 (500 last week), W=10600 (100 this week) → delta = -400
    mileage_history._samples.extend(
        [
            (lws - timedelta(hours=2), 10000.0),
            (lwe - timedelta(hours=2), 10500.0),
        ]
    )
    hass.states.async_set(
        MILEAGE_ENTITY, "10600", {"unit_of_measurement": "km"}
    )
    await hass.async_block_till_done()
    state = _find_state(hass, "_distance_week_delta")
    assert state is not None
    assert abs(float(state.state) - (-400.0)) < 0.2


async def test_distance_week_delta_unavailable_without_history(
    hass: HomeAssistant,
) -> None:
    entry = await _setup_full(hass)
    _, mileage_history = _histories(hass, entry)
    mileage_history._samples.clear()
    hass.states.async_set(
        MILEAGE_ENTITY, "11000", {"unit_of_measurement": "km"}
    )
    await hass.async_block_till_done()
    state = _find_state(hass, "_distance_week_delta")
    assert state is not None
    assert state.state in ("unavailable", "unknown")


async def test_distance_week_delta_unavailable_when_no_pre_last_week_sample(
    hass: HomeAssistant,
) -> None:
    """Without a sample before last_week_start the delta is undefined."""
    entry = await _setup_full(hass)
    _, mileage_history = _histories(hass, entry)
    _, lws, _, _ = _week_anchors(hass)
    # All samples AFTER last_week_start → value_at(last_week_start) is None.
    mileage_history._samples.clear()
    mileage_history._samples.extend(
        [
            (lws + timedelta(hours=2), 10000.0),
            (dt_util.utcnow() - timedelta(hours=1), 10200.0),
        ]
    )
    hass.states.async_set(
        MILEAGE_ENTITY, "10200", {"unit_of_measurement": "km"}
    )
    await hass.async_block_till_done()
    state = _find_state(hass, "_distance_week_delta")
    assert state is not None
    assert state.state in ("unavailable", "unknown")


# --------------------------------------------------------------------------- #
# EnergyConsumedWeekDeltaSensor                                               #
# --------------------------------------------------------------------------- #


async def test_energy_week_delta_negative_when_consumed_less(
    hass: HomeAssistant,
) -> None:
    """Last week: 40 pp consumed. This week so far: 20 pp consumed.

    Delta = (20 - 40) × 70 / 100 = -14 kWh.
    """
    entry = await _setup_full(hass)
    soc_history, _ = _histories(hass, entry)
    tws, lws, lwe, _ = _week_anchors(hass)

    soc_history._samples.clear()
    # 90 → 50 across last-week window (drop 40), 50 → 30 this week (drop 20)
    soc_history._samples.extend(
        [
            (lws - timedelta(hours=2), 90.0),
            (lwe - timedelta(hours=2), 50.0),  # 40 pp drop "last week"
            (tws + timedelta(hours=2), 50.0),  # no change at boundary
            (dt_util.utcnow() - timedelta(hours=1), 30.0),  # 20 pp this week
        ]
    )
    async_dispatcher_send(hass, signal_soc_history_updated(entry.entry_id))
    await hass.async_block_till_done()
    state = _find_state(hass, "_energy_consumed_week_delta_actual")
    assert state is not None
    assert abs(float(state.state) - (-14.0)) < 0.5


async def test_energy_week_delta_unavailable_without_history(
    hass: HomeAssistant,
) -> None:
    entry = await _setup_full(hass)
    soc_history, _ = _histories(hass, entry)
    soc_history._samples.clear()
    async_dispatcher_send(hass, signal_soc_history_updated(entry.entry_id))
    await hass.async_block_till_done()
    state = _find_state(hass, "_energy_consumed_week_delta_actual")
    assert state is not None
    assert state.state in ("unavailable", "unknown")


async def test_energy_week_delta_factory_uses_factory_capacity(
    hass: HomeAssistant,
) -> None:
    """Factory variant multiplies by 77 kWh (not the 70 of the actual helper)."""
    entry = await _setup_full(hass)
    soc_history, _ = _histories(hass, entry)
    tws, lws, lwe, _ = _week_anchors(hass)
    soc_history._samples.clear()
    # Last week: 30 pp drop. This week so far: 10 pp drop. Delta SoC = -20 pp.
    # Factory delta kWh = -20 × 77 / 100 = -15.4 kWh.
    soc_history._samples.extend(
        [
            (lws - timedelta(hours=2), 90.0),
            (lwe - timedelta(hours=2), 60.0),
            (tws + timedelta(hours=2), 60.0),
            (dt_util.utcnow() - timedelta(hours=1), 50.0),
        ]
    )
    async_dispatcher_send(hass, signal_soc_history_updated(entry.entry_id))
    await hass.async_block_till_done()
    state = _find_state(hass, "_energy_consumed_week_delta_factory")
    assert state is not None
    assert abs(float(state.state) - (-15.4)) < 0.5
