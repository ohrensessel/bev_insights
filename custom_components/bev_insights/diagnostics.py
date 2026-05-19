"""Diagnostics support for BEV Insights.

Dumps the integration's per-entry state in a form suitable for attaching
to bug reports: config, options, charge-tracker baseline + last session,
rolling-history summaries, resolved capacity values, and a snapshot of
the source entities' current states. Identifying fields (title, unique
id) are redacted; entity IDs themselves are kept because they're what
the developer needs to triage "is the user wiring the right inputs".
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_CHARGING_SENSOR,
    CONF_MILEAGE_SENSOR,
    CONF_RANGE_SENSOR,
    CONF_SOC_SENSOR,
    DOMAIN,
)
from .tracker import EntityHistory

# Keys whose values are user-set free-form strings (entry title) or
# entity-id concatenations (unique_id) that occasionally embed VINs,
# names, or other identifying detail. Numeric readings, configured
# entity IDs, and timestamps are kept — they're the parts a developer
# needs to read in order to diagnose a problem.
TO_REDACT = {"title", "unique_id"}


def _state_snapshot(hass: HomeAssistant, entity_id: str | None) -> dict[str, Any] | None:
    """Capture a small view of an entity's current state. None on miss."""
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None:
        return {"entity_id": entity_id, "state": None}
    return {
        "entity_id": entity_id,
        "state": state.state,
        "unit_of_measurement": state.attributes.get("unit_of_measurement"),
    }


def _history_summary(
    history: EntityHistory | None,
) -> dict[str, Any] | None:
    """Compact view of a MileageHistory / SocHistory deque."""
    if history is None:
        return None
    oldest = history.oldest_sample
    latest = history.latest_sample
    return {
        "sample_count": history.sample_count,
        "oldest": {"timestamp": oldest[0].isoformat(), "value": oldest[1]}
        if oldest is not None
        else None,
        "latest": {"timestamp": latest[0].isoformat(), "value": latest[1]}
        if latest is not None
        else None,
    }


def _manifest_version(hass: HomeAssistant) -> str | None:
    """Return the running integration's version from its manifest."""
    integration = hass.data.get("integrations", {}).get(DOMAIN)
    return integration.manifest.get("version") if integration else None


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    domain_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    tracker = domain_data.get("tracker")
    mileage_history = domain_data.get("mileage_history")
    soc_history = domain_data.get("soc_history")
    capacity_factory = domain_data.get("capacity_factory")
    capacity_actual = domain_data.get("capacity_actual")

    sources: dict[str, Any] = {}
    for label, key in (
        ("soc", CONF_SOC_SENSOR),
        ("range", CONF_RANGE_SENSOR),
        ("charging", CONF_CHARGING_SENSOR),
        ("mileage", CONF_MILEAGE_SENSOR),
    ):
        sources[label] = _state_snapshot(hass, entry.data.get(key))

    capacities: dict[str, Any] = {}
    if capacity_factory is not None:
        capacities["factory"] = {
            "value_kwh": capacity_factory.current(),
            "source": capacity_factory.describe(),
        }
    if capacity_actual is not None:
        capacities["actual"] = {
            "value_kwh": capacity_actual.current(),
            "source": capacity_actual.describe(),
        }

    data: dict[str, Any] = {
        "version": _manifest_version(hass),
        "entry": {
            "title": entry.title,
            "entry_id": entry.entry_id,
            "unique_id": entry.unique_id,
            "version": entry.version,
            "state": str(entry.state),
            "data": dict(entry.data),
            "options": dict(entry.options),
        },
        "sources": sources,
        "capacities": capacities,
        "tracker": {
            "is_charging": tracker.is_charging if tracker is not None else None,
            "baseline": tracker.baseline if tracker is not None else None,
            "last_session": tracker.last_session if tracker is not None else None,
        },
        "histories": {
            "mileage": _history_summary(mileage_history),
            "soc": _history_summary(soc_history),
        },
    }

    return async_redact_data(data, TO_REDACT)
