"""Tests for `async_setup_entry`, `async_unload_entry`, `async_migrate_entry`."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.myskoda_insights.const import (
    CONF_CAPACITY_ACTUAL_ENTITY,
    CONFIG_ENTRY_VERSION,
    DOMAIN,
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


async def _prime_states(hass: HomeAssistant) -> None:
    """Source states the integration reads on setup."""
    hass.states.async_set(SOC_ENTITY, "75")
    hass.states.async_set(RANGE_ENTITY, "300", {"unit_of_measurement": "km"})
    hass.states.async_set(MILEAGE_ENTITY, "12000", {"unit_of_measurement": "km"})
    hass.states.async_set(CHARGING_ENTITY, "off")
    hass.states.async_set(ACTUAL_CAPACITY_ENTITY, "70.0")


async def test_setup_entry_loaded(hass: HomeAssistant) -> None:
    await _prime_states(hass)
    entry = make_entry()
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id) is True
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.entry_id in hass.data[DOMAIN]


async def test_unload_entry_clears_domain_data(hass: HomeAssistant) -> None:
    await _prime_states(hass)
    entry = make_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert await hass.config_entries.async_unload(entry.entry_id) is True
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert entry.entry_id not in hass.data[DOMAIN]


async def test_setup_without_optional_charging_mileage(hass: HomeAssistant) -> None:
    """Without charging+mileage the tracker isn't built; setup still succeeds."""
    hass.states.async_set(SOC_ENTITY, "75")
    hass.states.async_set(RANGE_ENTITY, "300")
    hass.states.async_set(ACTUAL_CAPACITY_ENTITY, "70")

    data = base_entry_data()
    data.pop("charging_sensor")
    data.pop("mileage_sensor")
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=CONFIG_ENTRY_VERSION,
        data=data,
        title=data["name"],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    await hass.async_block_till_done()
    domain_data = hass.data[DOMAIN][entry.entry_id]
    assert domain_data["tracker"] is None
    assert domain_data["mileage_history"] is None


async def test_migrate_v1_to_v2_flags_for_reconfigure(hass: HomeAssistant) -> None:
    """A v1 entry has its old `capacity_actual_kwh` stripped and is left unset.

    Setup intentionally fails (`async_migrate_entry` returns False) so the user
    is prompted to reconfigure with a new entity reference.
    """
    v1_data = {
        "name": "Old Entry",
        "soc_sensor": SOC_ENTITY,
        "range_sensor": RANGE_ENTITY,
        "capacity_factory_kwh": 77.0,
        # legacy v1 field — must be stripped during migration.
        "capacity_actual_kwh": 68.5,
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        data=v1_data,
        title="Old Entry",
    )
    entry.add_to_hass(hass)

    # Migration returns False, so setup should not succeed.
    assert await hass.config_entries.async_setup(entry.entry_id) is False
    await hass.async_block_till_done()

    # Version is bumped, old key is stripped, new key is not yet set.
    assert entry.version == CONFIG_ENTRY_VERSION
    assert "capacity_actual_kwh" not in entry.data
    assert CONF_CAPACITY_ACTUAL_ENTITY not in entry.data
