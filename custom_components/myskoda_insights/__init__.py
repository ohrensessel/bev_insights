"""The MySkoda Insights integration.

Consumes sensors from the myskoda integration
(https://github.com/skodaconnect/homeassistant-myskoda) and exposes
additional derived sensors.
"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import (
    CONF_CHARGING_SENSOR,
    CONF_MILEAGE_SENSOR,
    CONF_SOC_SENSOR,
    DOMAIN,
)
from .tracker import ChargeTracker, MileageHistory

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up MySkoda Insights from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    charging_entity = entry.data.get(CONF_CHARGING_SENSOR)
    mileage_entity = entry.data.get(CONF_MILEAGE_SENSOR)

    tracker: ChargeTracker | None = None
    if charging_entity and mileage_entity:
        tracker = ChargeTracker(
            hass,
            entry,
            charging_entity=charging_entity,
            mileage_entity=mileage_entity,
            soc_entity=entry.data[CONF_SOC_SENSOR],
        )
        await tracker.async_load()
        tracker.async_start()

    mileage_history: MileageHistory | None = None
    if mileage_entity:
        mileage_history = MileageHistory(
            hass, entry, mileage_entity=mileage_entity
        )
        await mileage_history.async_load()
        mileage_history.async_start()

    hass.data[DOMAIN][entry.entry_id] = {
        "data": dict(entry.data),
        "tracker": tracker,
        "mileage_history": mileage_history,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        domain_data = hass.data[DOMAIN].pop(entry.entry_id, None)
        if domain_data:
            if tracker := domain_data.get("tracker"):
                await tracker.async_stop()
            if mileage_history := domain_data.get("mileage_history"):
                await mileage_history.async_stop()
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload integration when options or data change."""
    await hass.config_entries.async_reload(entry.entry_id)
