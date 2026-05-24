"""Tests for the missing-source-entity repair-issue flow."""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from custom_components.bev_insights.const import (
    CONF_CAPACITY_ACTUAL_ENTITY,
    CONF_MILEAGE_SENSOR,
    CONF_RANGE_SENSOR,
    CONF_SOC_SENSOR,
    DOMAIN,
)
from custom_components.bev_insights.repairs import (
    _LAST_SEEN_MILEAGE_KM,
    _issue_id,
    _value_issue_id,
)

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


# --------------------------------------------------------------------------- #
# Value-level checks: capacity helper                                         #
# --------------------------------------------------------------------------- #


async def test_capacity_value_issue_filed_when_too_low(hass: HomeAssistant) -> None:
    """A capacity helper below 5 kWh trips `value_capacity_out_of_range`."""
    await _prime_all(hass)
    hass.states.async_set(ACTUAL_CAPACITY_ENTITY, "1.2")  # well below 5
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    await hass.async_block_till_done()

    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, _value_issue_id(entry, "capacity_out_of_range")
    )
    assert issue is not None
    assert issue.translation_placeholders is not None
    assert issue.translation_placeholders["value"] == "1.2"


async def test_capacity_value_issue_filed_when_too_high(hass: HomeAssistant) -> None:
    """A capacity helper above 200 kWh trips the same issue (typo case)."""
    await _prime_all(hass)
    hass.states.async_set(ACTUAL_CAPACITY_ENTITY, "770")  # missed the decimal
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    await hass.async_block_till_done()

    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, _value_issue_id(entry, "capacity_out_of_range")
    )
    assert issue is not None


async def test_capacity_value_issue_clears_when_value_returns_to_range(
    hass: HomeAssistant,
) -> None:
    """Update the helper to a sane value → issue clears on the state-change tick."""
    await _prime_all(hass)
    hass.states.async_set(ACTUAL_CAPACITY_ENTITY, "0.5")
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    await hass.async_block_till_done()

    issue_id = _value_issue_id(entry, "capacity_out_of_range")
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

    hass.states.async_set(ACTUAL_CAPACITY_ENTITY, "75.0")
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_capacity_value_issue_absent_for_sane_value(hass: HomeAssistant) -> None:
    """A 70 kWh value is well inside the plausible range → no issue."""
    await _prime_all(hass)  # ACTUAL_CAPACITY_ENTITY=70.0
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    await hass.async_block_till_done()

    assert (
        ir.async_get(hass).async_get_issue(
            DOMAIN, _value_issue_id(entry, "capacity_out_of_range")
        )
        is None
    )


# --------------------------------------------------------------------------- #
# Value-level checks: SoC out of [0, 100]                                     #
# --------------------------------------------------------------------------- #


async def test_soc_value_issue_filed_when_negative(hass: HomeAssistant) -> None:
    await _prime_all(hass)
    hass.states.async_set(SOC_ENTITY, "-3")
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    await hass.async_block_till_done()

    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, _value_issue_id(entry, "soc_out_of_range")
    )
    assert issue is not None


async def test_soc_value_issue_filed_when_above_100(hass: HomeAssistant) -> None:
    await _prime_all(hass)
    hass.states.async_set(SOC_ENTITY, "115")
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    await hass.async_block_till_done()

    assert (
        ir.async_get(hass).async_get_issue(
            DOMAIN, _value_issue_id(entry, "soc_out_of_range")
        )
        is not None
    )


async def test_soc_value_issue_clears_when_in_range(hass: HomeAssistant) -> None:
    await _prime_all(hass)
    hass.states.async_set(SOC_ENTITY, "120")
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    await hass.async_block_till_done()

    issue_id = _value_issue_id(entry, "soc_out_of_range")
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

    hass.states.async_set(SOC_ENTITY, "75")
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_soc_unavailable_does_not_file_value_issue(hass: HomeAssistant) -> None:
    """An unavailable SoC entity is the missing-entity case, not a value bug."""
    await _prime_all(hass)
    hass.states.async_set(SOC_ENTITY, "unavailable")
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    await hass.async_block_till_done()

    assert (
        ir.async_get(hass).async_get_issue(
            DOMAIN, _value_issue_id(entry, "soc_out_of_range")
        )
        is None
    )


# --------------------------------------------------------------------------- #
# Value-level checks: mileage going backwards                                 #
# --------------------------------------------------------------------------- #


