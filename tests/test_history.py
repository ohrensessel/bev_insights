"""Tests for `EntityHistory`, `MileageHistory`, `SocHistory`."""
from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.myskoda_insights.const import (
    DOMAIN,
    signal_mileage_history_updated,
)
from custom_components.myskoda_insights.tracker import MileageHistory, SocHistory


def _entry() -> MockConfigEntry:
    return MockConfigEntry(domain=DOMAIN, data={}, entry_id="test_entry")


# --------------------------------------------------------------------------- #
# delta_since / mileage clamp                                                 #
# --------------------------------------------------------------------------- #


async def test_mileage_distance_since_returns_delta(hass: HomeAssistant) -> None:
    history = MileageHistory(hass, _entry(), mileage_entity="sensor.odo")
    now = dt_util.utcnow()
    history._samples.extend(
        [
            (now - timedelta(days=5), 1000.0),
            (now - timedelta(days=2), 1050.0),
            (now, 1080.0),
        ]
    )
    # cutoff between the first two samples → baseline = 1000.0
    cutoff = now - timedelta(days=3)
    assert history.distance_since(cutoff) == 80.0


async def test_mileage_distance_since_returns_none_without_baseline(
    hass: HomeAssistant,
) -> None:
    history = MileageHistory(hass, _entry(), mileage_entity="sensor.odo")
    now = dt_util.utcnow()
    history._samples.extend([(now, 1000.0)])
    # Only sample is AFTER cutoff → no baseline.
    assert history.distance_since(now - timedelta(days=1)) is None


async def test_mileage_distance_since_clamps_negative(hass: HomeAssistant) -> None:
    """A glitched odometer drop must not produce a negative distance."""
    history = MileageHistory(hass, _entry(), mileage_entity="sensor.odo")
    now = dt_util.utcnow()
    history._samples.extend(
        [
            (now - timedelta(days=3), 1100.0),  # baseline (at or before cutoff)
            (now, 1080.0),  # glitch: reading dropped
        ]
    )
    # delta_since gives raw_delta = -20; postprocess clamps to 0.
    assert history.distance_since(now - timedelta(days=2)) == 0.0


# --------------------------------------------------------------------------- #
# SocHistory.consumed_since                                                   #
# --------------------------------------------------------------------------- #


async def test_consumed_since_sums_only_downward_steps(
    hass: HomeAssistant,
) -> None:
    """Drive 90→70, charge 70→100, drive 100→60. consumed = 20 + 40 = 60."""
    history = SocHistory(hass, _entry(), soc_entity="sensor.soc")
    now = dt_util.utcnow()
    history._samples.extend(
        [
            (now - timedelta(days=8), 90.0),  # anchor (at or before cutoff)
            (now - timedelta(days=5), 70.0),  # -20
            (now - timedelta(days=4), 100.0),  # +30 (charge, ignored)
            (now - timedelta(days=3), 60.0),  # -40
        ]
    )
    cutoff = now - timedelta(days=7)
    assert history.consumed_since(cutoff) == 60.0


async def test_consumed_since_returns_none_without_anchor(
    hass: HomeAssistant,
) -> None:
    history = SocHistory(hass, _entry(), soc_entity="sensor.soc")
    now = dt_util.utcnow()
    history._samples.extend([(now, 80.0)])
    assert history.consumed_since(now - timedelta(days=1)) is None


async def test_consumed_since_zero_when_only_charging(
    hass: HomeAssistant,
) -> None:
    history = SocHistory(hass, _entry(), soc_entity="sensor.soc")
    now = dt_util.utcnow()
    history._samples.extend(
        [
            (now - timedelta(days=5), 50.0),  # anchor before cutoff
            (now - timedelta(days=2), 80.0),  # +30
            (now - timedelta(days=1), 100.0),  # +20
        ]
    )
    assert history.consumed_since(now - timedelta(days=4)) == 0.0


# --------------------------------------------------------------------------- #
# Event-driven recording: dedup + signal firing                               #
# --------------------------------------------------------------------------- #


