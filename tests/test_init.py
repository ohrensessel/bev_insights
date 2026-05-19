"""Tests for `async_setup_entry`, `async_unload_entry`, `async_migrate_entry`."""
from __future__ import annotations

import json
from pathlib import Path

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bev_insights.const import (
    BASELINE_MILEAGE_KM,
    BASELINE_SOC_PERCENT,
    BASELINE_TIMESTAMP,
    CONF_CAPACITY_ACTUAL_ENTITY,
    CONFIG_ENTRY_VERSION,
    DOMAIN,
    LEGACY_DOMAIN,
)

from .common import (
    ACTUAL_CAPACITY_ENTITY,
    CHARGING_ENTITY,
    MILEAGE_ENTITY,
    RANGE_ENTITY,
    SOC_ENTITY,
    base_entry_data,
    make_entry,
)


async def _prime_states(hass: HomeAssistant) -> None:
    """Source states the integration reads on setup."""
    hass.states.async_set(SOC_ENTITY, "75")
    hass.states.async_set(RANGE_ENTITY, "300", {"unit_of_measurement": "km"})
    hass.states.async_set(MILEAGE_ENTITY, "12000", {"unit_of_measurement": "km"})
    hass.states.async_set(CHARGING_ENTITY, "off")
    hass.states.async_set(ACTUAL_CAPACITY_ENTITY, "70.0")


async def test_setup_entry_loaded(hass: HomeAssistant) -> None:
    await _prime_states(hass)
    entry = make_entry()
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id) is True
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.entry_id in hass.data[DOMAIN]


async def test_unload_entry_clears_domain_data(hass: HomeAssistant) -> None:
    await _prime_states(hass)
    entry = make_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert await hass.config_entries.async_unload(entry.entry_id) is True
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert entry.entry_id not in hass.data[DOMAIN]


async def test_setup_without_optional_charging_mileage(hass: HomeAssistant) -> None:
    """Without charging+mileage the tracker isn't built; setup still succeeds."""
    hass.states.async_set(SOC_ENTITY, "75")
    hass.states.async_set(RANGE_ENTITY, "300")
    hass.states.async_set(ACTUAL_CAPACITY_ENTITY, "70")

    data = base_entry_data()
    data.pop("charging_sensor")
    data.pop("mileage_sensor")
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=CONFIG_ENTRY_VERSION,
        data=data,
        title=data["name"],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    await hass.async_block_till_done()
    domain_data = hass.data[DOMAIN][entry.entry_id]
    assert domain_data["tracker"] is None
    assert domain_data["mileage_history"] is None


def _seed_legacy_storage(
    hass: HomeAssistant,
    hass_storage: dict,
    key: str,
    payload: dict,
) -> Path:
    """Set up a legacy storage entry that the migration code can discover.

    Two parts are required, because the migration discovers candidates by
    globbing `.storage/` and then reads their content via the HA `Store`
    API. Under `pytest_homeassistant_custom_component`'s mocked storage,
    Store reads come from `hass_storage` rather than disk — so we have to
    populate both: a placeholder file (for the glob) and the mock dict
    (for the load).
    """
    storage_dir = Path(hass.config.path(".storage"))
    storage_dir.mkdir(parents=True, exist_ok=True)
    path = storage_dir / key
    # Content of the placeholder file is irrelevant in mocked-storage
    # tests; in production this is what Store actually reads.
    path.write_text(
        json.dumps(
            {"version": 1, "minor_version": 1, "key": key, "data": payload}
        )
    )
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": payload}
    return path


async def test_legacy_storage_is_migrated_to_new_domain_prefix(
    hass: HomeAssistant,
    hass_storage: dict,
) -> None:
    """Legacy myskoda_insights.* storage is rewritten under bev_insights.* and
    the persisted charge baseline is read by the new tracker after setup."""
    await _prime_states(hass)
    legacy_key = f"{LEGACY_DOMAIN}.charge_tracker.some_old_entry_id"
    legacy_path = _seed_legacy_storage(
        hass,
        hass_storage,
        legacy_key,
        {
            BASELINE_MILEAGE_KM: 50000.0,
            BASELINE_SOC_PERCENT: 80.0,
            BASELINE_TIMESTAMP: "2026-05-01T12:00:00+00:00",
        },
    )
    _seed_legacy_storage(
        hass,
        hass_storage,
        f"{LEGACY_DOMAIN}.mileage_history.some_old_entry_id",
        {"samples": []},
    )

    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    await hass.async_block_till_done()

    # Legacy entries are cleaned up via Store.async_remove (which in real HA
    # deletes the disk file; under mocked storage it clears the mock dict).
    # The disk-file glob is silenced by also removing the stub via the mock.
    _ = legacy_path  # disk stub kept around because async_remove is mocked
    assert legacy_key not in hass_storage
    new_charge_tracker_key = f"{DOMAIN}.charge_tracker.{entry.entry_id}"
    assert new_charge_tracker_key in hass_storage
    assert (
        f"{DOMAIN}.mileage_history.{entry.entry_id}" in hass_storage
    )

    # And the tracker loaded the baseline that was in the legacy entry.
    tracker = hass.data[DOMAIN][entry.entry_id]["tracker"]
    assert tracker.baseline == {
        BASELINE_MILEAGE_KM: 50000.0,
        BASELINE_SOC_PERCENT: 80.0,
        BASELINE_TIMESTAMP: "2026-05-01T12:00:00+00:00",
    }


