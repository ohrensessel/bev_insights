"""Tests for the efficiency-vs-outside-temperature feature.

Covers the temperature reading helpers, `TemperatureHistory.daily_average`,
the band / local-day formula helpers, config-flow wiring of the optional
temperature sensor, and the `EfficiencyVsTemperatureSensor` end-to-end.
"""
from __future__ import annotations

from datetime import timedelta

from homeassistant import config_entries
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util
import pytest

from custom_components.bev_insights.const import (
    CONF_OUTSIDE_TEMP_SENSOR,
    DOMAIN,
    signal_mileage_history_updated,
    signal_soc_history_updated,
    signal_temperature_history_updated,
)
from custom_components.bev_insights.sensor.formulas import (
    _local_day_windows,
    _temperature_band,
)
from custom_components.bev_insights.sensor.temperature import _range_loss_percent
from custom_components.bev_insights.tracker import TemperatureHistory
from custom_components.bev_insights.util import (
    read_temperature_c,
    temperature_c_from_state,
)

from .common import (
    ACTUAL_CAPACITY_ENTITY,
    CHARGING_ENTITY,
    MILEAGE_ENTITY,
    OUTSIDE_TEMP_ENTITY,
    RANGE_ENTITY,
    SOC_ENTITY,
    make_entry,
    seed_history,
)

# --------------------------------------------------------------------------- #
# util: temperature reading                                                   #
# --------------------------------------------------------------------------- #


async def test_read_temperature_celsius(hass: HomeAssistant) -> None:
    hass.states.async_set(
        "sensor.t", "12.5", {"unit_of_measurement": "°C"}
    )
    assert read_temperature_c(hass, "sensor.t") == 12.5


