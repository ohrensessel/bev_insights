"""Repair-issue management for missing or removed source entities.

Surfaces a yellow "needs attention" banner in HA's Repairs panel when a
configured source entity (SoC, range, charging, mileage, actual
capacity) is no longer registered with HA — typical causes are the user
renaming the entity, uninstalling the upstream integration, or
restoring from a backup without it. Without this, derived sensors just
silently go unavailable and the user has to figure out the cause.

Detection is "entity has no state object". HA creates a state for every
known entity (including unavailable ones), so a missing state means the
entity isn't on the bus at all — a real configuration problem, not a
transient blip.
"""
from __future__ import annotations

from collections.abc import Iterable
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .const import (
    CONF_CAPACITY_ACTUAL_ENTITY,
    CONF_CHARGING_SENSOR,
    CONF_MILEAGE_SENSOR,
    CONF_RANGE_SENSOR,
    CONF_SOC_SENSOR,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

# Order is shown in the Repairs panel; lead with the always-required ones.
_TRACKED_CONF_KEYS: tuple[str, ...] = (
    CONF_SOC_SENSOR,
    CONF_RANGE_SENSOR,
    CONF_CAPACITY_ACTUAL_ENTITY,
    CONF_CHARGING_SENSOR,
    CONF_MILEAGE_SENSOR,
)


def _issue_id(entry: ConfigEntry, conf_key: str) -> str:
    """Per-entry, per-conf-key issue identifier."""
    return f"missing_source_entity_{entry.entry_id}_{conf_key}"


def _configured_entities(entry: ConfigEntry) -> Iterable[tuple[str, str]]:
    """Yield (conf_key, entity_id) for each currently configured source."""
    for conf_key in _TRACKED_CONF_KEYS:
        entity_id = entry.data.get(conf_key)
        if entity_id:
            yield conf_key, entity_id


@callback
def _check_once(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Synchronously update repair issues for all configured entities."""
    for conf_key, entity_id in _configured_entities(entry):
        issue_id = _issue_id(entry, conf_key)
        if hass.states.get(entity_id) is None:
            ir.async_create_issue(
                hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="missing_source_entity",
                translation_placeholders={
                    "entity_id": entity_id,
                    "conf_key": conf_key,
                    "title": entry.title,
                },
            )
        else:
            ir.async_delete_issue(hass, DOMAIN, issue_id)


async def async_setup_repairs(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Install repair-issue management for an entry.

    - First check fires after HA has finished starting so the upstream
      integration has had its chance to register entities. On a runtime
      reload (HA already running) the check happens immediately.
    - Subsequent checks fire on `entity_registry_updated` events that
      touch one of our configured entity_ids, in either direction
      (entity created, removed, or renamed).
    """
    configured_ids = {entity_id for _, entity_id in _configured_entities(entry)}

    @callback
    def _on_registry_update(event: Event[Any]) -> None:
        # `entity_id` is the entity touched; `old_entity_id` appears on
        # rename events. Re-check if either side concerns us.
        affected = event.data.get("entity_id") or event.data.get("old_entity_id")
        if affected in configured_ids:
            _check_once(hass, entry)

    @callback
    def _initial_check(_event: Event[Any] | None = None) -> None:
        _check_once(hass, entry)

    if hass.is_running:
        _initial_check()
    else:
        entry.async_on_unload(
            hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STARTED, _initial_check
            )
        )

    entry.async_on_unload(
        hass.bus.async_listen("entity_registry_updated", _on_registry_update)
    )


@callback
def async_clear_repairs(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Clear any repair issues filed for this entry."""
    for conf_key, _ in _configured_entities(entry):
        ir.async_delete_issue(hass, DOMAIN, _issue_id(entry, conf_key))
