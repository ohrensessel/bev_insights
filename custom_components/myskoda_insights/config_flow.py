"""Config flow for MySkoda Insights."""
from __future__ import annotations

from typing import Any

from homeassistant import config_entries
from homeassistant.helpers import selector
import voluptuous as vol

from .const import (
    CONF_CAPACITY_ACTUAL_ENTITY,
    CONF_CAPACITY_FACTORY,
    CONF_CHARGING_SENSOR,
    CONF_MILEAGE_SENSOR,
    CONF_NAME,
    CONF_RANGE_SENSOR,
    CONF_SOC_SENSOR,
    CONFIG_ENTRY_VERSION,
    DEFAULT_CAPACITY_KWH,
    DEFAULT_NAME,
    DOMAIN,
)


def _sensor_or_binary_selector() -> selector.EntitySelector:
    return selector.EntitySelector(
        selector.EntitySelectorConfig(domain=["sensor", "binary_sensor"])
    )


def _sensor_selector() -> selector.EntitySelector:
    return selector.EntitySelector(
        selector.EntitySelectorConfig(domain="sensor")
    )


def _capacity_entity_selector() -> selector.EntitySelector:
    """Picker for the live actual-capacity source.

    Accepts `input_number` (the common "user-editable helper" approach)
    and `sensor` (for outputs of other integrations or template sensors).
    """
    return selector.EntitySelector(
        selector.EntitySelectorConfig(domain=["input_number", "sensor"])
    )


def _factory_capacity_selector() -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=1.0,
            max=250.0,
            step=0.1,
            unit_of_measurement="kWh",
            mode=selector.NumberSelectorMode.BOX,
        )
    )


def _schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    defaults = defaults or {}

    fields: dict[Any, Any] = {
        vol.Required(
            CONF_NAME, default=defaults.get(CONF_NAME, DEFAULT_NAME)
        ): str,
        vol.Required(
            CONF_SOC_SENSOR, default=defaults.get(CONF_SOC_SENSOR)
        ): _sensor_selector(),
        vol.Required(
            CONF_RANGE_SENSOR, default=defaults.get(CONF_RANGE_SENSOR)
        ): _sensor_selector(),
        vol.Required(
            CONF_CAPACITY_FACTORY,
            default=defaults.get(CONF_CAPACITY_FACTORY, DEFAULT_CAPACITY_KWH),
        ): _factory_capacity_selector(),
    }

    # Actual-capacity entity: required, but we don't have a default value
    # to suggest unless the user already picked one (e.g. on reconfigure).
    actual_default = defaults.get(CONF_CAPACITY_ACTUAL_ENTITY)
    if actual_default is not None:
        fields[
            vol.Required(
                CONF_CAPACITY_ACTUAL_ENTITY, default=actual_default
            )
        ] = _capacity_entity_selector()
    else:
        fields[vol.Required(CONF_CAPACITY_ACTUAL_ENTITY)] = _capacity_entity_selector()

    charging_default = defaults.get(CONF_CHARGING_SENSOR)
    if charging_default is not None:
        fields[
            vol.Optional(CONF_CHARGING_SENSOR, default=charging_default)
        ] = _sensor_or_binary_selector()
    else:
        fields[vol.Optional(CONF_CHARGING_SENSOR)] = _sensor_or_binary_selector()

    mileage_default = defaults.get(CONF_MILEAGE_SENSOR)
    if mileage_default is not None:
        fields[
            vol.Optional(CONF_MILEAGE_SENSOR, default=mileage_default)
        ] = _sensor_selector()
    else:
        fields[vol.Optional(CONF_MILEAGE_SENSOR)] = _sensor_selector()

    return vol.Schema(fields)


class MySkodaInsightsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for MySkoda Insights."""

    VERSION = CONFIG_ENTRY_VERSION

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            unique = (
                f"{user_input[CONF_SOC_SENSOR]}|{user_input[CONF_RANGE_SENSOR]}"
            )
            await self.async_set_unique_id(unique)
            self._abort_if_unique_id_configured()

            return self.async_create_entry(
                title=user_input[CONF_NAME],
                data=user_input,
            )

        return self.async_show_form(
            step_id="user",
            data_schema=_schema(),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        entry = self._get_reconfigure_entry()

        if user_input is not None:
            return self.async_update_reload_and_abort(
                entry,
                title=user_input[CONF_NAME],
                data=user_input,
            )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_schema(entry.data),
        )
