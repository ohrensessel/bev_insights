"""Tests for `EntityHistory`, `MileageHistory`, `SocHistory`."""
from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.myskoda_insights.const import DOMAIN
from custom_components.myskoda_insights.tracker import (
    MileageHistory,
    SocHistory,
)


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