async def test_records_initial_sample_on_start(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.odo", "1234.0")
    history = MileageHistory(hass, _entry(), mileage_entity="sensor.odo")
    history.async_start()
    await hass.async_block_till_done()
    assert history.has_data is True
    assert history.latest_sample[1] == 1234.0
    await history.async_stop()


async def test_state_change_records_new_sample(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.odo", "1000")
    history = MileageHistory(hass, _entry(), mileage_entity="sensor.odo")
    history.async_start()
    await hass.async_block_till_done()
    initial_count = len(history._samples)

    hass.states.async_set("sensor.odo", "1050")
    await hass.async_block_till_done()
    assert len(history._samples) == initial_count + 1
    assert history.latest_sample[1] == 1050.0
    await history.async_stop()


async def test_duplicate_value_is_skipped(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.odo", "1000")
    history = MileageHistory(hass, _entry(), mileage_entity="sensor.odo")
    history.async_start()
    await hass.async_block_till_done()
    count = len(history._samples)

    # Same value → no new sample appended.
    hass.states.async_set("sensor.odo", "1000")
    await hass.async_block_till_done()
    assert len(history._samples) == count
    await history.async_stop()


# --------------------------------------------------------------------------- #
# Pruning                                                                     #
# --------------------------------------------------------------------------- #


async def test_prune_drops_samples_past_max_age(hass: HomeAssistant) -> None:
    history = MileageHistory(hass, _entry(), mileage_entity="sensor.odo")
    now = dt_util.utcnow()
    history._samples.extend(
        [
            (now - timedelta(days=20), 100.0),  # too old
            (now - timedelta(days=10), 200.0),  # too old (max_age = 8 days)
            (now - timedelta(days=3), 300.0),
            (now, 350.0),
        ]
    )
    history._prune(now)
    assert len(history._samples) == 2
    assert history._samples[0][1] == 300.0


# --------------------------------------------------------------------------- #
# Persistence: async_save → async_load round-trip                             #
# --------------------------------------------------------------------------- #


async def test_mileage_history_persists_across_reload(
    hass: HomeAssistant,
) -> None:
    """A new MileageHistory over the same entry_id reads back the samples."""
    entry = _entry()
    hass.states.async_set("sensor.odo", "1000")
    history_a = MileageHistory(hass, entry, mileage_entity="sensor.odo")
    history_a.async_start()
    await hass.async_block_till_done()

    hass.states.async_set("sensor.odo", "1050")
    await hass.async_block_till_done()
    hass.states.async_set("sensor.odo", "1100")
    await hass.async_block_till_done()
    await history_a.async_stop()

    history_b = MileageHistory(hass, entry, mileage_entity="sensor.odo")
    await history_b.async_load()
    values = [v for _, v in history_b._samples]
    assert values == [1000.0, 1050.0, 1100.0]


async def test_soc_history_persists_across_reload(hass: HomeAssistant) -> None:
    entry = _entry()
    hass.states.async_set("sensor.soc", "80")
    history_a = SocHistory(hass, entry, soc_entity="sensor.soc")
    history_a.async_start()
    await hass.async_block_till_done()

    hass.states.async_set("sensor.soc", "60")
    await hass.async_block_till_done()
    await history_a.async_stop()

    history_b = SocHistory(hass, entry, soc_entity="sensor.soc")
    await history_b.async_load()
    values = [v for _, v in history_b._samples]
    assert values == [80.0, 60.0]


async def test_async_load_drops_samples_past_max_age(
    hass: HomeAssistant,
) -> None:
    """Stale samples on disk should be pruned at load time, not carried in."""
    entry = _entry()
    history_a = MileageHistory(hass, entry, mileage_entity="sensor.odo")
    now = dt_util.utcnow()
    history_a._samples.extend(
        [
            (now - timedelta(days=30), 100.0),  # stale (max_age = 8 days)
            (now - timedelta(days=2), 200.0),
        ]
    )
    await history_a._persist()

    history_b = MileageHistory(hass, entry, mileage_entity="sensor.odo")
    await history_b.async_load()
    values = [v for _, v in history_b._samples]
    assert values == [200.0]


async def test_async_load_handles_missing_or_corrupt_data(
    hass: HomeAssistant,
) -> None:
    """A fresh entry with no Store data on disk should load cleanly to empty."""
    entry = _entry()
    history = MileageHistory(hass, entry, mileage_entity="sensor.odo")
    await history.async_load()
    assert len(history._samples) == 0
    assert history.has_data is False


# --------------------------------------------------------------------------- #
# Dispatcher signal firing                                                    #
# --------------------------------------------------------------------------- #


async def test_state_change_fires_dispatcher_signal(hass: HomeAssistant) -> None:
    """Listeners on the per-entry mileage-update signal fire on each new sample."""
    entry = _entry()
    received: list[int] = []
    unsub = async_dispatcher_connect(
        hass,
        signal_mileage_history_updated(entry.entry_id),
        lambda: received.append(1),
    )

    hass.states.async_set("sensor.odo", "100")
    history = MileageHistory(hass, entry, mileage_entity="sensor.odo")
    history.async_start()
    await hass.async_block_till_done()
    assert len(received) == 1  # initial sample on start

    hass.states.async_set("sensor.odo", "150")
    await hass.async_block_till_done()
    assert len(received) == 2

    # Same value as last — no new sample, no signal.
    hass.states.async_set("sensor.odo", "150")
    await hass.async_block_till_done()
    assert len(received) == 2

    unsub()
    await history.async_stop()


async def test_soc_history_clamps_out_of_range_values(
    hass: HomeAssistant,
) -> None:
    """SoC reads outside [0, 100] are clamped to the legal range."""
    entry = _entry()
    hass.states.async_set("sensor.soc", "150")
    history = SocHistory(hass, entry, soc_entity="sensor.soc")
    history.async_start()
    await hass.async_block_till_done()
    assert history.latest_sample[1] == 100.0

    hass.states.async_set("sensor.soc", "-5")
    await hass.async_block_till_done()
    assert history.latest_sample[1] == 0.0
    await history.async_stop()
