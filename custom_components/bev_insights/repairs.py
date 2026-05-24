"""Repair-issue management for source-entity health.

Surfaces yellow "needs attention" banners in HA's Repairs panel for two
classes of problem:

1. **Missing / removed source entities** — a configured SoC, range,
   charging, mileage, or actual-capacity entity isn't registered with
   HA. Typical causes: the user renamed the entity, uninstalled the
   upstream integration, or restored from a backup without it.
   Detection is "entity has no state object" (HA creates a state for
   every known entity including unavailable ones, so a missing state
   means the entity isn't on the bus at all).

2. **Value-level sanity checks** — the entity exists but its value
   would degrade or break derived sensors:
   - Actual-capacity helper outside the plausible 5–200 kWh range
     (typo on an `input_number`, wrong sensor selected).
   - SoC source reporting < 0 or > 100 % (broken upstream integration).
   - Mileage going backwards by more than a kilometre (broken upstream
     integration; would cause `distance_*` deltas to flip negative
     before our `_postprocess_delta` clamps them to zero).
   - Range / mileage source reporting a distance unit we don't know how
     to convert (anything not in `_DISTANCE_TO_KM`).

Without these, derived sensors silently produce garbage or go
unavailable and the user has to figure out the cause from the logs.
"""
from __future__ import annotations

from collections.abc import Iterable
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_state_change_event

from .const import (
    CONF_CAPACITY_ACTUAL_ENTITY,
    CONF_CHARGING_SENSOR,
    CONF_MILEAGE_SENSOR,
    CONF_RANGE_SENSOR,
    CONF_SOC_SENSOR,
    DOMAIN,
)
from .util import _DISTANCE_TO_KM, INVALID_STATES, read_distance_km, read_float

_LOGGER = logging.getLogger(__name__)

# Order is shown in the Repairs panel; lead with the always-required ones.
_TRACKED_CONF_KEYS: tuple[str, ...] = (
    CONF_SOC_SENSOR,
    CONF_RANGE_SENSOR,
    CONF_CAPACITY_ACTUAL_ENTITY,
    CONF_CHARGING_SENSOR,
    CONF_MILEAGE_SENSOR,
)

# Plausibility bounds for the actual-capacity helper. Below 5 kWh would
# imply a city e-scooter battery; above 200 kWh exceeds the biggest BEV
# packs in production (Hummer EV ~ 213 kWh is the realistic ceiling, so
# 200 catches typos like "770" without flagging a legitimate huge pack).
_PLAUSIBLE_CAPACITY_MIN_KWH = 5.0
_PLAUSIBLE_CAPACITY_MAX_KWH = 200.0

# Tolerance for mileage going backwards. Odometers are integer-km on most
# upstream integrations; tiny dips usually indicate sensor jitter or a
# brief unavailable-then-available transition where the new value lags
# the previous. A drop of more than a kilometre is a real problem.
_MILEAGE_REVERSAL_THRESHOLD_KM = 1.0

# Module-level memory of the highest mileage we've seen per entry. Used
# to detect drops without keeping a full sample history (MileageHistory
# already does that for the sensors). Per-entry so multiple BEVs don't
# cross-contaminate. Cleared on entry unload via `async_clear_repairs`.
_LAST_SEEN_MILEAGE_KM: dict[str, float] = {}


def _issue_id(entry: ConfigEntry, conf_key: str) -> str:
    """Per-entry, per-conf-key issue identifier for missing-entity issues."""
    return f"missing_source_entity_{entry.entry_id}_{conf_key}"


def _value_issue_id(entry: ConfigEntry, kind: str, suffix: str | None = None) -> str:
    """Per-entry, per-kind issue identifier for value-level issues.

    `suffix` disambiguates when the same kind applies to multiple
    entities (e.g. the unknown-unit check runs against both the range
    and the mileage entity).
    """
    base = f"value_{kind}_{entry.entry_id}"
    return f"{base}_{suffix}" if suffix else base


def _configured_entities(entry: ConfigEntry) -> Iterable[tuple[str, str]]:
    """Yield (conf_key, entity_id) for each currently configured source."""
    for conf_key in _TRACKED_CONF_KEYS:
        entity_id = entry.data.get(conf_key)
        if entity_id:
            yield conf_key, entity_id


