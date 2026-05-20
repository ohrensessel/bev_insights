"""Tests for the recorder-based history backfill."""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bev_insights.backfill import async_backfill_from_recorder
from custom_components.bev_insights.const import DOMAIN
from custom_components.bev_insights.tracker import MileageHistory, SocHistory


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


async def test_backfill_from_recorder_populates_history(hass: HomeAssistant) -> None:
    """Full integration: recorder returns states, history is populated."""
    history = SocHistory(hass, _entry(), soc_entity="sensor.soc")

    mock_states = [
        _state("sensor.soc", "75", offset_days=5),
        _state("sensor.soc", "60", offset_days=2),
    ]

    mock_instance = MagicMock()
    mock_instance.async_add_executor_job = AsyncMock(return_value=mock_states)

    # get_instance is imported locally inside the function, so patch at the
    # source module. async_add_executor_job is mocked to return mock_states
    # directly, bypassing the real _fetch closure.
    hass.config.components.add("recorder")
    try:
        with patch(
            "homeassistant.components.recorder.get_instance",
            return_value=mock_instance,
        ):
            await async_backfill_from_recorder(hass, history, "sensor.soc", days=8)
    finally:
        hass.config.components.remove("recorder")

    assert history.has_data