async def test_read_temperature_converts_fahrenheit(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.t", "32", {"unit_of_measurement": "°F"})
    assert read_temperature_c(hass, "sensor.t") == pytest.approx(0.0)


async def test_read_temperature_no_unit_assumes_celsius(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.t", "-3.0")
    assert read_temperature_c(hass, "sensor.t") == -3.0


async def test_read_temperature_invalid(hass: HomeAssistant) -> None:
    assert read_temperature_c(hass, "sensor.missing") is None
    for bad in (STATE_UNAVAILABLE, STATE_UNKNOWN, "", "NaN-text"):
        hass.states.async_set("sensor.t", bad)
        assert read_temperature_c(hass, "sensor.t") is None


def test_temperature_c_from_state_fahrenheit() -> None:
    state = State("sensor.t", "212", {"unit_of_measurement": "°F"})
    assert temperature_c_from_state(state) == pytest.approx(100.0)


# --------------------------------------------------------------------------- #
# formulas                                                                    #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("temp_c", "expected"),
    [
        (-10.0, "below_0"),
        (-0.001, "below_0"),
        (0.0, "0_to_10"),
        (9.9, "0_to_10"),
        (10.0, "10_to_20"),
        (19.9, "10_to_20"),
        (20.0, "above_20"),
        (35.0, "above_20"),
    ],
)
def test_temperature_band(temp_c: float, expected: str) -> None:
    assert _temperature_band(temp_c) == expected


async def test_local_day_windows_splits_on_local_midnight(
    hass: HomeAssistant,
) -> None:
    hass.config.time_zone = "UTC"
    start = dt_util.parse_datetime("2026-01-01T06:00:00+00:00")
    end = dt_util.parse_datetime("2026-01-03T09:00:00+00:00")
    windows = _local_day_windows(start, end, hass)
    # Partial first day, full middle day, partial last day.
    assert [(w[0].isoformat(), w[1].isoformat()) for w in windows] == [
        ("2026-01-01T06:00:00+00:00", "2026-01-02T00:00:00+00:00"),
        ("2026-01-02T00:00:00+00:00", "2026-01-03T00:00:00+00:00"),
        ("2026-01-03T00:00:00+00:00", "2026-01-03T09:00:00+00:00"),
    ]


async def test_local_day_windows_empty_when_end_not_after_start(
    hass: HomeAssistant,
) -> None:
    now = dt_util.utcnow()
    assert _local_day_windows(now, now, hass) == []


def test_range_loss_percent_needs_two_populated_bands() -> None:
    bands = [
        {"factory_kwh_per_100km": None},
        {"factory_kwh_per_100km": 18.0},
    ]
    assert _range_loss_percent(bands) is None


def test_range_loss_percent_none_when_warmest_zero() -> None:
    bands = [
        {"factory_kwh_per_100km": 20.0},
        {"factory_kwh_per_100km": 0.0},
    ]
    assert _range_loss_percent(bands) is None


# --------------------------------------------------------------------------- #
# TemperatureHistory.daily_average                                            #
# --------------------------------------------------------------------------- #


def _temp_history(hass: HomeAssistant) -> TemperatureHistory:
    entry = make_entry()
    return TemperatureHistory(hass, entry, temperature_entity="sensor.t")


async def test_daily_average_time_weighted(hass: HomeAssistant) -> None:
    """10 °C for the first 6h, 4 °C for the next 18h → weighted 5.5 °C."""
    history = _temp_history(hass)
    day_start = dt_util.parse_datetime("2026-01-01T00:00:00+00:00")
    seed_history(history, [
        (day_start, 10.0),
        (day_start + timedelta(hours=6), 4.0),
    ])
    avg = history.daily_average(day_start, day_start + timedelta(hours=24))
    # (10*6 + 4*18) / 24 = 5.5
    assert avg == pytest.approx(5.5)


async def test_daily_average_uses_value_held_at_window_start(
    hass: HomeAssistant,
) -> None:
    """A sample before the window still anchors the leading segment."""
    history = _temp_history(hass)
    base = dt_util.parse_datetime("2026-01-01T00:00:00+00:00")
    seed_history(history, [
        (base - timedelta(hours=12), 8.0),  # before the window
    ])
    avg = history.daily_average(base, base + timedelta(hours=24))
    assert avg == pytest.approx(8.0)


async def test_daily_average_none_without_anchor(hass: HomeAssistant) -> None:
    """No sample at or before the window start → no value to weight."""
    history = _temp_history(hass)
    base = dt_util.parse_datetime("2026-01-01T00:00:00+00:00")
    seed_history(history, [
        (base + timedelta(hours=30), 8.0),  # entirely after the window
    ])
    assert history.daily_average(base, base + timedelta(hours=24)) is None


async def test_daily_average_empty_and_degenerate(hass: HomeAssistant) -> None:
    history = _temp_history(hass)
    now = dt_util.utcnow()
    assert history.daily_average(now - timedelta(hours=1), now) is None
    seed_history(history, [(now - timedelta(hours=2), 5.0)])
    # end <= start
    assert history.daily_average(now, now) is None


# --------------------------------------------------------------------------- #
# Config flow                                                                 #
# --------------------------------------------------------------------------- #


async def test_config_flow_accepts_outside_temp_sensor(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    user_input = {
        "name": "Temp Car",
        "soc_sensor": "sensor.soc",
        "range_sensor": "sensor.range",
        "capacity_factory_kwh": 77.0,
        "capacity_actual_entity": "input_number.cap",
        CONF_OUTSIDE_TEMP_SENSOR: OUTSIDE_TEMP_ENTITY,
    }
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_OUTSIDE_TEMP_SENSOR] == OUTSIDE_TEMP_ENTITY


# --------------------------------------------------------------------------- #
# Sensor + wiring                                                             #
# --------------------------------------------------------------------------- #


async def _setup(hass: HomeAssistant, *, with_temp: bool):
    hass.config.time_zone = "UTC"
    hass.states.async_set(SOC_ENTITY, "50")
    hass.states.async_set(RANGE_ENTITY, "200", {"unit_of_measurement": "km"})
    hass.states.async_set(MILEAGE_ENTITY, "10000", {"unit_of_measurement": "km"})
    hass.states.async_set(CHARGING_ENTITY, "off")
    hass.states.async_set(ACTUAL_CAPACITY_ENTITY, "70.0")
    if with_temp:
        hass.states.async_set(
            OUTSIDE_TEMP_ENTITY, "12.0", {"unit_of_measurement": "°C"}
        )
        entry = make_entry(**{CONF_OUTSIDE_TEMP_SENSOR: OUTSIDE_TEMP_ENTITY})
    else:
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
    return None


async def test_sensor_absent_without_temperature_sensor(
    hass: HomeAssistant,
) -> None:
    entry = await _setup(hass, with_temp=False)
    assert hass.data[DOMAIN][entry.entry_id]["temperature_history"] is None
    assert _find_state(hass, "_efficiency_vs_temperature") is None


async def test_history_created_when_configured(hass: HomeAssistant) -> None:
    entry = await _setup(hass, with_temp=True)
    assert (
        hass.data[DOMAIN][entry.entry_id]["temperature_history"] is not None
    )


async def test_sensor_unavailable_without_temperature_samples(
    hass: HomeAssistant,
) -> None:
    """With the entity unavailable at startup the sensor has no data."""
    hass.config.time_zone = "UTC"
    hass.states.async_set(SOC_ENTITY, "50")
    hass.states.async_set(RANGE_ENTITY, "200", {"unit_of_measurement": "km"})
    hass.states.async_set(MILEAGE_ENTITY, "10000", {"unit_of_measurement": "km"})
    hass.states.async_set(CHARGING_ENTITY, "off")
    hass.states.async_set(ACTUAL_CAPACITY_ENTITY, "70.0")
    hass.states.async_set(OUTSIDE_TEMP_ENTITY, STATE_UNAVAILABLE)
    entry = make_entry(**{CONF_OUTSIDE_TEMP_SENSOR: OUTSIDE_TEMP_ENTITY})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    state = _find_state(hass, "_efficiency_vs_temperature")
    assert state is not None
    assert state.state == STATE_UNAVAILABLE


async def test_sensor_buckets_efficiency_by_temperature_band(
    hass: HomeAssistant,
) -> None:
    """A cold driving day and a warm one land in distinct bands.

    Day -3 (avg -5 °C): drove 100 km, used 30 % SoC.
    Day -2 (avg +25 °C): drove 100 km, used 20 % SoC.
    Factory (77 kWh): cold = 23.1 kWh/100 km, warm = 15.4 kWh/100 km.
    Range loss = (23.1 - 15.4) / 15.4 * 100 = 50 %.
    """
    entry = await _setup(hass, with_temp=True)
    domain = hass.data[DOMAIN][entry.entry_id]
    soc_history = domain["soc_history"]
    mileage_history = domain["mileage_history"]
    temperature_history = domain["temperature_history"]

    now = dt_util.utcnow()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    t3s = midnight - timedelta(days=3)
    t2s = midnight - timedelta(days=2)
    t1s = midnight - timedelta(days=1)

    seed_history(temperature_history, [
        (t3s, -5.0),
        (t2s, 25.0),
        (midnight, 12.0),
    ])
    seed_history(mileage_history, [
        (t3s, 1000.0),
        (t2s, 1100.0),
        (t1s, 1200.0),
    ])
    seed_history(soc_history, [
        (t3s, 100.0),
        (t2s, 70.0),
        (t1s, 50.0),
    ])

    for signal in (
        signal_temperature_history_updated(entry.entry_id),
        signal_soc_history_updated(entry.entry_id),
        signal_mileage_history_updated(entry.entry_id),
    ):
        async_dispatcher_send(hass, signal)
    await hass.async_block_till_done()

    state = _find_state(hass, "_efficiency_vs_temperature")
    assert state is not None
    # State = today's held average temperature.
    assert float(state.state) == pytest.approx(12.0)

    bands = {b["band"]: b for b in state.attributes["bands"]}
    cold = bands["below_0"]
    warm = bands["above_20"]
    assert cold["days"] == 1
    assert cold["distance_km"] == pytest.approx(100.0)
    assert cold["soc_consumed_percent"] == pytest.approx(30.0)
    assert cold["factory_kwh_per_100km"] == pytest.approx(23.1)
    assert warm["days"] == 1
    assert warm["factory_kwh_per_100km"] == pytest.approx(15.4)
    # Mild bands saw no driving.
    assert bands["0_to_10"]["days"] == 0
    assert bands["0_to_10"]["factory_kwh_per_100km"] is None

    assert state.attributes["range_loss_percent"] == pytest.approx(50.0)
    assert state.attributes["window_days"] == 15
