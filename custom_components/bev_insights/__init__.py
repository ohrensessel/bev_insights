"""The BEV Insights integration.

Consumes a small set of source entities (SoC %, range km, optional
charging-state, optional mileage) from any upstream integration and
exposes derived sensors: full-battery range, state of health, efficiency
in multiple unit variants, last charge added, rolling-window energy and
distance, and more.

Originally built against the myskoda integration
(https://github.com/skodaconnect/homeassistant-myskoda); other source
integrations should work as long as the required entities are present
and report numeric states.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store

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
    LEGACY_DOMAIN,
    STORAGE_VERSION,
)
from .tracker import ChargeTracker, MileageHistory, SocHistory

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]

# Suffixes appended to the domain prefix to form the three storage filenames
# the integration writes. Kept in one place so the legacy-storage migration
# below and the runtime code agree on the layout.
_STORAGE_SUFFIXES: tuple[str, ...] = (
    "charge_tracker",
    "mileage_history",
    "soc_history",
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up BEV Insights from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    # If a v1→v2 repair issue was filed by `async_migrate_entry`, the entry
    # reaching setup means the user has reconfigured. Clear the issue.
    ir.async_delete_issue(hass, DOMAIN, _v1_migration_issue_id(entry))

    await _migrate_legacy_storage(hass, entry)

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
        old_kwh = float(entry.data.get("capacity_actual_kwh", 0.0))
        _LOGGER.warning(
            "Migrating BEV Insights config entry to v2: please create "
            "an input_number helper with your actual remaining capacity "
            "(the previous value was %.2f kWh) and select it via "
            "Reconfigure on the integration card.",
            old_kwh,
        )
        # Surface this in HA's Repairs panel so the user can't miss it.
        # The issue is cleared by `async_setup_entry` on the first
        # successful setup after reconfiguration.
        ir.async_create_issue(
            hass,
            DOMAIN,
            _v1_migration_issue_id(entry),
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="v1_capacity_migration",
            translation_placeholders={
                "old_kwh": f"{old_kwh:.2f}",
                "title": entry.title,
            },
        )
        new_data = {k: v for k, v in entry.data.items() if k != "capacity_actual_kwh"}
        # HA changed how the entry version is bumped during migration:
        # - On HA 2024.x, `entry.version` was a writable attribute and the
        #   `version=` kwarg on `async_update_entry` didn't exist (or was a
        #   silent no-op via **kwargs forwarding).
        # - On HA 2025.x+, `entry.version` is a read-only property and the
        #   only way to bump it is the `async_update_entry(version=...)`
        #   kwarg.
        # Try the older path first; AttributeError means we're on the newer
        # HA and need the kwarg form.
        try:
            entry.version = CONFIG_ENTRY_VERSION
        except AttributeError:
            hass.config_entries.async_update_entry(
                entry, data=new_data, version=CONFIG_ENTRY_VERSION
            )
        else:
            hass.config_entries.async_update_entry(entry, data=new_data)
        # Setup will fail without a capacity_actual_entity; flag the entry
        # as needing reconfiguration. HA will surface a "Reconfigure"
        # prompt to the user in the UI.
        return False
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload integration when options or data change."""
    await hass.config_entries.async_reload(entry.entry_id)


def _v1_migration_issue_id(entry: ConfigEntry) -> str:
    """Per-entry repair issue id for the v1→v2 capacity migration."""
    return f"v1_capacity_migration_{entry.entry_id}"


async def _migrate_legacy_storage(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """One-time migration of .storage data from the legacy domain.

    Prior to v1.0.0 this integration was named `myskoda_insights`. After
    the rename to `bev_insights`, an existing HA install will have its
    persisted state (charge baseline + last session, plus rolling SoC and
    mileage histories) under the legacy storage prefix. Since the entry_id
    changes when the user re-creates the config entry under the new
    domain, the strategy is:

      * Discover legacy storage filenames by globbing `.storage/`.
      * For each `legacy_filename → new_key` pair, if the new key does not
        yet hold data, read it through the Store API and write it back
        under the new key (re-keying it to the current entry_id).
      * Delete the legacy disk file afterwards.

    Single-instance assumption: the integration is meant to be configured
    once per vehicle. If a user happened to have multiple legacy entries
    around, this picks an arbitrary one — the migration warning makes
    that explicit. Re-runs are no-ops once the new keys hold data.

    The split between disk-glob (for discovery) and Store-API (for data
    movement) is what makes this code test-friendly: when running under
    `pytest_homeassistant_custom_component`'s mocked storage, Store reads
    and writes go through the in-memory mock dict while the disk glob
    still finds whatever legacy filenames the test pre-created.
    """
    storage_dir = Path(hass.config.path(".storage"))

    def _find_legacy_filenames() -> list[tuple[str, str]]:
        if not storage_dir.is_dir():
            return []
        results: list[tuple[str, str]] = []
        for suffix in _STORAGE_SUFFIXES:
            matches = sorted(storage_dir.glob(f"{LEGACY_DOMAIN}.{suffix}.*"))
            if matches:
                results.append((matches[0].name, suffix))
        return results

    legacy_pairs = await hass.async_add_executor_job(_find_legacy_filenames)
    if not legacy_pairs:
        return

    for legacy_filename, suffix in legacy_pairs:
        new_key = f"{DOMAIN}.{suffix}.{entry.entry_id}"
        new_store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, new_key)
        if await new_store.async_load() is not None:
            # New key already holds data — leave the legacy entry in place
            # so the user can decide what to do with it manually.
            continue
        legacy_store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, legacy_filename
        )
        data = await legacy_store.async_load()
        if data is not None:
            await new_store.async_save(data)
            _LOGGER.warning(
                "BEV Insights: migrated legacy storage %s → %s "
                "(rename from myskoda_insights to bev_insights)",
                legacy_filename,
                new_key,
            )
        # Cleanup: drop the legacy entry whether or not we migrated. In
        # production async_remove deletes the disk file; in tests it
        # clears the mocked dict. Either way, the legacy filename will
        # not show up in the next migration scan.
        await legacy_store.async_remove()
