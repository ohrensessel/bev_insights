"""Tests for the recorder-based history backfill."""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bev_insights.backfill import (
    _find_last_complete_cycle,
    _parse_distance_km,
    _parse_soc,
    _value_at,
    async_backfill_from_recorder,
    async_backfill_tracker_from_recorder,
)
from custom_components.bev_insights.const import (
    BASELINE_MILEAGE_KM,
    BASELINE_SOC_PERCENT,
    DOMAIN,
    SESSION_END_SOC_PERCENT,
    SESSION_START_SOC_PERCENT,
)
from custom_components.bev_insights.tracker import (
    ChargeTracker,
    MileageHistory,
    SocHistory,
)

# The recorder pulls in psutil_home_assistant on some HA builds, which is not
# installed on the minimum-supported HA test matrix.  Import it optionally so
# the module still collects; tests that need it are skipped when unavailable.
try:
    import homeassistant.components.recorder as _hass_recorder
except ImportError:  # pragma: no cover - environment-dependent
    _hass_recorder = None  # type: ignore[assignment]


def _entry() -> MockConfigEntry:
    return MockConfigEntry(domain=DOMAIN, data={}, entry_id="test_entry")


def _state(entity_id: str, state_val: str, attrs: dict | None = None, offset_days: float = 1.0) -> State:
    """Build a minimal State object with a timestamp offset from now."""
    return State(
        entity_id,
        state_val,
        attributes=attrs or {},
        last_updated=dt_util.utcnow() - timedelta(days=offset_days),
        last_changed=dt_util.utcnow() - timedelta(days=offset_days),
    )


# --------------------------------------------------------------------------- #
# EntityHistory.async_backfill                                                #
# --------------------------------------------------------------------------- #


async def test_backfill_inserts_samples_into_empty_deque(hass: HomeAssistant) -> None:
    """async_backfill loads historical states into an empty SocHistory."""
    history = SocHistory(hass, _entry(), soc_entity="sensor.soc")
    assert not history.has_data

    states = [
        _state("sensor.soc", "70", offset_days=6),
        _state("sensor.soc", "60", offset_days=3),
        _state("sensor.soc", "50", offset_days=1),
    ]
    await history.async_backfill(states)

    assert history.has_data
    assert history.sample_count == 3
    # Oldest sample should be the 70% one
    oldest = history.oldest_sample
    assert oldest is not None
    assert oldest[1] == 70.0


async def test_backfill_is_noop_when_deque_has_data(hass: HomeAssistant) -> None:
    """async_backfill does nothing when the deque already has samples."""
    history = SocHistory(hass, _entry(), soc_entity="sensor.soc")
    now = dt_util.utcnow()
    history._samples.append((now - timedelta(days=1), 55.0))

    states = [_state("sensor.soc", "80", offset_days=5)]
    await history.async_backfill(states)

    # Still only the one pre-existing sample
    assert history.sample_count == 1
    assert history._samples[0][1] == 55.0


async def test_backfill_skips_non_numeric_states(hass: HomeAssistant) -> None:
    """unavailable / unknown states are skipped."""
    history = SocHistory(hass, _entry(), soc_entity="sensor.soc")
    states = [
        _state("sensor.soc", "unavailable", offset_days=4),
        _state("sensor.soc", "unknown", offset_days=3),
        _state("sensor.soc", "65", offset_days=2),
    ]
    await history.async_backfill(states)
    assert history.sample_count == 1
    assert history._samples[0][1] == 65.0


async def test_backfill_skips_too_old_samples(hass: HomeAssistant) -> None:
    """Samples older than max_age_days are silently dropped."""
    history = SocHistory(hass, _entry(), soc_entity="sensor.soc", max_age_days=8)
    states = [
        _state("sensor.soc", "80", offset_days=10),  # too old
        _state("sensor.soc", "70", offset_days=5),   # within window
    ]
    await history.async_backfill(states)
    assert history.sample_count == 1
    assert history._samples[0][1] == 70.0


async def test_soc_history_backfill_clamps_values(hass: HomeAssistant) -> None:
    """SocHistory clamps out-of-range values the same way live recording does."""
    history = SocHistory(hass, _entry(), soc_entity="sensor.soc")
    states = [
        _state("sensor.soc", "-5", offset_days=3),   # below 0 → clamped to 0
        _state("sensor.soc", "110", offset_days=2),  # above 100 → clamped to 100
        _state("sensor.soc", "60", offset_days=1),
    ]
    await history.async_backfill(states)
    values = [s[1] for s in history._samples]
    assert values[0] == 0.0
    assert values[1] == 100.0
    assert values[2] == 60.0


