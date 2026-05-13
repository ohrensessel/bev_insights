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

from .capacity import CapacitySource, EntityCapacity, FixedCapacity
from .const import (
    CONF_CAPACITY_ACTUAL_ENTITY,
    CONF_CAPACITY_FACTORY,
    CONF_CHARGING_SENSOR,
    CONF_MILEAGE_SENSOR,
    CONF_SOC_SENSOR,
    CONFIG_ENTRY_VERSION,
    DEFAULT_CAPACITY_KWH,
    DOMAIN,
)
from .tracker import ChargeTracker, MileageHistory, SocHistory

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

    soc_history = SocHistory(
        hass, entry, soc_entity=entry.data[CONF_SOC_SENSOR]
    )
    await soc_history.async_load()
    soc_history.async_start()

    # Build the two capacity sources up front so sensor.py doesn't need
    # to know how to read them — it just calls .current() per recalc.
    capacity_factory: CapacitySource = FixedCapacity(
        float(entry.data.get(CONF_CAPACITY_FACTORY, DEFAULT_CAPACITY_KWH))
    )
    capacity_actual: CapacitySource = EntityCapacity(
        hass, entry.data[CONF_CAPACITY_ACTUAL_ENTITY]
    )

    hass.data[DOMAIN][entry.entry_id] = {
        "data": dict(entry.data),
        "tracker": tracker,
        "mileage_history": mileage_history,
        "soc_history": soc_history,
        "capacity_factory": capacity_factory,
        "capacity_actual": capacity_actual,
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
            if soc_history := domain_data.get("soc_history"):
                await soc_history.async_stop()
    return unload_ok


async def async_migrate_entry(
    hass: HomeAssistant, entry: ConfigEntry
) -> bool:
    """Migrate older config entries.

    v1 → v2: the actual-capacity field used to be a float (kWh).
    From v2 onward it's an entity_id whose state is read live. We don't
    have an entity to point at, so we leave it unset and the user is
    prompted to fill it in on next reconfigure. The old value is dropped
    after being logged so the user can recreate it as an input_number.
    """
    if entry.version == 1:
        _LOGGER.warning(
            "Migrating MySkoda Insights config entry to v2: please create "
            "an input_number helper with your actual remaining capacity "
            "(the previous value was %.2f kWh) and select it via "
            "Reconfigure on the integration card.",
            float(entry.data.get("capacity_actual_kwh", 0.0)),
        )
        new_data = {k: v for k, v in entry.data.items() if k != "capacity_actual_kwh"}
        hass.config_entries.async_update_entry(
            entry, data=new_data, version=CONFIG_ENTRY_VERSION
        )
        # Setup will fail without a capacity_actual_entity; flag the entry
        # as needing reconfiguration. HA will surface a "Reconfigure"
        # prompt to the user in the UI.
        return False
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload integration when options or data change."""
    await hass.config_entries.async_reload(entry.entry_id)
