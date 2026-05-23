"""Snapshot tests for the public-facing entity schema and the diagnostics dump.

These guard the integration's *user-visible API*:
- entity-set + device_class / state_class / unit_of_measurement
- diagnostics-dump shape

Any breaking change (renamed entity, removed sensor, units flip,
diagnostics key drop) will fail these tests with a diff against the
checked-in snapshot. Run `pytest --snapshot-update` to accept an
intentional change.
"""
from __future__ import annotations

import re
from typing import Any

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bev_insights.const import CONFIG_ENTRY_VERSION, DOMAIN
from custom_components.bev_insights.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .common import (
    ACTUAL_CAPACITY_ENTITY,
    CHARGING_ENTITY,
    MILEAGE_ENTITY,
    RANGE_ENTITY,
    SOC_ENTITY,
    base_entry_data,
)

# Deterministic entry id keeps the snapshot stable across runs. Real
# entry ids are random UUIDs.
_ENTRY_ID = "snapshot_test_entry"


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    """Spin up a fully-wired integration with a deterministic entry id."""
    hass.states.async_set(SOC_ENTITY, "50")
    hass.states.async_set(RANGE_ENTITY, "200", {"unit_of_measurement": "km"})
    hass.states.async_set(MILEAGE_ENTITY, "10000", {"unit_of_measurement": "km"})
    hass.states.async_set(CHARGING_ENTITY, "off")
    hass.states.async_set(ACTUAL_CAPACITY_ENTITY, "70.0")

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=CONFIG_ENTRY_VERSION,
        data=base_entry_data(),
        title="Snapshot Test",
        entry_id=_ENTRY_ID,
        unique_id="snapshot|test",
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


# --------------------------------------------------------------------------- #
# Entity schema                                                               #
# --------------------------------------------------------------------------- #


async def test_entity_schema_snapshot(hass: HomeAssistant, snapshot) -> None:
    """Snapshot every derived entity + its key device/state/unit attributes.

    Catches accidental renames, class changes, missing sensors, and unit
    flips — anything that would silently break a user's dashboard, LTS
    series, or automations.
    """
    entry = await _setup(hass)
    registry = hass.data["entity_registry"]

    schema: list[dict[str, Any]] = []
    for entry_obj in registry.entities.values():
        if entry_obj.config_entry_id != entry.entry_id:
            continue
        # Strip the per-entry prefix so the snapshot doesn't carry the
        # synthetic entry_id around (kept stable here, but cleaner this way).
        unique_id_suffix = entry_obj.unique_id.removeprefix(f"{entry.entry_id}_")
        state = hass.states.get(entry_obj.entity_id)
        attrs = state.attributes if state else {}
        schema.append(
            {
                "unique_id_suffix": unique_id_suffix,
                "translation_key": entry_obj.translation_key,
                "device_class": (
                    entry_obj.device_class or attrs.get("device_class")
                ),
                "state_class": attrs.get("state_class"),
                "unit_of_measurement": attrs.get("unit_of_measurement"),
                "entity_category": (
                    entry_obj.entity_category.value
                    if entry_obj.entity_category
                    else None
                ),
            }
        )
    schema.sort(key=lambda e: e["unique_id_suffix"])

    assert schema == snapshot


# --------------------------------------------------------------------------- #
# Diagnostics dump                                                            #
# --------------------------------------------------------------------------- #


_ISO_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:\d{2}|Z)?$"
)


def _scrub_volatile(value: Any) -> Any:
    """Replace per-run-volatile fields with placeholders.

    Currently scrubs ISO-8601 timestamps; values like sample counts,
    states, capacity, entity_ids, etc. are kept verbatim so any change
    to them surfaces as a snapshot diff.
    """
    if isinstance(value, dict):
        return {k: _scrub_volatile(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_volatile(v) for v in value]
    if isinstance(value, str) and _ISO_TIMESTAMP_RE.match(value):
        return "<ISO_TIMESTAMP>"
    return value


async def test_diagnostics_snapshot(hass: HomeAssistant, snapshot) -> None:
    """Snapshot the diagnostics-dump schema and concrete values.

    Timestamps are scrubbed (they're set to `dt_util.utcnow()` during
    fixture setup so they shift every run). Everything else — keys,
    nesting, redaction, sensor values, capacity figures — is held to
    the checked-in snapshot.
    """
    entry = await _setup(hass)
    data = await async_get_config_entry_diagnostics(hass, entry)
    assert _scrub_volatile(data) == snapshot
