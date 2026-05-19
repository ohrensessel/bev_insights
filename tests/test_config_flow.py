"""Tests for the config flow (user step + reconfigure step)."""
from __future__ import annotations

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.bev_insights.const import (
    CONF_CAPACITY_ACTUAL_ENTITY,
    CONF_CAPACITY_FACTORY,
    CONF_CHARGING_SENSOR,
    CONF_MILEAGE_SENSOR,
    CONF_NAME,
    CONF_RANGE_SENSOR,
    CONF_SOC_SENSOR,
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


async def test_user_step_shows_form(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}


async def test_user_step_creates_entry(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    user_input = base_entry_data()
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == user_input[CONF_NAME]
    assert result["data"][CONF_SOC_SENSOR] == user_input[CONF_SOC_SENSOR]
    assert (
        result["data"][CONF_CAPACITY_ACTUAL_ENTITY]
        == user_input[CONF_CAPACITY_ACTUAL_ENTITY]
    )


async def test_user_step_minimal_required_fields(hass: HomeAssistant) -> None:
    """Optional charging + mileage fields may be omitted."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    user_input = {
        CONF_NAME: "Minimal",
        CONF_SOC_SENSOR: "sensor.soc",
        CONF_RANGE_SENSOR: "sensor.range",
        CONF_CAPACITY_FACTORY: 77.0,
        CONF_CAPACITY_ACTUAL_ENTITY: "input_number.cap",
    }
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert CONF_CHARGING_SENSOR not in result["data"]
    assert CONF_MILEAGE_SENSOR not in result["data"]


async def test_user_step_aborts_when_already_configured(
    hass: HomeAssistant,
) -> None:
    existing = make_entry()
    existing.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], base_entry_data()
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_entry_version_matches_constant(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    await hass.config_entries.flow.async_configure(
        result["flow_id"], base_entry_data()
    )
    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    assert entries[0].version == CONFIG_ENTRY_VERSION


async def test_reconfigure_step_updates_entry(hass: HomeAssistant) -> None:
    # Reconfigure triggers a reload; the entry has to be fully set up first
    # or the reload task hangs and pollutes teardown.
    hass.states.async_set(SOC_ENTITY, "50")
    hass.states.async_set(RANGE_ENTITY, "200")
    hass.states.async_set(MILEAGE_ENTITY, "10000")
    hass.states.async_set(CHARGING_ENTITY, "off")
    hass.states.async_set(ACTUAL_CAPACITY_ENTITY, "70.0")
    entry = make_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # `start_reconfigure_flow` is a MockConfigEntry shortcut added in a
    # newer pytest-ha plugin release. The HA-level API has been there for
    # longer, but the `SOURCE_RECONFIGURE` constant was added after the
    # declared minimum (2024.7) — its value has always been the literal
    # "reconfigure", so use the string directly to stay compatible.
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reconfigure", "entry_id": entry.entry_id},
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    new_data = base_entry_data(capacity_factory_kwh=82.0)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], new_data
    )
    await hass.async_block_till_done()
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_CAPACITY_FACTORY] == 82.0
