"""Formula-correctness tests for the window-based sensors.

`EnergyConsumedWindowSensor`, `AverageEfficiencyWindowSensor`,
`DistanceRolling7DaysSensor`, and `DistanceThisWeekSensor` all read from
`MileageHistory` / `SocHistory` rather than directly from source
entities. To exercise the formulas deterministically these tests seed
the history deques in place, fire the dispatcher signal, and assert.
"""
from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util
import pytest

from custom_components.bev_insights.const import (
    CONF_STANDSTILL_MOVEMENT_THRESHOLD_KM,
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


# --------------------------------------------------------------------------- #
# EnergyConsumedWindowSensor                                                  #
# --------------------------------------------------------------------------- #


async def test_energy_consumed_rolling_7d_basic(hass: HomeAssistant) -> None:
    """Drive 100→60 over the last 3 days. consumed = 40%."""
    entry = await _setup_full(hass)
    soc_history, _ = _histories(hass, entry)

    now = dt_util.utcnow()
    soc_history._samples.clear()
    soc_history._samples.extend(
        [
            (now - timedelta(days=8), 100.0),  # anchor before cutoff
            (now - timedelta(days=3), 100.0),
            (now - timedelta(days=1), 60.0),
        ]
    )
    async_dispatcher_send(hass, signal_soc_history_updated(entry.entry_id))
    await hass.async_block_till_done()

    # Factory: 77 × 40 / 100 = 30.8 kWh
    state = _find_state(hass, "_energy_consumed_rolling_7_days_factory")
    assert float(state.state) == pytest.approx(30.8)
    # Actual: 70 × 40 / 100 = 28.0 kWh
    state = _find_state(hass, "_energy_consumed_rolling_7_days_actual")
    assert float(state.state) == pytest.approx(28.0)


async def test_energy_consumed_ignores_charging_inside_window(
    hass: HomeAssistant,
) -> None:
    """Drive 80→40, charge 40→90, drive 90→60. Consumed = 40 + 30 = 70%."""
    entry = await _setup_full(hass)
    soc_history, _ = _histories(hass, entry)

    now = dt_util.utcnow()
    soc_history._samples.clear()
    soc_history._samples.extend(
        [
            (now - timedelta(days=8), 80.0),  # anchor
            (now - timedelta(days=5), 40.0),  # -40 driving
            (now - timedelta(days=3), 90.0),  # +50 charging (ignored)
            (now - timedelta(days=1), 60.0),  # -30 driving
        ]
    )
    async_dispatcher_send(hass, signal_soc_history_updated(entry.entry_id))
    await hass.async_block_till_done()

    # 77 × 70 / 100 = 53.9
    state = _find_state(hass, "_energy_consumed_rolling_7_days_factory")
    assert float(state.state) == pytest.approx(53.9)


async def test_energy_consumed_unavailable_without_any_samples(
    hass: HomeAssistant,
) -> None:
    """No samples at all → unavailable."""
    entry = await _setup_full(hass)
    soc_history, _ = _histories(hass, entry)
    soc_history._samples.clear()
    async_dispatcher_send(hass, signal_soc_history_updated(entry.entry_id))
    await hass.async_block_till_done()
    state = _find_state(hass, "_energy_consumed_rolling_7_days_factory")
    assert state.state in ("unavailable", "unknown")


async def test_energy_consumed_fresh_install_uses_oldest_as_anchor(
    hass: HomeAssistant,
) -> None:
    """All samples inside the window (fresh install): fall back to oldest
    as anchor and set partial_window_data=True."""
    entry = await _setup_full(hass)
    soc_history, _ = _histories(hass, entry)

    now = dt_util.utcnow()
    soc_history._samples.clear()
    soc_history._samples.extend(
        [
            (now - timedelta(hours=2), 80.0),  # oldest → anchor (fallback)
            (now - timedelta(hours=1), 60.0),  # -20 driving
        ]
    )
    async_dispatcher_send(hass, signal_soc_history_updated(entry.entry_id))
    await hass.async_block_till_done()

    # consumed = 20%; factory: 77 × 20 / 100 = 15.4 kWh
    state = _find_state(hass, "_energy_consumed_rolling_7_days_factory")
    assert float(state.state) == pytest.approx(15.4)
    assert state.attributes["partial_window_data"] is True


async def test_energy_consumed_this_week_is_total_with_last_reset(
    hass: HomeAssistant,
) -> None:
    """LTS classification differs between the two window shapes.

    `this_week` is `device_class=ENERGY` + `state_class=TOTAL` with
    `last_reset` pinned to the current week's Monday-00:00 — clean
    per-week sum LTS plus Energy Dashboard eligibility.

    `rolling_7_days` drops the ENERGY device class so MEASUREMENT is
    allowed (HA rejects ENERGY+MEASUREMENT, and ENERGY+TOTAL on a
    sliding window would lie to the Energy Dashboard). MEASUREMENT
    gives the user min/max/mean LTS for trending the rolling figure.
    """
    entry = await _setup_full(hass)
    soc_history, _ = _histories(hass, entry)

    now = dt_util.utcnow()
    soc_history._samples.clear()
    soc_history._samples.extend(
        [
            (now - timedelta(days=8), 80.0),
            (now - timedelta(hours=1), 60.0),
        ]
    )
    async_dispatcher_send(hass, signal_soc_history_updated(entry.entry_id))
    await hass.async_block_till_done()

    weekly = _find_state(hass, "_energy_consumed_this_week_factory")
    rolling = _find_state(hass, "_energy_consumed_rolling_7_days_factory")

    # this_week: ENERGY device class + TOTAL state class + last_reset.
    assert weekly.attributes["device_class"] == "energy"
    assert weekly.attributes["state_class"] == "total"
    assert "last_reset" in weekly.attributes
    assert weekly.attributes["last_reset"] == weekly.attributes["window_start"]

    # rolling_7_days: no ENERGY device class; MEASUREMENT state class.
    assert rolling.attributes.get("device_class") is None
    assert rolling.attributes["state_class"] == "measurement"


async def test_energy_consumed_unavailable_when_actual_capacity_missing(
    hass: HomeAssistant,
) -> None:
    """Drop the actual-capacity helper and the actual-variant sensor goes
    unavailable, while the factory variant keeps working."""
    entry = await _setup_full(hass)
    soc_history, _ = _histories(hass, entry)

    now = dt_util.utcnow()
    soc_history._samples.clear()
    soc_history._samples.extend(
        [(now - timedelta(days=8), 100.0), (now - timedelta(days=1), 60.0)]
    )

    hass.states.async_set(ACTUAL_CAPACITY_ENTITY, "unavailable")
    async_dispatcher_send(hass, signal_soc_history_updated(entry.entry_id))
    await hass.async_block_till_done()

    state = _find_state(hass, "_energy_consumed_rolling_7_days_actual")
    assert state.state in ("unavailable", "unknown")
    state = _find_state(hass, "_energy_consumed_rolling_7_days_factory")
    assert float(state.state) == pytest.approx(30.8)


# --------------------------------------------------------------------------- #
# DistanceRolling7DaysSensor                                                  #
# --------------------------------------------------------------------------- #


async def test_distance_rolling_7d_math(hass: HomeAssistant) -> None:
    entry = await _setup_full(hass)
    _, mileage_history = _histories(hass, entry)

    now = dt_util.utcnow()
    mileage_history._samples.clear()
    mileage_history._samples.extend(
        [
            (now - timedelta(days=8), 10000.0),  # anchor before cutoff
            (now - timedelta(days=4), 10100.0),
            (now, 10250.0),
        ]
    )
    async_dispatcher_send(hass, signal_mileage_history_updated(entry.entry_id))
    await hass.async_block_till_done()

    state = _find_state(hass, "_distance_rolling_7_days")
    assert float(state.state) == pytest.approx(250.0)


async def test_distance_rolling_7d_unavailable_without_anchor(
    hass: HomeAssistant,
) -> None:
    entry = await _setup_full(hass)
    _, mileage_history = _histories(hass, entry)
    mileage_history._samples.clear()
    async_dispatcher_send(hass, signal_mileage_history_updated(entry.entry_id))
    await hass.async_block_till_done()
    state = _find_state(hass, "_distance_rolling_7_days")
    assert state.state in ("unavailable", "unknown")


# --------------------------------------------------------------------------- #
# DistanceThisWeekSensor                                                      #
# --------------------------------------------------------------------------- #


async def test_distance_this_week_is_total_with_last_reset(
    hass: HomeAssistant,
) -> None:
    """Same shape as the weekly energy sensor: TOTAL + last_reset pointing
    at the current week's Monday-00:00, for clean LTS bookkeeping."""
    entry = await _setup_full(hass)
    _, mileage_history = _histories(hass, entry)

    now = dt_util.utcnow()
    mileage_history._samples.clear()
    mileage_history._samples.extend(
        [
            (now - timedelta(days=30), 10000.0),
            (now, 10042.5),
        ]
    )
    hass.states.async_set(MILEAGE_ENTITY, "10042.5", {"unit_of_measurement": "km"})
    await hass.async_block_till_done()

    state = _find_state(hass, "_distance_this_week")
    assert state.attributes["state_class"] == "total"
    assert "last_reset" in state.attributes
    assert state.attributes["last_reset"] == state.attributes["week_start"]


async def test_distance_this_week_with_pre_week_anchor(
    hass: HomeAssistant,
) -> None:
    """A sample older than `week_start` anchors the figure; partial_week_data
    is False. `DistanceThisWeekSensor` listens to the mileage entity directly,
    so we trigger recompute by bumping its state."""
    entry = await _setup_full(hass)
    _, mileage_history = _histories(hass, entry)

    now = dt_util.utcnow()
    mileage_history._samples.clear()
    # 30 days back guarantees we sit before any reasonable week_start, so the
    # baseline-aware path is exercised regardless of which weekday `now` is.
    mileage_history._samples.extend(
        [
            (now - timedelta(days=30), 10000.0),
            (now, 10042.5),
        ]
    )
    hass.states.async_set(MILEAGE_ENTITY, "10042.5", {"unit_of_measurement": "km"})
    await hass.async_block_till_done()

    state = _find_state(hass, "_distance_this_week")
    assert float(state.state) == pytest.approx(42.5)
    assert state.attributes["partial_week_data"] is False


async def test_distance_this_week_fresh_install_sets_partial_flag(
    hass: HomeAssistant,
) -> None:
    """No sample older than week_start → fall back to (current − oldest)
    and flag the figure as partial."""
    entry = await _setup_full(hass)
    _, mileage_history = _histories(hass, entry)

    now = dt_util.utcnow()
    mileage_history._samples.clear()
    # Both samples sit inside the current week (a few minutes apart).
    mileage_history._samples.extend(
        [
            (now - timedelta(minutes=10), 10000.0),
            (now, 10010.0),
        ]
    )
    hass.states.async_set(MILEAGE_ENTITY, "10010", {"unit_of_measurement": "km"})
    await hass.async_block_till_done()

    state = _find_state(hass, "_distance_this_week")
    # Fallback path: current − oldest = 10010 − 10000 = 10.0
    assert float(state.state) == pytest.approx(10.0)
    assert state.attributes["partial_week_data"] is True


# --------------------------------------------------------------------------- #
# AverageEfficiencyWindowSensor                                               #
# --------------------------------------------------------------------------- #


async def test_avg_efficiency_window_kwh_per_100km(hass: HomeAssistant) -> None:
    """Drive 100→50 over 200 km in the last 5 days.
    kWh consumed = 50 × 77 / 100 = 38.5; kWh/100 km = 38.5 / 200 × 100 = 19.25."""
    entry = await _setup_full(hass)
    soc_history, mileage_history = _histories(hass, entry)

    now = dt_util.utcnow()
    soc_history._samples.clear()
    soc_history._samples.extend(
        [
            (now - timedelta(days=8), 100.0),
            (now - timedelta(days=5), 100.0),
            (now - timedelta(days=1), 50.0),
        ]
    )
    mileage_history._samples.clear()
    mileage_history._samples.extend(
        [
            (now - timedelta(days=8), 10000.0),
            (now - timedelta(days=5), 10000.0),
            (now - timedelta(days=1), 10200.0),
        ]
    )
    async_dispatcher_send(hass, signal_soc_history_updated(entry.entry_id))
    async_dispatcher_send(hass, signal_mileage_history_updated(entry.entry_id))
    await hass.async_block_till_done()

    state = _find_state(
        hass, "_avg_efficiency_rolling_7_days_factory_kwh_per_100km"
    )
    assert float(state.state) == pytest.approx(19.25)


async def test_avg_efficiency_window_km_per_kwh(hass: HomeAssistant) -> None:
    """Same scenario, opposite unit. km/kWh = 200 / 38.5 ≈ 5.195."""
    entry = await _setup_full(hass)
    soc_history, mileage_history = _histories(hass, entry)

    now = dt_util.utcnow()
    soc_history._samples.clear()
    soc_history._samples.extend(
        [
            (now - timedelta(days=8), 100.0),
            (now - timedelta(days=1), 50.0),
        ]
    )
    mileage_history._samples.clear()
    mileage_history._samples.extend(
        [
            (now - timedelta(days=8), 10000.0),
            (now - timedelta(days=1), 10200.0),
        ]
    )
    async_dispatcher_send(hass, signal_soc_history_updated(entry.entry_id))
    async_dispatcher_send(hass, signal_mileage_history_updated(entry.entry_id))
    await hass.async_block_till_done()

    state = _find_state(hass, "_avg_efficiency_rolling_7_days_factory_km_per_kwh")
    assert float(state.state) == pytest.approx(200 / 38.5, rel=1e-3)


async def test_avg_efficiency_window_unavailable_without_data(
    hass: HomeAssistant,
) -> None:
    entry = await _setup_full(hass)
    soc_history, mileage_history = _histories(hass, entry)
    soc_history._samples.clear()
    mileage_history._samples.clear()
    async_dispatcher_send(hass, signal_soc_history_updated(entry.entry_id))
    async_dispatcher_send(hass, signal_mileage_history_updated(entry.entry_id))
    await hass.async_block_till_done()
    state = _find_state(
        hass, "_avg_efficiency_rolling_7_days_factory_kwh_per_100km"
    )
    assert state.state in ("unavailable", "unknown")


async def test_avg_efficiency_window_fresh_install_uses_oldest_as_anchor(
    hass: HomeAssistant,
) -> None:
    """All samples inside the window: compute from oldest available baseline
    and set partial_window_data=True."""
    entry = await _setup_full(hass)
    soc_history, mileage_history = _histories(hass, entry)

    now = dt_util.utcnow()
    soc_history._samples.clear()
    soc_history._samples.extend(
        [
            (now - timedelta(hours=2), 80.0),  # oldest → anchor (fallback)
            (now - timedelta(hours=1), 60.0),  # -20 driving
        ]
    )
    mileage_history._samples.clear()
    mileage_history._samples.extend(
        [
            (now - timedelta(hours=2), 10000.0),  # oldest → anchor (fallback)
            (now - timedelta(hours=1), 10100.0),  # +100 km
        ]
    )
    async_dispatcher_send(hass, signal_soc_history_updated(entry.entry_id))
    async_dispatcher_send(hass, signal_mileage_history_updated(entry.entry_id))
    await hass.async_block_till_done()

    # consumed = 20% × 77 = 15.4 kWh; kWh/100 km = 15.4 / 100 × 100 = 15.4
    state = _find_state(
        hass, "_avg_efficiency_rolling_7_days_factory_kwh_per_100km"
    )
    assert float(state.state) == pytest.approx(15.4)
    assert state.attributes["partial_window_data"] is True


# --------------------------------------------------------------------------- #
# StandstillConsumptionWindowSensor                                           #
# --------------------------------------------------------------------------- #


async def test_standstill_consumption_pure_parked(hass: HomeAssistant) -> None:
    """SoC drops 10% while mileage is flat → full drop counted as standstill."""
    entry = await _setup_full(hass)
    soc_history, mileage_history = _histories(hass, entry)

    now = dt_util.utcnow()
    soc_history._samples.clear()
    soc_history._samples.extend(
        [
            (now - timedelta(days=8), 80.0),  # anchor
            (now - timedelta(days=3), 80.0),
            (now - timedelta(days=1), 70.0),  # -10% while parked
        ]
    )
    mileage_history._samples.clear()
    mileage_history._samples.extend(
        [
            (now - timedelta(days=8), 10000.0),
            (now - timedelta(days=1), 10000.0),  # no movement
        ]
    )
    async_dispatcher_send(hass, signal_soc_history_updated(entry.entry_id))
    await hass.async_block_till_done()

    # factory: 77 × 10 / 100 = 7.7 kWh; actual: 70 × 10 / 100 = 7.0 kWh
    state = _find_state(hass, "_standstill_consumption_rolling_7_days_factory")
    assert float(state.state) == pytest.approx(7.7)
    state = _find_state(hass, "_standstill_consumption_rolling_7_days_actual")
    assert float(state.state) == pytest.approx(7.0)


async def test_standstill_consumption_pure_driving(hass: HomeAssistant) -> None:
    """SoC drops 20% while mileage advances → nothing counted as standstill."""
    entry = await _setup_full(hass)
    soc_history, mileage_history = _histories(hass, entry)

    now = dt_util.utcnow()
    soc_history._samples.clear()
    soc_history._samples.extend(
        [
            (now - timedelta(days=8), 80.0),
            (now - timedelta(days=1), 60.0),  # -20% while driving
        ]
    )
    mileage_history._samples.clear()
    mileage_history._samples.extend(
        [
            (now - timedelta(days=8), 10000.0),
            (now - timedelta(days=1), 10150.0),  # +150 km
        ]
    )
    async_dispatcher_send(hass, signal_soc_history_updated(entry.entry_id))
    await hass.async_block_till_done()

    state = _find_state(hass, "_standstill_consumption_rolling_7_days_factory")
    assert float(state.state) == pytest.approx(0.0)


async def test_standstill_consumption_mixed(hass: HomeAssistant) -> None:
    """Two intervals: one parked (5% SoC), one driving (15% SoC).
    Only the parked interval contributes to standstill."""
    entry = await _setup_full(hass)
    soc_history, mileage_history = _histories(hass, entry)

    now = dt_util.utcnow()
    soc_history._samples.clear()
    soc_history._samples.extend(
        [
            (now - timedelta(days=8), 90.0),  # anchor
            (now - timedelta(days=4), 85.0),  # -5% parked
            (now - timedelta(days=2), 70.0),  # -15% driving
        ]
    )
    mileage_history._samples.clear()
    mileage_history._samples.extend(
        [
            (now - timedelta(days=8), 10000.0),
            (now - timedelta(days=4), 10000.0),  # parked
            (now - timedelta(days=2), 10100.0),  # drove 100 km
        ]
    )
    async_dispatcher_send(hass, signal_soc_history_updated(entry.entry_id))
    await hass.async_block_till_done()

    # only 5% standstill; factory: 77 × 5 / 100 = 3.85 kWh
    state = _find_state(hass, "_standstill_consumption_rolling_7_days_factory")
    assert float(state.state) == pytest.approx(3.85)


async def test_standstill_consumption_unavailable_without_mileage(
    hass: HomeAssistant,
) -> None:
    """No mileage samples → can't classify intervals → unavailable."""
    entry = await _setup_full(hass)
    soc_history, mileage_history = _histories(hass, entry)

    now = dt_util.utcnow()
    soc_history._samples.clear()
    soc_history._samples.extend(
        [(now - timedelta(days=8), 80.0), (now - timedelta(days=1), 70.0)]
    )
    mileage_history._samples.clear()
    async_dispatcher_send(hass, signal_soc_history_updated(entry.entry_id))
    await hass.async_block_till_done()

    state = _find_state(hass, "_standstill_consumption_rolling_7_days_factory")
    assert state.state in ("unavailable", "unknown")


async def test_standstill_consumption_custom_threshold(
    hass: HomeAssistant,
) -> None:
    """With a 2 km threshold, a 1.5 km drive counts as standstill.
    With the default 0.1 km threshold it would be excluded."""
    hass.states.async_set(SOC_ENTITY, "80")
    hass.states.async_set(RANGE_ENTITY, "300", {"unit_of_measurement": "km"})
    hass.states.async_set(MILEAGE_ENTITY, "10000", {"unit_of_measurement": "km"})
    hass.states.async_set(CHARGING_ENTITY, "off")
    hass.states.async_set(ACTUAL_CAPACITY_ENTITY, "70.0")
    entry = make_entry(options={CONF_STANDSTILL_MOVEMENT_THRESHOLD_KM: 2.0})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    domain_data = hass.data[DOMAIN][entry.entry_id]
    soc_history = domain_data["soc_history"]
    mileage_history = domain_data["mileage_history"]

    now = dt_util.utcnow()
    soc_history._samples.clear()
    soc_history._samples.extend(
        [
            (now - timedelta(days=8), 80.0),
            (now - timedelta(days=1), 70.0),  # -10% with 1.5 km movement
        ]
    )
    mileage_history._samples.clear()
    mileage_history._samples.extend(
        [
            (now - timedelta(days=8), 10000.0),
            (now - timedelta(days=1), 10001.5),  # only 1.5 km → < 2 km threshold
        ]
    )
    async_dispatcher_send(hass, signal_soc_history_updated(entry.entry_id))
    await hass.async_block_till_done()

    # 1.5 km < 2.0 km threshold → whole 10% drop counted as standstill
    # factory: 77 × 10 / 100 = 7.7 kWh
    state = _find_state(hass, "_standstill_consumption_rolling_7_days_factory")
    assert float(state.state) == pytest.approx(7.7)


async def test_standstill_consumption_this_week_is_total(
    hass: HomeAssistant,
) -> None:
    """`this_week` variant declares state_class=TOTAL with last_reset at Monday."""
    entry = await _setup_full(hass)
    soc_history, mileage_history = _histories(hass, entry)

    now = dt_util.utcnow()
    soc_history._samples.clear()
    soc_history._samples.extend(
        [(now - timedelta(days=8), 80.0), (now - timedelta(hours=1), 78.0)]
    )
    mileage_history._samples.clear()
    mileage_history._samples.extend(
        [(now - timedelta(days=8), 10000.0), (now - timedelta(hours=1), 10000.0)]
    )
    async_dispatcher_send(hass, signal_soc_history_updated(entry.entry_id))
    await hass.async_block_till_done()

    state = _find_state(hass, "_standstill_consumption_this_week_factory")
    assert state.attributes["state_class"] == "total"
    assert "last_reset" in state.attributes
    assert state.attributes["last_reset"] == state.attributes["window_start"]


async def test_avg_efficiency_window_actual_capacity_variant_uses_actual(
    hass: HomeAssistant,
) -> None:
    """Actual-variant must use the live actual capacity helper (70 kWh here)."""
    entry = await _setup_full(hass)
    soc_history, mileage_history = _histories(hass, entry)

    now = dt_util.utcnow()
    soc_history._samples.clear()
    soc_history._samples.extend(
        [
            (now - timedelta(days=8), 100.0),
            (now - timedelta(days=1), 50.0),
        ]
    )
    mileage_history._samples.clear()
    mileage_history._samples.extend(
        [
            (now - timedelta(days=8), 10000.0),
            (now - timedelta(days=1), 10200.0),
        ]
    )
    async_dispatcher_send(hass, signal_soc_history_updated(entry.entry_id))
    async_dispatcher_send(hass, signal_mileage_history_updated(entry.entry_id))
    await hass.async_block_till_done()

    # consumed = 50 × 70 / 100 = 35 kWh; kWh/100 km = 35 / 200 × 100 = 17.5
    state = _find_state(
        hass, "_avg_efficiency_rolling_7_days_actual_kwh_per_100km"
    )
    assert float(state.state) == pytest.approx(17.5)