async def test_legacy_storage_migration_is_noop_when_new_data_exists(
    hass: HomeAssistant,
    hass_storage: dict,
) -> None:
    """If the new-domain key is already present, the legacy entry must stay
    untouched — the migration is idempotent and does not clobber fresh data."""
    await _prime_states(hass)

    entry = make_entry()
    legacy_key = f"{LEGACY_DOMAIN}.charge_tracker.some_old_entry_id"
    legacy_path = _seed_legacy_storage(
        hass,
        hass_storage,
        legacy_key,
        {BASELINE_MILEAGE_KM: 99999.0, BASELINE_SOC_PERCENT: 99.0},
    )
    new_key = f"{DOMAIN}.charge_tracker.{entry.entry_id}"
    _seed_legacy_storage(
        hass,
        hass_storage,
        new_key,
        {
            BASELINE_MILEAGE_KM: 12345.0,
            BASELINE_SOC_PERCENT: 50.0,
            BASELINE_TIMESTAMP: "2026-05-10T08:00:00+00:00",
        },
    )

    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    await hass.async_block_till_done()

    assert legacy_path.exists()  # left alone
    assert legacy_key in hass_storage  # not removed from mock either
    # Loaded value comes from the new key, not the legacy one.
    tracker = hass.data[DOMAIN][entry.entry_id]["tracker"]
    assert tracker.baseline[BASELINE_MILEAGE_KM] == 12345.0


async def test_setup_without_legacy_storage_works(
    hass: HomeAssistant,
) -> None:
    """Clean install (no legacy files) is the common path — must not error."""
    await _prime_states(hass)
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    await hass.async_block_till_done()
    assert hass.data[DOMAIN][entry.entry_id]["tracker"].baseline is None


async def test_migrate_v1_to_v2_flags_for_reconfigure(hass: HomeAssistant) -> None:
    """A v1 entry has its old `capacity_actual_kwh` stripped and is left unset.

    Setup intentionally fails (`async_migrate_entry` returns False) so the user
    is prompted to reconfigure with a new entity reference.
    """
    v1_data = {
        "name": "Old Entry",
        "soc_sensor": SOC_ENTITY,
        "range_sensor": RANGE_ENTITY,
        "capacity_factory_kwh": 77.0,
        # legacy v1 field — must be stripped during migration.
        "capacity_actual_kwh": 68.5,
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        data=v1_data,
        title="Old Entry",
    )
    entry.add_to_hass(hass)

    # Migration returns False, so setup should not succeed.
    assert await hass.config_entries.async_setup(entry.entry_id) is False
    await hass.async_block_till_done()

    # Version is bumped, old key is stripped, new key is not yet set.
    assert entry.version == CONFIG_ENTRY_VERSION
    assert "capacity_actual_kwh" not in entry.data
    assert CONF_CAPACITY_ACTUAL_ENTITY not in entry.data

    # A repair issue is filed with the old kWh value so the user can find
    # it again when creating the helper.
    issue_id = f"v1_capacity_migration_{entry.entry_id}"
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.translation_placeholders == {
        "old_kwh": "68.50",
        "title": "Old Entry",
    }


async def test_repair_issue_clears_on_successful_setup(
    hass: HomeAssistant,
) -> None:
    """Once the user reconfigures and setup succeeds, the v1 repair issue
    should be cleared automatically."""
    await _prime_states(hass)

    # Pre-seed an issue as though a previous migration attempt filed one.
    entry = make_entry()
    entry.add_to_hass(hass)
    issue_id = f"v1_capacity_migration_{entry.entry_id}"
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key="v1_capacity_migration",
        translation_placeholders={"old_kwh": "68.50", "title": entry.title},
    )
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

    assert await hass.config_entries.async_setup(entry.entry_id) is True
    await hass.async_block_till_done()

    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
