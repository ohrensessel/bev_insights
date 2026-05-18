"""Tests for `custom_components.bev_insights.util`."""
from __future__ import annotations

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State

from custom_components.bev_insights.util import (
    is_charging,
    read_distance_km,
    read_float,
)


async def test_read_float_returns_value(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.x", "42.5")
    assert read_float(hass, "sensor.x") == 42.5


async def test_read_float_missing_entity(hass: HomeAssistant) -> None:
    assert read_float(hass, "sensor.missing") is None


async def test_read_float_invalid_states(hass: HomeAssistant) -> None:
    for bad in (STATE_UNAVAILABLE, STATE_UNKNOWN, "", "not-a-number"):
        hass.states.async_set("sensor.x", bad)
        assert read_float(hass, "sensor.x") is None


async def test_read_distance_km_assumes_km_when_unitless(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.r", "120.0")
    assert read_distance_km(hass, "sensor.r") == 120.0


async def test_read_distance_km_converts_miles(hass: HomeAssistant) -> None:
    hass.states.async_set(
        "sensor.r", "100", {"unit_of_measurement": "mi"}
    )
    # 100 mi = 160.9344 km
    assert read_distance_km(hass, "sensor.r") == 160.9344


async def test_read_distance_km_converts_metres(hass: HomeAssistant) -> None:
    hass.states.async_set(
        "sensor.r", "2500", {"unit_of_measurement": "m"}
    )
    assert read_distance_km(hass, "sensor.r") == 2.5


async def test_read_distance_km_unknown_unit_falls_back_to_km(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set(
        "sensor.r", "10", {"unit_of_measurement": "furlongs"}
    )
    assert read_distance_km(hass, "sensor.r") == 10.0


async def test_read_distance_km_invalid(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.r", STATE_UNAVAILABLE)
    assert read_distance_km(hass, "sensor.r") is None


def test_is_charging_truthy_values() -> None:
    for value in ("on", "true", "charging", "ON", "Charging"):
        assert is_charging(State("binary_sensor.x", value)) is True


def test_is_charging_falsy_values() -> None:
    for value in ("off", "false", "not_charging", "discharging"):
        assert is_charging(State("binary_sensor.x", value)) is False


def test_is_charging_none_and_invalid() -> None:
    assert is_charging(None) is False
    assert is_charging(State("binary_sensor.x", STATE_UNAVAILABLE)) is False
    assert is_charging(State("binary_sensor.x", STATE_UNKNOWN)) is False
