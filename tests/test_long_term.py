"""Tests for the LTS-backed long-term distance sensors.

These exercise the period-start math, the recorder-statistics query path
(mocked), and the caching behaviour that keeps the hot recompute path
synchronous.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bev_insights.const import CONFIG_ENTRY_VERSION, DOMAIN
from custom_components.bev_insights.sensor.long_term import (
    DistanceThisMonthSensor,
    DistanceThisYearSensor,
)

from .common import (
    ACTUAL_CAPACITY_ENTITY,
    CHARGING_ENTITY,
    MILEAGE_ENTITY,
    RANGE_ENTITY,
    SOC_ENTITY,
    base_entry_data,
    make_entry,
)

# Statistics-module import gated the same way `test_backfill.py` gates the
# recorder import — on the minimum-supported HA the recorder pulls in
# psutil_home_assistant which isn't installed on every matrix row.
try:
    from homeassistant.components.recorder import statistics as _hass_statistics
except ImportError:  # pragma: no cover - environment-dependent
    _hass_statistics = None  # type: ignore[assignment]


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        version=CONFIG_ENTRY_VERSION,
        data=base_entry_data(),
        entry_id="long_term_test",
    )


# --------------------------------------------------------------------------- #
# Period-start arithmetic                                                     #
# --------------------------------------------------------------------------- #


def test_month_period_start_is_first_of_month(hass: HomeAssistant) -> None:
    sensor = DistanceThisMonthSensor(_entry(), MILEAGE_ENTITY)
    start = sensor._period_start()
    # Converting back to local time should give day=1, hour=0.
    local = dt_util.as_local(start)
    assert local.day == 1
    assert local.hour == 0
    assert local.minute == 0
    assert local.second == 0


def test_year_period_start_is_january_first(hass: HomeAssistant) -> None:
    sensor = DistanceThisYearSensor(_entry(), MILEAGE_ENTITY)
    start = sensor._period_start()
    local = dt_util.as_local(start)
    assert local.month == 1
    assert local.day == 1
    assert local.hour == 0


# --------------------------------------------------------------------------- #
# Synchronous recalculate uses the cache                                      #
# --------------------------------------------------------------------------- #


def test_recalculate_is_unavailable_without_cached_baseline(
    hass: HomeAssistant,
) -> None:
    """No cached baseline → sensor stays unavailable, never crashes."""
    sensor = DistanceThisMonthSensor(_entry(), MILEAGE_ENTITY)
    sensor.hass = hass
    hass.states.async_set(MILEAGE_ENTITY, "12000", {"unit_of_measurement": "km"})
    sensor._recalculate()
    assert sensor._attr_available is False
    assert sensor._attr_native_value is None


def test_recalculate_with_cached_baseline_computes_delta(
    hass: HomeAssistant,
) -> None:
    sensor = DistanceThisMonthSensor(_entry(), MILEAGE_ENTITY)
    sensor.hass = hass
    sensor._cached_period_start = sensor._period_start()
    sensor._cached_start_value = 11500.0
    hass.states.async_set(MILEAGE_ENTITY, "12340", {"unit_of_measurement": "km"})
    sensor._recalculate()
    assert sensor._attr_available is True
    assert sensor._attr_native_value == 840.0
    assert sensor._attr_last_reset == sensor._period_start()


def test_recalculate_clamps_negative_delta(hass: HomeAssistant) -> None:
    """If the odometer somehow drops below baseline, value clamps to 0."""
    sensor = DistanceThisMonthSensor(_entry(), MILEAGE_ENTITY)
    sensor.hass = hass
    sensor._cached_period_start = sensor._period_start()
    sensor._cached_start_value = 12000.0
    hass.states.async_set(MILEAGE_ENTITY, "11500", {"unit_of_measurement": "km"})
    sensor._recalculate()
    assert sensor._attr_available is True
    assert sensor._attr_native_value == 0.0


def test_recalculate_stale_period_goes_unavailable(hass: HomeAssistant) -> None:
    """If the cached period is from an earlier month, sensor goes unavailable
    so it doesn't serve a stale value while the async refresh catches up.
    """
    sensor = DistanceThisMonthSensor(_entry(), MILEAGE_ENTITY)
    sensor.hass = hass
    # Pretend the cache was set for last year.
    sensor._cached_period_start = sensor._period_start() - timedelta(days=365)
    sensor._cached_start_value = 5000.0
    hass.states.async_set(MILEAGE_ENTITY, "12000", {"unit_of_measurement": "km"})
    sensor._recalculate()
    assert sensor._attr_available is False


def test_recalculate_unavailable_when_mileage_entity_missing(
    hass: HomeAssistant,
) -> None:
    sensor = DistanceThisMonthSensor(_entry(), MILEAGE_ENTITY)
    sensor.hass = hass
    sensor._cached_period_start = sensor._period_start()
    sensor._cached_start_value = 11500.0
    # MILEAGE_ENTITY never set in hass.
    sensor._recalculate()
    assert sensor._attr_available is False


# --------------------------------------------------------------------------- #
# Async baseline refresh                                                      #
# --------------------------------------------------------------------------- #


async def test_refresh_baseline_noop_when_cache_current(
    hass: HomeAssistant,
) -> None:
    """A cached baseline for the current period is left untouched."""
    sensor = DistanceThisMonthSensor(_entry(), MILEAGE_ENTITY)
    sensor.hass = hass
    sensor._cached_period_start = sensor._period_start()
    sensor._cached_start_value = 9999.0

    fetched = False

    async def _fake_fetch(_ts: datetime) -> float | None:
        nonlocal fetched
        fetched = True
        return 1.0

    with patch.object(sensor, "_fetch_value_at", side_effect=_fake_fetch):
        await sensor._async_refresh_baseline()
    assert fetched is False
    assert sensor._cached_start_value == 9999.0


async def test_refresh_baseline_populates_cache_when_empty(
    hass: HomeAssistant,
) -> None:
    sensor = DistanceThisMonthSensor(_entry(), MILEAGE_ENTITY)
    sensor.hass = hass
    hass.states.async_set(MILEAGE_ENTITY, "12300", {"unit_of_measurement": "km"})
    # Make async_write_ha_state a no-op since the sensor isn't actually
    # added to a platform.
    sensor.async_write_ha_state = MagicMock()  # type: ignore[method-assign]

    with patch.object(sensor, "_fetch_value_at", return_value=11000.0):
        await sensor._async_refresh_baseline()
    assert sensor._cached_start_value == 11000.0
    assert sensor._cached_period_start == sensor._period_start()
    assert sensor._attr_available is True
    assert sensor._attr_native_value == 1300.0


async def test_refresh_baseline_keeps_old_cache_on_fetch_failure(
    hass: HomeAssistant,
) -> None:
    sensor = DistanceThisMonthSensor(_entry(), MILEAGE_ENTITY)
    sensor.hass = hass
    sensor._cached_period_start = sensor._period_start() - timedelta(days=400)
    sensor._cached_start_value = 5.0  # value from old period
    with patch.object(sensor, "_fetch_value_at", return_value=None):
        await sensor._async_refresh_baseline()
    # Cache untouched; recompute path will keep the sensor unavailable
    # because the stale cache check fires.
    assert sensor._cached_start_value == 5.0


# --------------------------------------------------------------------------- #
# Statistics query (mocked recorder)                                          #
# --------------------------------------------------------------------------- #


async def test_fetch_value_at_returns_none_without_recorder(
    hass: HomeAssistant,
) -> None:
    sensor = DistanceThisMonthSensor(_entry(), MILEAGE_ENTITY)
    sensor.hass = hass
    assert "recorder" not in hass.config.components
    result = await sensor._fetch_value_at(sensor._period_start())
    assert result is None


@pytest.mark.skipif(
    _hass_statistics is None,
    reason="recorder.statistics not importable on this HA build",
)
async def test_fetch_value_at_extracts_first_row_state(
    hass: HomeAssistant,
) -> None:
    sensor = DistanceThisMonthSensor(_entry(), MILEAGE_ENTITY)
    sensor.hass = hass
    mock_rows = [
        {"start": 0, "end": 3600, "state": 11500.0},
        {"start": 3600, "end": 7200, "state": 11502.5},
    ]
    mock_instance = MagicMock()
    mock_instance.async_add_executor_job = AsyncMock(
        return_value={MILEAGE_ENTITY: mock_rows}
    )
    hass.config.components.add("recorder")
    try:
        with patch(
            "homeassistant.components.recorder.get_instance",
            return_value=mock_instance,
        ):
            value = await sensor._fetch_value_at(sensor._period_start())
    finally:
        hass.config.components.remove("recorder")
    assert value == 11500.0


@pytest.mark.skipif(
    _hass_statistics is None,
    reason="recorder.statistics not importable on this HA build",
)
async def test_fetch_value_at_swallows_recorder_errors(
    hass: HomeAssistant,
) -> None:
    sensor = DistanceThisMonthSensor(_entry(), MILEAGE_ENTITY)
    sensor.hass = hass
    mock_instance = MagicMock()
    mock_instance.async_add_executor_job = AsyncMock(
        side_effect=RuntimeError("statistics boom")
    )
    hass.config.components.add("recorder")
    try:
        with patch(
            "homeassistant.components.recorder.get_instance",
            return_value=mock_instance,
        ):
            value = await sensor._fetch_value_at(sensor._period_start())
    finally:
        hass.config.components.remove("recorder")
    assert value is None


@pytest.mark.skipif(
    _hass_statistics is None,
    reason="recorder.statistics not importable on this HA build",
)
async def test_fetch_value_at_returns_none_when_no_rows(
    hass: HomeAssistant,
) -> None:
    """No statistics for the entity yet (fresh install) → None."""
    sensor = DistanceThisMonthSensor(_entry(), MILEAGE_ENTITY)
    sensor.hass = hass
    mock_instance = MagicMock()
    mock_instance.async_add_executor_job = AsyncMock(return_value={})
    hass.config.components.add("recorder")
    try:
        with patch(
            "homeassistant.components.recorder.get_instance",
            return_value=mock_instance,
        ):
            value = await sensor._fetch_value_at(sensor._period_start())
    finally:
        hass.config.components.remove("recorder")
    assert value is None


# --------------------------------------------------------------------------- #
# Extra-state attributes                                                      #
# --------------------------------------------------------------------------- #


def test_attributes_expose_period_and_baseline(hass: HomeAssistant) -> None:
    sensor = DistanceThisYearSensor(_entry(), MILEAGE_ENTITY)
    sensor.hass = hass
    sensor._cached_period_start = sensor._period_start()
    sensor._cached_start_value = 9876.5
    attrs = sensor.extra_state_attributes
    assert attrs["period"] == "this_year"
    assert attrs["period_start"] == sensor._period_start().isoformat()
    assert attrs["baseline_mileage_km"] == 9876.5


def test_attributes_omit_baseline_when_cache_empty(hass: HomeAssistant) -> None:
    sensor = DistanceThisYearSensor(_entry(), MILEAGE_ENTITY)
    sensor.hass = hass
    attrs = sensor.extra_state_attributes
    assert "baseline_mileage_km" not in attrs


# --------------------------------------------------------------------------- #
# Full-entry wiring                                                           #
# --------------------------------------------------------------------------- #


async def test_long_term_sensors_registered_with_full_entry(
    hass: HomeAssistant,
) -> None:
    """Both sensors appear in the entity registry on a fully-wired setup."""
    hass.states.async_set(SOC_ENTITY, "60")
    hass.states.async_set(RANGE_ENTITY, "250", {"unit_of_measurement": "km"})
    hass.states.async_set(MILEAGE_ENTITY, "12000", {"unit_of_measurement": "km"})
    hass.states.async_set(CHARGING_ENTITY, "off")
    hass.states.async_set(ACTUAL_CAPACITY_ENTITY, "70")
    entry = make_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    registry = hass.data["entity_registry"]
    suffixes = {
        e.unique_id[len(entry.entry_id):]
        for e in registry.entities.values()
        if e.config_entry_id == entry.entry_id
    }
    assert "_distance_this_month" in suffixes
    assert "_distance_this_year" in suffixes