async def test_mileage_history_backfill_converts_miles(hass: HomeAssistant) -> None:
    """MileageHistory converts historical mile readings to kilometres."""
    history = MileageHistory(hass, _entry(), mileage_entity="sensor.odo")
    states = [
        _state("sensor.odo", "1000", {"unit_of_measurement": "mi"}, offset_days=3),
        _state("sensor.odo", "1100", {"unit_of_measurement": "mi"}, offset_days=1),
    ]
    await history.async_backfill(states)
    assert history.sample_count == 2
    # 1000 mi × 1.609344 ≈ 1609.344 km
    assert abs(history._samples[0][1] - 1609.344) < 0.01
    assert abs(history._samples[1][1] - 1770.278) < 0.01


async def test_backfill_deduplicates_consecutive_identical(hass: HomeAssistant) -> None:
    """Consecutive samples with the same value are collapsed to one."""
    history = SocHistory(hass, _entry(), soc_entity="sensor.soc")
    states = [
        _state("sensor.soc", "70", offset_days=4),
        _state("sensor.soc", "70", offset_days=3),  # duplicate → dropped
        _state("sensor.soc", "65", offset_days=2),
    ]
    await history.async_backfill(states)
    assert history.sample_count == 2
    assert history._samples[0][1] == 70.0
    assert history._samples[1][1] == 65.0


# --------------------------------------------------------------------------- #
# async_backfill_from_recorder                                                #
# --------------------------------------------------------------------------- #


async def test_backfill_from_recorder_skips_when_recorder_absent(
    hass: HomeAssistant,
) -> None:
    """Does nothing when the recorder component is not loaded."""
    history = SocHistory(hass, _entry(), soc_entity="sensor.soc")
    # recorder is not in hass.config.components in normal test setup
    assert "recorder" not in hass.config.components
    await async_backfill_from_recorder(hass, history, "sensor.soc", days=8)
    assert not history.has_data


async def test_backfill_from_recorder_skips_when_already_has_data(
    hass: HomeAssistant,
) -> None:
    """Does nothing when the deque already has data."""
    history = SocHistory(hass, _entry(), soc_entity="sensor.soc")
    history._samples.append((dt_util.utcnow(), 50.0))

    # Even if recorder were loaded, the call should return immediately.
    hass.config.components.add("recorder")
    try:
        await async_backfill_from_recorder(hass, history, "sensor.soc", days=8)
    finally:
        hass.config.components.remove("recorder")

    assert history.sample_count == 1  # unchanged


@pytest.mark.skipif(
    _hass_recorder is None,
    reason="homeassistant.components.recorder not importable on this HA build",
)
async def test_backfill_from_recorder_empty_result_leaves_history_empty(
    hass: HomeAssistant,
) -> None:
    """Recorder returns an empty list → history stays empty, no crash."""
    history = SocHistory(hass, _entry(), soc_entity="sensor.soc")

    mock_instance = MagicMock()
    mock_instance.async_add_executor_job = AsyncMock(return_value=[])

    hass.config.components.add("recorder")
    try:
        with patch.object(_hass_recorder, "get_instance", return_value=mock_instance):
            await async_backfill_from_recorder(hass, history, "sensor.soc", days=8)
    finally:
        hass.config.components.remove("recorder")

    assert not history.has_data


@pytest.mark.skipif(
    _hass_recorder is None,
    reason="homeassistant.components.recorder not importable on this HA build",
)
async def test_backfill_from_recorder_swallows_recorder_errors(
    hass: HomeAssistant,
) -> None:
    """A recorder-side exception is logged and swallowed; history stays empty."""
    history = SocHistory(hass, _entry(), soc_entity="sensor.soc")

    mock_instance = MagicMock()
    mock_instance.async_add_executor_job = AsyncMock(
        side_effect=RuntimeError("recorder exploded")
    )

    hass.config.components.add("recorder")
    try:
        with patch.object(_hass_recorder, "get_instance", return_value=mock_instance):
            await async_backfill_from_recorder(hass, history, "sensor.soc", days=8)
    finally:
        hass.config.components.remove("recorder")

    assert not history.has_data


