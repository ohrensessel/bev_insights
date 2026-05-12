"""Config flow for MySkoda Insights."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers import selector

from .const import (
    CONF_NAME,
    CONF_RANGE_SENSOR,
    CONF_SOC_SENSOR,
    DEFAULT_NAME,
    DOMAIN,
)


def _sensor_selector() -> selector.EntitySelector:
    return selector.EntitySelector(
        selector.EntitySelectorConfig(domain="sensor")
    )


def _schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_NAME, default=defaults.get(CONF_NAME, DEFAULT_NAME)
            ): str,
            vol.Required(
                CONF_SOC_SENSOR, default=defaults.get(CONF_SOC_SENSOR)
            ): _sensor_selector(),
            vol.Required(
                CONF_RANGE_SENSOR, default=defaults.get(CONF_RANGE_SENSOR)
            ): _sensor_selector(),
        }
    )


class MySkodaInsightsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for MySkoda Insights."""

    VERSION = 1

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
