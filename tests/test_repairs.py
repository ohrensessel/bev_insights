"""Tests for the missing-source-entity repair-issue flow."""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from custom_components.bev_insights.const import (
    CONF_CAPACITY_ACTUAL_ENTITY,
    CONF_SOC_SENSOR,
    DOMAIN,
)
from custom_components.bev_insights.repairs import _issue_id

from .common import (
    ACTUAL_CAPACITY_ENTITY,
    CHARGING_ENTITY,
    MILEAGE_ENTITY,
    RANGE_ENTITY,
    SOC_ENTITY,
    make_entry,
)


async def _prime_all(hass: HomeAssistant) -> None:
    """Make every configured source entity present (no issues expected)."""
    hass.states.async_set(SOC_ENTITY, "75")
    hass.states.async_set(RANGE_ENTITY, "300", {"unit_of_measurement": "km"})
    hass.states.async_set(MILEAGE_ENTITY, "12000", {"unit_of_measurement": "km"})
    hass.states.async_set(CHARGING_ENTITY, "off")
    hass.states.async_set(ACTUAL_CAPACITY_ENTITY, "70.0")


# --------------------------------------------------------------------------- #
# Happy path                                                                  #
# --------------------------------------------------------------------------- #


async def test_no_issues_when_all_entities_present(hass: HomeAssistant) -> None:
    await _prime_all(hass)
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    await hass.async_block_till_done()

    issues = ir.async_get(hass)
    for conf_key in (
        CONF_SOC_SENSOR,
        CONF_CAPACITY_ACTUAL_ENTITY,
    ):
        assert issues.async_get_issue(DOMAIN, _issue_id(entry, conf_key)) is None


# --------------------------------------------------------------------------- #
# Initial detection                                                           #
# --------------------------------------------------------------------------- #


async def test_issue_created_when_source_entity_missing_at_setup(
    hass: HomeAssistant,
) -> None:
    """SoC sensor never registered → repair issue is filed."""
    # Prime everything except SoC.
    hass.states.async_set(RANGE_ENTITY, "300", {"unit_of_measurement": "km"})
    hass.states.async_set(MILEAGE_ENTITY, "12000", {"unit_of_measurement": "km"})
    hass.states.async_set(CHARGING_ENTITY, "off")
    hass.states.async_set(ACTUAL_CAPACITY_ENTITY, "70.0")

    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    await hass.async_block_till_done()

    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, _issue_id(entry, CONF_SOC_SENSOR)
    )
    assert issue is not None
    assert issue.severity == ir.IssueSeverity.WARNING
    assert issue.translation_key == "missing_source_entity"
    assert issue.translation_placeholders == {
        "entity_id": SOC_ENTITY,
        "conf_key": CONF_SOC_SENSOR,
        "title": entry.title,
    }


# --------------------------------------------------------------------------- #
# Runtime detection                                                           #
# --------------------------------------------------------------------------- #


async def test_issue_clears_when_missing_entity_appears(
    hass: HomeAssistant,
) -> None:
    """When the previously missing entity comes back, the issue clears."""
    # Set up with SoC missing.
    hass.states.async_set(RANGE_ENTITY, "300", {"unit_of_measurement": "km"})
    hass.states.async_set(MILEAGE_ENTITY, "12000", {"unit_of_measurement": "km"})
    hass.states.async_set(CHARGING_ENTITY, "off")
    hass.states.async_set(ACTUAL_CAPACITY_ENTITY, "70.0")

    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    await hass.async_block_till_done()
    issue_id = _issue_id(entry, CONF_SOC_SENSOR)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

    # The user fixes the upstream integration — SoC comes back. Simulate a
    # registry update event for the affected entity_id.
    hass.states.async_set(SOC_ENTITY, "55")
    hass.bus.async_fire(
        "entity_registry_updated",
        {"action": "create", "entity_id": SOC_ENTITY},
    )
    await hass.async_block_till_done()

    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_issue_filed_when_entity_disappears_at_runtime(
    hass: HomeAssistant,
) -> None:
    """If the user removes a source entity post-setup, an issue appears."""
    await _prime_all(hass)
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    await hass.async_block_till_done()

    issue_id = _issue_id(entry, CONF_SOC_SENSOR)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None

    # SoC goes away (e.g. upstream integration removed by user).
    hass.states.async_remove(SOC_ENTITY)
    hass.bus.async_fire(
        "entity_registry_updated",
        {"action": "remove", "entity_id": SOC_ENTITY},
    )
    await hass.async_block_till_done()

    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None


async def test_registry_event_for_unrelated_entity_does_not_touch_issues(
    hass: HomeAssistant,
) -> None:
    """A registry event for some other entity must not trigger a re-check."""
    await _prime_all(hass)
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    await hass.async_block_till_done()

    # Pre-create a spurious issue we can verify doesn't get cleared by the
    # unrelated event (i.e. the listener short-circuits early).
    issue_id = _issue_id(entry, CONF_SOC_SENSOR)
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="missing_source_entity",
        translation_placeholders={
            "entity_id": SOC_ENTITY,
            "conf_key": CONF_SOC_SENSOR,
            "title": entry.title,
        },
    )

    hass.bus.async_fire(
        "entity_registry_updated",
        {"action": "create", "entity_id": "sensor.unrelated"},
    )
    await hass.async_block_till_done()

    # Spurious issue is still there — the unrelated event was ignored.
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None


# --------------------------------------------------------------------------- #
# Unload cleanup                                                              #
# --------------------------------------------------------------------------- #


async def test_unload_clears_pending_issues(hass: HomeAssistant) -> None:
    """When the config entry is unloaded, any of its issues are cleared."""
    # Setup with SoC missing → issue is filed.
    hass.states.async_set(RANGE_ENTITY, "300", {"unit_of_measurement": "km"})
    hass.states.async_set(MILEAGE_ENTITY, "12000", {"unit_of_measurement": "km"})
    hass.states.async_set(CHARGING_ENTITY, "off")
    hass.states.async_set(ACTUAL_CAPACITY_ENTITY, "70.0")

    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    await hass.async_block_till_done()
    issue_id = _issue_id(entry, CONF_SOC_SENSOR)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

    assert await hass.config_entries.async_unload(entry.entry_id) is True
    await hass.async_block_till_done()

    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
