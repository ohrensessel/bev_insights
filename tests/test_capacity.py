"""Tests for `custom_components.myskoda_insights.capacity`."""
from __future__ import annotations

from homeassistant.core import HomeAssistant

from custom_components.myskoda_insights.capacity import (
    EntityCapacity,
    FixedCapacity,
)


def test_fixed_capacity_returns_configured_value() -> None:
    assert FixedCapacity(77.0).current() == 77.0


def test_fixed_capacity_zero_or_negative_is_unusable() -> None:
    assert FixedCapacity(0.0).current() is None
    assert FixedCapacity(-1.0).current() is None


def test_fixed_capacity_has_no_source_entity() -> None:
    assert FixedCapacity(77.0).source_entity is None


def test_fixed_capacity_describe() -> None:
    assert "kWh" in FixedCapacity(77.0).describe()
    assert "fixed" in FixedCapacity(77.0).describe()


async def test_entity_capacity_reads_state(hass: HomeAssistant) -> None:
    hass.states.async_set("input_number.cap", "70.5")
    cap = EntityCapacity(hass, "input_number.cap")
    assert cap.current() == 70.5
    assert cap.source_entity == "input_number.cap"
    assert cap.describe() == "input_number.cap"


async def test_entity_capacity_returns_none_when_missing(hass: HomeAssistant) -> None:
    cap = EntityCapacity(hass, "input_number.does_not_exist")
    assert cap.current() is None


async def test_entity_capacity_returns_none_when_unavailable(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set("input_number.cap", "unavailable")
    cap = EntityCapacity(hass, "input_number.cap")
    assert cap.current() is None


async def test_entity_capacity_returns_none_for_non_positive(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set("input_number.cap", "0")
    assert EntityCapacity(hass, "input_number.cap").current() is None

    hass.states.async_set("input_number.cap", "-5")
    assert EntityCapacity(hass, "input_number.cap").current() is None


async def test_entity_capacity_picks_up_live_changes(hass: HomeAssistant) -> None:
    """Each .current() call re-reads — that's the point of EntityCapacity."""
    hass.states.async_set("input_number.cap", "70")
    cap = EntityCapacity(hass, "input_number.cap")
    assert cap.current() == 70.0
    hass.states.async_set("input_number.cap", "65")
    assert cap.current() == 65.0