async def test_mileage_reversal_files_issue(hass: HomeAssistant) -> None:
    """Mileage drops by > 1 km between samples → reversal issue files."""
    await _prime_all(hass)
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    await hass.async_block_till_done()

    issue_id = _value_issue_id(entry, "mileage_went_backwards")
    # Initial state was 12000; no issue yet (first observation).
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None

    # Drop by 50 km — clearly a reversal.
    hass.states.async_set(
        MILEAGE_ENTITY, "11950", {"unit_of_measurement": "km"}
    )
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None


async def test_mileage_small_jitter_does_not_file(hass: HomeAssistant) -> None:
    """A ~0.5 km dip is within the noise tolerance and stays quiet."""
    await _prime_all(hass)
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    await hass.async_block_till_done()

    issue_id = _value_issue_id(entry, "mileage_went_backwards")
    hass.states.async_set(
        MILEAGE_ENTITY, "11999.5", {"unit_of_measurement": "km"}
    )
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_mileage_reversal_clears_when_value_recovers(
    hass: HomeAssistant,
) -> None:
    """Once mileage climbs back past the previous peak the issue clears."""
    await _prime_all(hass)
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    await hass.async_block_till_done()

    issue_id = _value_issue_id(entry, "mileage_went_backwards")
    hass.states.async_set(MILEAGE_ENTITY, "11500", {"unit_of_measurement": "km"})
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

    # Recovery past the original peak (12000).
    hass.states.async_set(MILEAGE_ENTITY, "12001", {"unit_of_measurement": "km"})
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_mileage_unload_clears_module_state(hass: HomeAssistant) -> None:
    """Unloading an entry drops its cached last-seen mileage."""
    await _prime_all(hass)
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    await hass.async_block_till_done()
    assert entry.entry_id in _LAST_SEEN_MILEAGE_KM

    assert await hass.config_entries.async_unload(entry.entry_id) is True
    await hass.async_block_till_done()
    assert entry.entry_id not in _LAST_SEEN_MILEAGE_KM


# --------------------------------------------------------------------------- #
# Value-level checks: unknown distance unit                                   #
# --------------------------------------------------------------------------- #


async def test_unknown_distance_unit_on_mileage_files_issue(
    hass: HomeAssistant,
) -> None:
    """A mileage entity reporting `furlongs` triggers the unit issue."""
    await _prime_all(hass)
    hass.states.async_set(
        MILEAGE_ENTITY, "12000", {"unit_of_measurement": "furlongs"}
    )
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    await hass.async_block_till_done()

    issue = ir.async_get(hass).async_get_issue(
        DOMAIN,
        _value_issue_id(entry, "unknown_distance_unit", suffix=CONF_MILEAGE_SENSOR),
    )
    assert issue is not None
    assert issue.translation_placeholders is not None
    assert issue.translation_placeholders["unit"] == "furlongs"


async def test_unknown_distance_unit_on_range_files_issue(
    hass: HomeAssistant,
) -> None:
    await _prime_all(hass)
    hass.states.async_set(RANGE_ENTITY, "300", {"unit_of_measurement": "??"})
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    await hass.async_block_till_done()

    assert (
        ir.async_get(hass).async_get_issue(
            DOMAIN,
            _value_issue_id(
                entry, "unknown_distance_unit", suffix=CONF_RANGE_SENSOR
            ),
        )
        is not None
    )


async def test_known_distance_units_do_not_file_issue(hass: HomeAssistant) -> None:
    """km, mi, m are all recognised — no issue for either entity."""
    hass.states.async_set(SOC_ENTITY, "75")
    hass.states.async_set(RANGE_ENTITY, "300", {"unit_of_measurement": "mi"})
    hass.states.async_set(MILEAGE_ENTITY, "12000", {"unit_of_measurement": "km"})
    hass.states.async_set(CHARGING_ENTITY, "off")
    hass.states.async_set(ACTUAL_CAPACITY_ENTITY, "70.0")
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    await hass.async_block_till_done()

    for conf_key in (CONF_MILEAGE_SENSOR, CONF_RANGE_SENSOR):
        assert (
            ir.async_get(hass).async_get_issue(
                DOMAIN,
                _value_issue_id(entry, "unknown_distance_unit", suffix=conf_key),
            )
            is None
        )


async def test_unknown_distance_unit_clears_when_unit_recognized(
    hass: HomeAssistant,
) -> None:
    """User fixes the upstream unit → issue clears on the next state tick."""
    await _prime_all(hass)
    hass.states.async_set(
        RANGE_ENTITY, "300", {"unit_of_measurement": "leagues"}
    )
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    await hass.async_block_till_done()

    issue_id = _value_issue_id(
        entry, "unknown_distance_unit", suffix=CONF_RANGE_SENSOR
    )
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

    hass.states.async_set(RANGE_ENTITY, "300", {"unit_of_measurement": "km"})
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