@callback
def _check_once(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Synchronously update missing-entity issues for all configured entities."""
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


@callback
def _check_capacity_value(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """File / clear `value_capacity_out_of_range` for the actual-capacity helper."""
    entity_id = entry.data.get(CONF_CAPACITY_ACTUAL_ENTITY)
    if not entity_id:
        return
    issue_id = _value_issue_id(entry, "capacity_out_of_range")
    value = read_float(hass, entity_id)
    # Entity unavailable / unknown / un-parseable: the missing-entity or
    # data-quality conditions are handled elsewhere; don't double-flag.
    if value is None:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    if (
        value < _PLAUSIBLE_CAPACITY_MIN_KWH
        or value > _PLAUSIBLE_CAPACITY_MAX_KWH
    ):
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="value_capacity_out_of_range",
            translation_placeholders={
                "entity_id": entity_id,
                "value": f"{value:g}",
                "min_kwh": f"{_PLAUSIBLE_CAPACITY_MIN_KWH:g}",
                "max_kwh": f"{_PLAUSIBLE_CAPACITY_MAX_KWH:g}",
                "title": entry.title,
            },
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, issue_id)


@callback
def _check_soc_value(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """File / clear `value_soc_out_of_range` for the SoC source entity."""
    entity_id = entry.data.get(CONF_SOC_SENSOR)
    if not entity_id:
        return
    issue_id = _value_issue_id(entry, "soc_out_of_range")
    state = hass.states.get(entity_id)
    if state is None or state.state in INVALID_STATES:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    try:
        value = float(state.state)
    except (TypeError, ValueError):
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    if value < 0.0 or value > 100.0:
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="value_soc_out_of_range",
            translation_placeholders={
                "entity_id": entity_id,
                "value": f"{value:g}",
                "title": entry.title,
            },
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, issue_id)


@callback
def _check_mileage_reversal(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """File / clear `value_mileage_went_backwards` when the odometer drops.

    Uses module-level per-entry state so we can detect the reversal
    without re-querying recorder history. The issue clears when the
    odometer catches back up to (or passes) the prior peak — a transient
    glitch resolves itself, a real reversal stays flagged until the user
    fixes the upstream integration and the odometer climbs through.
    """
    entity_id = entry.data.get(CONF_MILEAGE_SENSOR)
    if not entity_id:
        return
    issue_id = _value_issue_id(entry, "mileage_went_backwards")
    value = read_distance_km(hass, entity_id)
    if value is None:
        return  # Entity unavailable — leave any prior issue intact.
    prev = _LAST_SEEN_MILEAGE_KM.get(entry.entry_id)
    if prev is None:
        _LAST_SEEN_MILEAGE_KM[entry.entry_id] = value
        return
    if value + _MILEAGE_REVERSAL_THRESHOLD_KM < prev:
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="value_mileage_went_backwards",
            translation_placeholders={
                "entity_id": entity_id,
                "previous_km": f"{prev:.1f}",
                "current_km": f"{value:.1f}",
                "title": entry.title,
            },
        )
    elif value >= prev:
        # Odometer reached / passed the old peak — promote it and clear.
        _LAST_SEEN_MILEAGE_KM[entry.entry_id] = value
        ir.async_delete_issue(hass, DOMAIN, issue_id)


@callback
def _check_distance_unit(
    hass: HomeAssistant, entry: ConfigEntry, conf_key: str
) -> None:
    """File / clear `value_unknown_distance_unit` for one distance entity.

    Run once per distance-typed source entity. The integration's
    `read_distance_km` falls back to "km" when it sees an unknown unit
    (with a log warning), so the symptom is silently-wrong values
    rather than a hard error — exactly the kind of thing Repairs
    catches better than the user reading logs.
    """
    entity_id = entry.data.get(conf_key)
    if not entity_id:
        return
    issue_id = _value_issue_id(entry, "unknown_distance_unit", suffix=conf_key)
    state = hass.states.get(entity_id)
    if state is None or state.state in INVALID_STATES:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    unit = state.attributes.get("unit_of_measurement")
    # Missing unit is fine — our reader defaults to km. We only flag
    # units we explicitly can't interpret.
    if unit is None or unit in _DISTANCE_TO_KM:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="value_unknown_distance_unit",
        translation_placeholders={
            "entity_id": entity_id,
            "conf_key": conf_key,
            "unit": str(unit),
            "title": entry.title,
        },
    )


@callback
def _check_values_once(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Run every value-level check synchronously."""
    _check_capacity_value(hass, entry)
    _check_soc_value(hass, entry)
    _check_mileage_reversal(hass, entry)
    for conf_key in (CONF_MILEAGE_SENSOR, CONF_RANGE_SENSOR):
        _check_distance_unit(hass, entry, conf_key)


async def async_setup_repairs(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Install repair-issue management for an entry.

    - First checks (both missing-entity and value-level) fire after HA
      has finished starting so the upstream integration has had its
      chance to register entities and report values. On a runtime
      reload (HA already running) the checks happen immediately.
    - Missing-entity re-checks fire on `entity_registry_updated` events
      that touch one of our configured entity_ids.
    - Value-level re-checks fire on state changes of the relevant
      configured entities. SoC and capacity changes recheck themselves;
      mileage changes drive both the reversal and the unit check; range
      changes drive only the unit check.
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
    def _on_value_change(_event: Event[Any]) -> None:
        # Cheap and consolidated — value checks are O(few) and only
        # touch entities we control. Running all of them on any value
        # tick avoids stale-state windows from stale per-entity
        # callback closures.
        _check_values_once(hass, entry)

    @callback
    def _initial_check(_event: Event[Any] | None = None) -> None:
        _check_once(hass, entry)
        _check_values_once(hass, entry)

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

    # Listen to state changes on the entities whose values feed any of
    # the value checks. Listing them explicitly (rather than reusing
    # `configured_ids`) makes the wiring explicit and skips the
    # charging entity, whose value isn't sanity-checked.
    value_listened_ids: list[str] = []
    for conf_key in (
        CONF_SOC_SENSOR,
        CONF_RANGE_SENSOR,
        CONF_CAPACITY_ACTUAL_ENTITY,
        CONF_MILEAGE_SENSOR,
    ):
        entity_id = entry.data.get(conf_key)
        if entity_id:
            value_listened_ids.append(entity_id)
    if value_listened_ids:
        entry.async_on_unload(
            async_track_state_change_event(
                hass, value_listened_ids, _on_value_change
            )
        )


@callback
def async_clear_repairs(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Clear any repair issues filed for this entry and drop cached state."""
    for conf_key, _ in _configured_entities(entry):
        ir.async_delete_issue(hass, DOMAIN, _issue_id(entry, conf_key))
    for kind in ("capacity_out_of_range", "soc_out_of_range", "mileage_went_backwards"):
        ir.async_delete_issue(hass, DOMAIN, _value_issue_id(entry, kind))
    for conf_key in (CONF_MILEAGE_SENSOR, CONF_RANGE_SENSOR):
        ir.async_delete_issue(
            hass, DOMAIN, _value_issue_id(entry, "unknown_distance_unit", suffix=conf_key)
        )
    _LAST_SEEN_MILEAGE_KM.pop(entry.entry_id, None)
