"""Tests for `EntityHistory`, `MileageHistory`, `SocHistory`."""
from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bev_insights.const import (
    DOMAIN,
    signal_mileage_history_updated,
    signal_soc_history_updated,
)
from custom_components.bev_insights.tracker import (
    EntityHistory,
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


async def test_mileage_distance_since_falls_back_to_oldest_when_no_baseline(
    hass: HomeAssistant,
) -> None:
    history = MileageHistory(hass, _entry(), mileage_entity="sensor.odo")
    now = dt_util.utcnow()
    history._samples.extend([(now, 1000.0)])
    # Only sample is AFTER cutoff → falls back to oldest (same point → 0 km).
    assert history.distance_since(now - timedelta(days=1)) == 0.0


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


async def test_consumed_since_falls_back_to_oldest_when_no_anchor(
    hass: HomeAssistant,
) -> None:
    history = SocHistory(hass, _entry(), soc_entity="sensor.soc")
    now = dt_util.utcnow()
    history._samples.extend([(now, 80.0)])
    # Only sample is AFTER cutoff → falls back to oldest as anchor (no
    # subsequent samples to consume from → 0.0, not None).
    assert history.consumed_since(now - timedelta(days=1)) == 0.0


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
    # Pin max_age explicitly so the test doesn't drift when the default
    # retention changes (e.g. 8 → 15 days in v1.6).
    history = MileageHistory(
        hass, _entry(), mileage_entity="sensor.odo", max_age_days=8
    )
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


async def test_max_age_days_param_overrides_default(hass: HomeAssistant) -> None:
    """Custom max_age_days flows through to the prune cutoff."""
    history = MileageHistory(
        hass, _entry(), mileage_entity="sensor.odo", max_age_days=3
    )
    now = dt_util.utcnow()
    history._samples.extend(
        [
            (now - timedelta(days=5), 100.0),  # outside the configured 3-day window
            (now - timedelta(days=2), 200.0),
            (now, 250.0),
        ]
    )
    history._prune(now)
    values = [v for _, v in history._samples]
    assert values == [200.0, 250.0]


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
    await history_a._store.async_save(history_a._payload())

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
# Storage corruption: tolerate malformed on-disk payloads                     #
# --------------------------------------------------------------------------- #
#
# Disk files can rot — partial writes, manual edits, schema drift from a
# downgrade, or filesystem corruption. The load path is wrapped in
# defensive guards so a single bad record never kills startup; these
# tests pin that contract.


def _seed_storage(
    hass_storage: dict, storage_key: str, data: dict
) -> None:
    """Inject a Store payload directly into pytest-homeassistant's mock storage."""
    hass_storage[storage_key] = {
        "version": 1,
        "minor_version": 1,
        "key": storage_key,
        "data": data,
    }


async def test_async_load_skips_samples_with_unparseable_timestamp(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    """A garbage timestamp string drops just that sample, not the whole load."""
    entry = _entry()
    _seed_storage(
        hass_storage,
        f"bev_insights.mileage_history.{entry.entry_id}",
        {
            "samples": [
                {"timestamp": "not-a-date", "mileage_km": 1000.0},
                {"timestamp": dt_util.utcnow().isoformat(), "mileage_km": 2000.0},
            ]
        },
    )
    history = MileageHistory(hass, entry, mileage_entity="sensor.odo")
    await history.async_load()
    values = [v for _, v in history._samples]
    # Garbage row dropped (ts parses to None); good row kept.
    assert values == [2000.0]


async def test_async_load_skips_samples_with_missing_keys(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    """Missing `timestamp` or value key → KeyError caught, sample dropped."""
    entry = _entry()
    now_iso = dt_util.utcnow().isoformat()
    _seed_storage(
        hass_storage,
        f"bev_insights.mileage_history.{entry.entry_id}",
        {
            "samples": [
                {"mileage_km": 1000.0},  # no timestamp
                {"timestamp": now_iso},  # no value
                {"timestamp": now_iso, "mileage_km": 3000.0},
            ]
        },
    )
    history = MileageHistory(hass, entry, mileage_entity="sensor.odo")
    await history.async_load()
    values = [v for _, v in history._samples]
    assert values == [3000.0]


async def test_async_load_skips_samples_with_non_numeric_value(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    """A value that can't be coerced to float drops the sample."""
    entry = _entry()
    now_iso = dt_util.utcnow().isoformat()
    _seed_storage(
        hass_storage,
        f"bev_insights.mileage_history.{entry.entry_id}",
        {
            "samples": [
                {"timestamp": now_iso, "mileage_km": "not-a-number"},
                {"timestamp": now_iso, "mileage_km": None},
                {"timestamp": now_iso, "mileage_km": 4000.0},
            ]
        },
    )
    history = MileageHistory(hass, entry, mileage_entity="sensor.odo")
    await history.async_load()
    values = [v for _, v in history._samples]
    assert values == [4000.0]


async def test_async_load_handles_samples_key_missing(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    """An on-disk payload without a `samples` key loads to empty cleanly."""
    entry = _entry()
    _seed_storage(
        hass_storage,
        f"bev_insights.mileage_history.{entry.entry_id}",
        {"version": 99, "other_field": "noise"},
    )
    history = MileageHistory(hass, entry, mileage_entity="sensor.odo")
    await history.async_load()
    assert history.has_data is False


async def test_async_load_handles_top_level_non_dict(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    """A non-dict top-level payload (e.g. a list) is rejected without raising."""
    entry = _entry()
    hass_storage[f"bev_insights.mileage_history.{entry.entry_id}"] = {
        "version": 1,
        "minor_version": 1,
        "key": f"bev_insights.mileage_history.{entry.entry_id}",
        "data": ["this", "should", "be", "a", "dict"],
    }
    history = MileageHistory(hass, entry, mileage_entity="sensor.odo")
    await history.async_load()
    assert history.has_data is False


async def test_async_load_partial_corruption_keeps_good_samples(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    """Mix of good and bad rows → only the good ones land."""
    entry = _entry()
    now = dt_util.utcnow()
    _seed_storage(
        hass_storage,
        f"bev_insights.soc_history.{entry.entry_id}",
        {
            "samples": [
                {"timestamp": (now - timedelta(days=2)).isoformat(), "soc_percent": 90.0},
                {"timestamp": "garbage", "soc_percent": 50.0},
                {"timestamp": (now - timedelta(days=1)).isoformat(), "soc_percent": "nope"},
                {"timestamp": now.isoformat(), "soc_percent": 70.0},
            ]
        },
    )
    history = SocHistory(hass, entry, soc_entity="sensor.soc")
    await history.async_load()
    values = [v for _, v in history._samples]
    assert values == [90.0, 70.0]


# --------------------------------------------------------------------------- #
# Lifecycle: async_stop edge cases                                            #
# --------------------------------------------------------------------------- #


async def test_async_stop_without_start_is_noop(hass: HomeAssistant) -> None:
    """Calling stop on a tracker that was never started must not raise."""
    history = MileageHistory(hass, _entry(), mileage_entity="sensor.odo")
    # async_load might be called; async_start was not.
    await history.async_load()
    await history.async_stop()  # No _unsub, no _samples → silent no-op.
    assert history.has_data is False


async def test_async_stop_with_empty_history_skips_flush(
    hass: HomeAssistant,
) -> None:
    """Stop with samples=[] skips the disk write — checked by no surprise raise."""
    history = MileageHistory(hass, _entry(), mileage_entity="sensor.odo")
    history.async_start()
    await hass.async_block_till_done()
    # Source entity never set → no samples ever recorded.
    assert history.has_data is False
    await history.async_stop()
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


# --------------------------------------------------------------------------- #
# Subclass hook contract: _read / _signal                                     #
# --------------------------------------------------------------------------- #
#
# `EntityHistory._read` / `_signal` are abstract stubs that raise. Recording
# and signalling both route through them, so a concrete subclass that forgot
# to override either would fail at runtime inside HA. These tests pin the
# contract directly: the base raises, and every concrete subclass overrides.


async def test_base_entity_history_hooks_raise(hass: HomeAssistant) -> None:
    """The un-overridden base hooks must raise NotImplementedError."""
    base = EntityHistory(
        hass,
        _entry(),
        source_entity="sensor.whatever",
        storage_key_prefix="bev_insights.base_test",
        max_age_days=8,
    )
    with pytest.raises(NotImplementedError):
        base._read()
    with pytest.raises(NotImplementedError):
        base._signal()


@pytest.mark.parametrize(
    ("make", "expected_signal"),
    [
        (
            lambda hass: MileageHistory(hass, _entry(), mileage_entity="sensor.odo"),
            signal_mileage_history_updated("test_entry"),
        ),
        (
            lambda hass: SocHistory(hass, _entry(), soc_entity="sensor.soc"),
            signal_soc_history_updated("test_entry"),
        ),
    ],
)
async def test_concrete_subclasses_override_hooks(
    hass: HomeAssistant, make, expected_signal
) -> None:
    """Each concrete subclass supplies real `_read` / `_signal` impls.

    `_signal` returns the per-entry dispatcher signal; `_read` returns None
    when the source entity has no state (rather than raising the base stub).
    """
    history = make(hass)
    assert history._signal() == expected_signal
    assert history._read() is None