@pytest.mark.skipif(
    _hass_recorder is None,
    reason="homeassistant.components.recorder not importable on this HA build",
)
async def test_backfill_from_recorder_populates_history(hass: HomeAssistant) -> None:
    """Full integration: recorder returns states, history is populated."""
    history = SocHistory(hass, _entry(), soc_entity="sensor.soc")

    mock_states = [
        _state("sensor.soc", "75", offset_days=5),
        _state("sensor.soc", "60", offset_days=2),
    ]

    mock_instance = MagicMock()
    mock_instance.async_add_executor_job = AsyncMock(return_value=mock_states)

    # patch.object works because _hass_recorder is already in sys.modules;
    # the local `from ... import get_instance` inside the function then picks
    # up the mock. async_add_executor_job is mocked to return mock_states
    # directly, bypassing the real _fetch closure.
    hass.config.components.add("recorder")
    try:
        with patch.object(_hass_recorder, "get_instance", return_value=mock_instance):
            await async_backfill_from_recorder(hass, history, "sensor.soc", days=8)
    finally:
        hass.config.components.remove("recorder")

    assert history.has_data


@pytest.mark.skipif(
    _hass_recorder is None,
    reason="homeassistant.components.recorder not importable on this HA build",
)
async def test_backfill_from_recorder_executes_fetch_closure(
    hass: HomeAssistant,
) -> None:
    """End-to-end of the `_fetch` closure.

    The other recorder tests short-circuit `async_add_executor_job` to a
    canned return value, which means the closure body that actually calls
    `state_changes_during_period` is never exercised. Here we route the
    executor-job through real invocation and stub the recorder's history
    function instead — covers the dict-lookup + list-wrap path inside
    `_fetch`.
    """
    history = SocHistory(hass, _entry(), soc_entity="sensor.soc")
    mock_states = [_state("sensor.soc", "65", offset_days=3)]

    async def _invoke_fn(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    # Don't bake HA's positional signature into the stub; accept anything and
    # assert only the contract we rely on — that `_fetch` requests our entity
    # by id. Keeps the test robust to recorder-API param reordering/renames.
    def _fake_state_changes(*args, **kwargs):
        assert kwargs["entity_id"] == "sensor.soc"
        return {"sensor.soc": mock_states}

    mock_instance = MagicMock()
    mock_instance.async_add_executor_job = AsyncMock(side_effect=_invoke_fn)

    hass.config.components.add("recorder")
    try:
        with (
            patch.object(_hass_recorder, "get_instance", return_value=mock_instance),
            patch.object(
                _hass_recorder.history,
                "state_changes_during_period",
                side_effect=_fake_state_changes,
            ),
        ):
            await async_backfill_from_recorder(hass, history, "sensor.soc", days=8)
    finally:
        hass.config.components.remove("recorder")

    assert history.has_data
    assert history.sample_count == 1


@pytest.mark.skipif(
    _hass_recorder is None,
    reason="homeassistant.components.recorder not importable on this HA build",
)
async def test_backfill_from_recorder_skips_logging_when_all_states_invalid(
    hass: HomeAssistant,
) -> None:
    """Recorder returns only invalid states → deque stays empty, no info log.

    Exercises the `if count:` False branch — the executor returned data,
    but `async_backfill` filtered it all out (e.g. recorder gave us only
    "unavailable" / pre-window states), leaving the deque empty.
    """
    history = SocHistory(hass, _entry(), soc_entity="sensor.soc")

    mock_states = [
        _state("sensor.soc", "unavailable", offset_days=3),
        _state("sensor.soc", "unknown", offset_days=2),
    ]
    mock_instance = MagicMock()
    mock_instance.async_add_executor_job = AsyncMock(return_value=mock_states)

    hass.config.components.add("recorder")
    try:
        with patch.object(_hass_recorder, "get_instance", return_value=mock_instance):
            await async_backfill_from_recorder(hass, history, "sensor.soc", days=8)
    finally:
        hass.config.components.remove("recorder")

    assert not history.has_data


# --------------------------------------------------------------------------- #
# Tracker baseline backfill — pure helpers                                    #
# --------------------------------------------------------------------------- #


CHARGING = "binary_sensor.car_charging"
MILEAGE = "sensor.car_mileage"
SOC = "sensor.car_soc"


def test_value_at_returns_latest_at_or_before() -> None:
    states = [
        _state("sensor.x", "1", offset_days=5),
        _state("sensor.x", "2", offset_days=3),
        _state("sensor.x", "3", offset_days=1),
    ]
    ts = dt_util.utcnow() - timedelta(days=2)
    result = _value_at(states, ts)
    assert result is not None
    assert result.state == "2"


def test_value_at_returns_none_when_all_after_ts() -> None:
    states = [_state("sensor.x", "1", offset_days=1)]
    ts = dt_util.utcnow() - timedelta(days=5)
    assert _value_at(states, ts) is None


def test_parse_distance_km_handles_miles() -> None:
    state = _state("sensor.odo", "100", {"unit_of_measurement": "mi"})
    assert _parse_distance_km(state) == pytest.approx(160.9344)


def test_parse_distance_km_returns_none_for_unavailable() -> None:
    state = _state("sensor.odo", "unavailable")
    assert _parse_distance_km(state) is None


def test_parse_distance_km_returns_none_for_non_numeric() -> None:
    """Garbage strings from a buggy upstream parse to None, not raise."""
    assert _parse_distance_km(_state("sensor.odo", "not-a-number")) is None


def test_parse_distance_km_returns_none_when_state_is_none() -> None:
    assert _parse_distance_km(None) is None


def test_parse_soc_clamps_out_of_range() -> None:
    assert _parse_soc(_state("sensor.soc", "-5")) == 0.0
    assert _parse_soc(_state("sensor.soc", "120")) == 100.0
    assert _parse_soc(_state("sensor.soc", "55")) == 55.0


def test_parse_soc_returns_none_when_state_is_none() -> None:
    assert _parse_soc(None) is None


def test_parse_soc_returns_none_for_non_numeric() -> None:
    assert _parse_soc(_state("sensor.soc", "n/a")) is None


def test_parse_soc_returns_none_for_unavailable() -> None:
    assert _parse_soc(_state("sensor.soc", "unavailable")) is None


def test_find_last_complete_cycle_returns_paired_edges() -> None:
    """A clean off→on→off run reports both edges."""
    states = [
        _state(CHARGING, "off", offset_days=10),
        _state(CHARGING, "on", offset_days=8),
        _state(CHARGING, "off", offset_days=7),
    ]
    start_ts, end_ts = _find_last_complete_cycle(states)
    assert start_ts is not None
    assert end_ts is not None
    assert start_ts < end_ts


def test_find_last_complete_cycle_picks_latest_falling_edge() -> None:
    """Two complete cycles → returns the most recent one."""
    states = [
        _state(CHARGING, "off", offset_days=10),
        _state(CHARGING, "on", offset_days=9),
        _state(CHARGING, "off", offset_days=8),  # first falling edge
        _state(CHARGING, "on", offset_days=5),
        _state(CHARGING, "off", offset_days=4),  # latest falling edge — picked
    ]
    start_ts, end_ts = _find_last_complete_cycle(states)
    assert start_ts is not None
    assert end_ts is not None
    # End ts is roughly 4 days ago, not 8.
    assert (dt_util.utcnow() - end_ts) < timedelta(days=5)


def test_find_last_complete_cycle_walks_back_through_sustained_charging() -> None:
    """Multiple consecutive 'on' samples between the rising and falling edges.

    The walk-back must skip the intermediate charging samples and anchor on
    the rising edge that actually opened the session, not the nearest sample.
    """
    states = [
        _state(CHARGING, "off", offset_days=10),
        _state(CHARGING, "on", offset_days=9),  # rising edge
        _state(CHARGING, "on", offset_days=8),  # still charging
        _state(CHARGING, "on", offset_days=7),  # still charging
        _state(CHARGING, "off", offset_days=6),  # falling edge
    ]
    start_ts, end_ts = _find_last_complete_cycle(states)
    assert start_ts is not None
    assert end_ts is not None
    # Start anchors on the 9-days-ago rising edge, not the 7-days-ago sample.
    assert (dt_util.utcnow() - start_ts) > timedelta(days=8)


def test_find_last_complete_cycle_returns_none_when_no_falling_edge() -> None:
    states = [
        _state(CHARGING, "off", offset_days=5),
        _state(CHARGING, "on", offset_days=2),  # still charging at "now"
    ]
    start_ts, end_ts = _find_last_complete_cycle(states)
    assert start_ts is None
    assert end_ts is None


def test_find_last_complete_cycle_returns_end_only_when_rising_missing() -> None:
    """HA restart mid-charge: only the falling edge is in history."""
    states = [
        _state(CHARGING, "on", offset_days=5),
        _state(CHARGING, "off", offset_days=3),
    ]
    start_ts, end_ts = _find_last_complete_cycle(states)
    # Rising edge isn't observed because there's no preceding "off" sample.
    assert start_ts is None
    assert end_ts is not None




# --------------------------------------------------------------------------- #
# Tracker baseline backfill — ChargeTracker.async_backfill_baseline           #
# --------------------------------------------------------------------------- #


def _make_tracker(hass: HomeAssistant) -> ChargeTracker:
    return ChargeTracker(
        hass, _entry(), charging_entity=CHARGING, mileage_entity=MILEAGE, soc_entity=SOC
    )


async def test_tracker_baseline_backfill_sets_baseline(hass: HomeAssistant) -> None:
    tracker = _make_tracker(hass)
    await tracker.async_load()
    end_ts = dt_util.utcnow() - timedelta(days=2)

    accepted = await tracker.async_backfill_baseline(
        mileage_km=15000.0, soc_percent=85.0, end_ts=end_ts
    )

    assert accepted is True
    assert tracker.baseline is not None
    assert tracker.baseline[BASELINE_MILEAGE_KM] == 15000.0
    assert tracker.baseline[BASELINE_SOC_PERCENT] == 85.0
    assert tracker.last_session is None


async def test_tracker_baseline_backfill_with_full_cycle_sets_session(
    hass: HomeAssistant,
) -> None:
    tracker = _make_tracker(hass)
    await tracker.async_load()
    end_ts = dt_util.utcnow() - timedelta(days=2)
    start_ts = end_ts - timedelta(hours=4)

    await tracker.async_backfill_baseline(
        mileage_km=15000.0,
        soc_percent=85.0,
        end_ts=end_ts,
        start_soc_percent=30.0,
        start_ts=start_ts,
    )

    assert tracker.last_session is not None
    assert tracker.last_session[SESSION_START_SOC_PERCENT] == 30.0
    assert tracker.last_session[SESSION_END_SOC_PERCENT] == 85.0
    assert tracker.session_log == [tracker.last_session]


async def test_tracker_baseline_backfill_is_noop_when_baseline_exists(
    hass: HomeAssistant,
) -> None:
    tracker = _make_tracker(hass)
    await tracker.async_load()
    end_ts = dt_util.utcnow() - timedelta(days=2)

    await tracker.async_backfill_baseline(
        mileage_km=10000.0, soc_percent=50.0, end_ts=end_ts
    )
    accepted = await tracker.async_backfill_baseline(
        mileage_km=99999.0, soc_percent=99.0, end_ts=end_ts
    )

    assert accepted is False
    # First values preserved — the second call did nothing.
    assert tracker.baseline is not None
    assert tracker.baseline[BASELINE_MILEAGE_KM] == 10000.0


async def test_tracker_baseline_backfill_persists_across_reload(
    hass: HomeAssistant,
) -> None:
    """Backfilled baseline survives a reload like a live-captured one."""
    entry = _entry()
    tracker_a = ChargeTracker(
        hass, entry, charging_entity=CHARGING, mileage_entity=MILEAGE, soc_entity=SOC
    )
    await tracker_a.async_load()
    end_ts = dt_util.utcnow() - timedelta(days=1)
    await tracker_a.async_backfill_baseline(
        mileage_km=12345.6, soc_percent=72.0, end_ts=end_ts
    )

    tracker_b = ChargeTracker(
        hass, entry, charging_entity=CHARGING, mileage_entity=MILEAGE, soc_entity=SOC
    )
    await tracker_b.async_load()
    assert tracker_b.baseline is not None
    assert tracker_b.baseline[BASELINE_MILEAGE_KM] == 12345.6
    assert tracker_b.baseline[BASELINE_SOC_PERCENT] == 72.0


# --------------------------------------------------------------------------- #
# async_backfill_tracker_from_recorder                                        #
# --------------------------------------------------------------------------- #


async def test_tracker_recorder_backfill_skips_when_recorder_absent(
    hass: HomeAssistant,
) -> None:
    tracker = _make_tracker(hass)
    await tracker.async_load()
    assert "recorder" not in hass.config.components

    await async_backfill_tracker_from_recorder(
        hass, tracker, CHARGING, MILEAGE, SOC, days=8
    )
    assert tracker.baseline is None


async def test_tracker_recorder_backfill_skips_when_baseline_exists(
    hass: HomeAssistant,
) -> None:
    tracker = _make_tracker(hass)
    await tracker.async_load()
    await tracker.async_backfill_baseline(
        mileage_km=1.0, soc_percent=1.0, end_ts=dt_util.utcnow()
    )

    hass.config.components.add("recorder")
    try:
        await async_backfill_tracker_from_recorder(
            hass, tracker, CHARGING, MILEAGE, SOC, days=8
        )
    finally:
        hass.config.components.remove("recorder")
    # Untouched.
    assert tracker.baseline is not None
    assert tracker.baseline[BASELINE_MILEAGE_KM] == 1.0


@pytest.mark.skipif(
    _hass_recorder is None,
    reason="homeassistant.components.recorder not importable on this HA build",
)
async def test_tracker_recorder_backfill_swallows_recorder_errors(
    hass: HomeAssistant,
) -> None:
    tracker = _make_tracker(hass)
    await tracker.async_load()

    mock_instance = MagicMock()
    mock_instance.async_add_executor_job = AsyncMock(
        side_effect=RuntimeError("recorder boom")
    )

    hass.config.components.add("recorder")
    try:
        with patch.object(_hass_recorder, "get_instance", return_value=mock_instance):
            await async_backfill_tracker_from_recorder(
                hass, tracker, CHARGING, MILEAGE, SOC, days=8
            )
    finally:
        hass.config.components.remove("recorder")

    assert tracker.baseline is None


@pytest.mark.skipif(
    _hass_recorder is None,
    reason="homeassistant.components.recorder not importable on this HA build",
)
async def test_tracker_recorder_backfill_populates_baseline_and_session(
    hass: HomeAssistant,
) -> None:
    """Full integration: recorder reports a clean off→on→off; baseline + session set."""
    tracker = _make_tracker(hass)
    await tracker.async_load()

    # Offsets are chosen so the mileage and SoC samples bracket each
    # charging edge by a comfortable margin — without that, microsecond
    # differences between _state() calls make _value_at's "<= end_ts"
    # check flaky.
    charging_states = [
        _state(CHARGING, "off", offset_days=9),
        _state(CHARGING, "on", offset_days=5),
        _state(CHARGING, "off", offset_days=2),
    ]
    mileage_states = [
        _state(MILEAGE, "12000", {"unit_of_measurement": "km"}, offset_days=6),
        _state(MILEAGE, "12500", {"unit_of_measurement": "km"}, offset_days=2.5),
    ]
    soc_states = [
        _state(SOC, "30", offset_days=5.5),
        _state(SOC, "82", offset_days=2.5),
    ]

    per_entity = {
        CHARGING: charging_states,
        MILEAGE: mileage_states,
        SOC: soc_states,
    }

    async def _fake_executor_job(fn, entity_id):
        return per_entity[entity_id]

    mock_instance = MagicMock()
    mock_instance.async_add_executor_job = AsyncMock(side_effect=_fake_executor_job)

    hass.config.components.add("recorder")
    try:
        with patch.object(_hass_recorder, "get_instance", return_value=mock_instance):
            await async_backfill_tracker_from_recorder(
                hass, tracker, CHARGING, MILEAGE, SOC, days=8
            )
    finally:
        hass.config.components.remove("recorder")

    assert tracker.baseline is not None
    assert tracker.baseline[BASELINE_MILEAGE_KM] == 12500.0
    assert tracker.baseline[BASELINE_SOC_PERCENT] == 82.0
    assert tracker.last_session is not None
    assert tracker.last_session[SESSION_START_SOC_PERCENT] == 30.0
    assert tracker.last_session[SESSION_END_SOC_PERCENT] == 82.0


@pytest.mark.skipif(
    _hass_recorder is None,
    reason="homeassistant.components.recorder not importable on this HA build",
)
async def test_tracker_recorder_backfill_executes_fetch_closure(
    hass: HomeAssistant,
) -> None:
    """Drive the inner `_fetch` closure end-to-end (parallel to the EntityHistory one)."""
    tracker = _make_tracker(hass)
    await tracker.async_load()

    per_entity = {
        CHARGING: [
            _state(CHARGING, "off", offset_days=6),
            _state(CHARGING, "on", offset_days=4),
            _state(CHARGING, "off", offset_days=2),
        ],
        MILEAGE: [_state(MILEAGE, "20000", {"unit_of_measurement": "km"}, offset_days=2.5)],
        SOC: [
            _state(SOC, "30", offset_days=4.5),
            _state(SOC, "80", offset_days=2.5),
        ],
    }

    # Accept any signature; assert only that the closure requests a known
    # entity by id, so the test doesn't break on recorder-API param changes.
    def _fake_state_changes(*args, **kwargs):
        entity_id = kwargs["entity_id"]
        assert entity_id in per_entity
        return {entity_id: per_entity.get(entity_id, [])}

    async def _invoke_fn(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    mock_instance = MagicMock()
    mock_instance.async_add_executor_job = AsyncMock(side_effect=_invoke_fn)

    hass.config.components.add("recorder")
    try:
        with (
            patch.object(_hass_recorder, "get_instance", return_value=mock_instance),
            patch.object(
                _hass_recorder.history,
                "state_changes_during_period",
                side_effect=_fake_state_changes,
            ),
        ):
            await async_backfill_tracker_from_recorder(
                hass, tracker, CHARGING, MILEAGE, SOC, days=8
            )
    finally:
        hass.config.components.remove("recorder")

    assert tracker.baseline is not None
    assert tracker.baseline[BASELINE_MILEAGE_KM] == 20000.0


@pytest.mark.skipif(
    _hass_recorder is None,
    reason="homeassistant.components.recorder not importable on this HA build",
)
async def test_tracker_recorder_backfill_no_charging_history_is_noop(
    hass: HomeAssistant,
) -> None:
    """Recorder has no charging history at all → bail before scanning edges."""
    tracker = _make_tracker(hass)
    await tracker.async_load()

    async def _fake_executor_job(_fn, _entity_id):
        return []  # every entity returns an empty history.

    mock_instance = MagicMock()
    mock_instance.async_add_executor_job = AsyncMock(side_effect=_fake_executor_job)

    hass.config.components.add("recorder")
    try:
        with patch.object(_hass_recorder, "get_instance", return_value=mock_instance):
            await async_backfill_tracker_from_recorder(
                hass, tracker, CHARGING, MILEAGE, SOC, days=8
            )
    finally:
        hass.config.components.remove("recorder")

    assert tracker.baseline is None


@pytest.mark.skipif(
    _hass_recorder is None,
    reason="homeassistant.components.recorder not importable on this HA build",
)
async def test_tracker_recorder_backfill_skips_when_mileage_missing_at_end(
    hass: HomeAssistant,
) -> None:
    """Charging cycle is visible but mileage has no sample by end_ts → bail.

    Without a mileage reading at the falling edge there's nothing to anchor
    a baseline on, so the tracker is left untouched.
    """
    tracker = _make_tracker(hass)
    await tracker.async_load()

    per_entity = {
        CHARGING: [
            _state(CHARGING, "off", offset_days=6),
            _state(CHARGING, "on", offset_days=4),
            _state(CHARGING, "off", offset_days=2),
        ],
        MILEAGE: [],  # no mileage history at all.
        SOC: [
            _state(SOC, "30", offset_days=4.5),
            _state(SOC, "80", offset_days=2.5),
        ],
    }

    async def _fake_executor_job(_fn, entity_id):
        return per_entity[entity_id]

    mock_instance = MagicMock()
    mock_instance.async_add_executor_job = AsyncMock(side_effect=_fake_executor_job)

    hass.config.components.add("recorder")
    try:
        with patch.object(_hass_recorder, "get_instance", return_value=mock_instance):
            await async_backfill_tracker_from_recorder(
                hass, tracker, CHARGING, MILEAGE, SOC, days=8
            )
    finally:
        hass.config.components.remove("recorder")

    assert tracker.baseline is None


@pytest.mark.skipif(
    _hass_recorder is None,
    reason="homeassistant.components.recorder not importable on this HA build",
)
async def test_tracker_recorder_backfill_skips_when_soc_missing_at_end(
    hass: HomeAssistant,
) -> None:
    """Symmetric case to the mileage-missing one — SoC unavailable at end_ts."""
    tracker = _make_tracker(hass)
    await tracker.async_load()

    per_entity = {
        CHARGING: [
            _state(CHARGING, "off", offset_days=6),
            _state(CHARGING, "on", offset_days=4),
            _state(CHARGING, "off", offset_days=2),
        ],
        MILEAGE: [
            _state(MILEAGE, "20000", {"unit_of_measurement": "km"}, offset_days=2.5)
        ],
        SOC: [],  # SoC history empty.
    }

    async def _fake_executor_job(_fn, entity_id):
        return per_entity[entity_id]

    mock_instance = MagicMock()
    mock_instance.async_add_executor_job = AsyncMock(side_effect=_fake_executor_job)

    hass.config.components.add("recorder")
    try:
        with patch.object(_hass_recorder, "get_instance", return_value=mock_instance):
            await async_backfill_tracker_from_recorder(
                hass, tracker, CHARGING, MILEAGE, SOC, days=8
            )
    finally:
        hass.config.components.remove("recorder")

    assert tracker.baseline is None


@pytest.mark.skipif(
    _hass_recorder is None,
    reason="homeassistant.components.recorder not importable on this HA build",
)
async def test_tracker_recorder_backfill_sets_baseline_only_when_rising_edge_missing(
    hass: HomeAssistant,
) -> None:
    """Falling-edge-only history → baseline backfilled, but no last_session.

    Mirrors an HA restart mid-charge: only the falling edge made it into the
    recorder. The baseline still loads (mileage + SoC at end_ts are
    available), but the `start_ts is not None` branch is skipped, so
    `last_session` stays empty.
    """
    tracker = _make_tracker(hass)
    await tracker.async_load()

    per_entity = {
        CHARGING: [
            _state(CHARGING, "on", offset_days=5),
            _state(CHARGING, "off", offset_days=2),  # only the falling edge.
        ],
        MILEAGE: [
            _state(MILEAGE, "30000", {"unit_of_measurement": "km"}, offset_days=2.5)
        ],
        SOC: [_state(SOC, "75", offset_days=2.5)],
    }

    async def _fake_executor_job(_fn, entity_id):
        return per_entity[entity_id]

    mock_instance = MagicMock()
    mock_instance.async_add_executor_job = AsyncMock(side_effect=_fake_executor_job)

    hass.config.components.add("recorder")
    try:
        with patch.object(_hass_recorder, "get_instance", return_value=mock_instance):
            await async_backfill_tracker_from_recorder(
                hass, tracker, CHARGING, MILEAGE, SOC, days=8
            )
    finally:
        hass.config.components.remove("recorder")

    assert tracker.baseline is not None
    assert tracker.baseline[BASELINE_MILEAGE_KM] == 30000.0
    assert tracker.baseline[BASELINE_SOC_PERCENT] == 75.0
    # No rising edge in history → no session pairing.
    assert tracker.last_session is None


@pytest.mark.skipif(
    _hass_recorder is None,
    reason="homeassistant.components.recorder not importable on this HA build",
)
async def test_tracker_recorder_backfill_no_falling_edge_is_noop(
    hass: HomeAssistant,
) -> None:
    """Window shows only continuous charging — no baseline to backfill."""
    tracker = _make_tracker(hass)
    await tracker.async_load()

    charging_states = [
        _state(CHARGING, "off", offset_days=5),
        _state(CHARGING, "on", offset_days=2),  # still charging at "now"
    ]
    per_entity = {CHARGING: charging_states, MILEAGE: [], SOC: []}

    async def _fake_executor_job(fn, entity_id):
        return per_entity[entity_id]

    mock_instance = MagicMock()
    mock_instance.async_add_executor_job = AsyncMock(side_effect=_fake_executor_job)

    hass.config.components.add("recorder")
    try:
        with patch.object(_hass_recorder, "get_instance", return_value=mock_instance):
            await async_backfill_tracker_from_recorder(
                hass, tracker, CHARGING, MILEAGE, SOC, days=8
            )
    finally:
        hass.config.components.remove("recorder")

    assert tracker.baseline is None
