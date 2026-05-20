"""Config flow for BEV Insights."""
from __future__ import annotations

import contextlib
from typing import Any

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector
import voluptuous as vol

from .const import (
    CONF_CAPACITY_ACTUAL_ENTITY,
    CONF_CAPACITY_FACTORY,
    CONF_CHARGING_SENSOR,
    CONF_HISTORY_DAYS,
    CONF_MILEAGE_SENSOR,
    CONF_MIN_MEASURED_RANGE_KM,
    CONF_MIN_MEASURED_RANGE_SOC_PERCENT,
    CONF_NAME,
    CONF_RANGE_SENSOR,
    CONF_SOC_SENSOR,
    CONF_STANDSTILL_MOVEMENT_THRESHOLD_KM,
    CONFIG_ENTRY_VERSION,
    DEFAULT_CAPACITY_KWH,
    DEFAULT_NAME,
    DOMAIN,
    MILEAGE_HISTORY_DAYS,
    MIN_MEASURED_RANGE_KM,
    MIN_MEASURED_RANGE_SOC_PERCENT,
    STANDSTILL_MOVEMENT_THRESHOLD_KM,
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


class BevInsightsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for BEV Insights."""

    VERSION = CONFIG_ENTRY_VERSION

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> BevInsightsOptionsFlow:
        """Expose the options flow on the integration card."""
        return BevInsightsOptionsFlow(config_entry)

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
        # `_get_reconfigure_entry` and `async_update_reload_and_abort` are
        # ConfigFlow convenience helpers added after our declared minimum HA
        # (2024.7). Use the longer-standing primitives instead: look up the
        # entry from the flow context, update it, schedule a reload as a
        # background task (matching what the helper does internally), and
        # abort with the user-facing reason.
        entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        if entry is None:
            return self.async_abort(reason="unknown_entry")

        if user_input is not None:
            self.hass.config_entries.async_update_entry(
                entry, title=user_input[CONF_NAME], data=user_input
            )
            self.hass.async_create_task(
                self.hass.config_entries.async_reload(entry.entry_id)
            )
            return self.async_abort(reason="reconfigure_successful")

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_schema(dict(entry.data)),
        )


def _options_schema(current: dict[str, Any]) -> vol.Schema:
    """Schema for the options form. Defaults match the module-level constants."""
    return vol.Schema(
        {
            vol.Optional(
                CONF_MIN_MEASURED_RANGE_KM,
                default=current.get(
                    CONF_MIN_MEASURED_RANGE_KM, MIN_MEASURED_RANGE_KM
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.0,
                    max=200.0,
                    step=1.0,
                    unit_of_measurement="km",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_MIN_MEASURED_RANGE_SOC_PERCENT,
                default=current.get(
                    CONF_MIN_MEASURED_RANGE_SOC_PERCENT,
                    MIN_MEASURED_RANGE_SOC_PERCENT,
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.0,
                    max=50.0,
                    step=0.5,
                    unit_of_measurement="%",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_HISTORY_DAYS,
                default=current.get(CONF_HISTORY_DAYS, MILEAGE_HISTORY_DAYS),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=2,
                    max=60,
                    step=1,
                    unit_of_measurement="days",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_STANDSTILL_MOVEMENT_THRESHOLD_KM,
                default=current.get(
                    CONF_STANDSTILL_MOVEMENT_THRESHOLD_KM,
                    STANDSTILL_MOVEMENT_THRESHOLD_KM,
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.0,
                    max=5.0,
                    step=0.1,
                    unit_of_measurement="km",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
        }
    )


class BevInsightsOptionsFlow(config_entries.OptionsFlow):
    """Options flow: tune thresholds and history retention.

    All three options have sensible defaults that match the values the
    integration shipped with before they were made configurable; users
    who don't touch this form get the same behaviour as before.
    """

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        # HA changed `OptionsFlow.config_entry` from a writable attribute
        # to a read-only property auto-populated by the OptionsFlowManager
        # (the deprecation note in HA 2025.1 promised removal in 2025.12).
        # On newer HA the property is read-only and `self.config_entry`
        # works via the framework anyway, so swallow AttributeError.
        with contextlib.suppress(AttributeError):
            self.config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=_options_schema(dict(self.config_entry.options)),
        )
