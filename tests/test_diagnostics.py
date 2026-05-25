"""Tests for `bev_insights.diagnostics`."""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bev_insights.const import (
    BASELINE_MILEAGE_KM,
    BASELINE_SOC_PERCENT,
    BASELINE_TIMESTAMP,
    CONFIG_ENTRY_VERSION,
    DOMAIN,
)
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
    make_entry,
)


async def _setup(hass: HomeAssistant):
    hass.states.async_set(SOC_ENTITY, "50")
    hass.states.async_set(RANGE_ENTITY, "200", {"unit_of_measurement": "km"})
    hass.states.async_set(MILEAGE_ENTITY, "10000", {"unit_of_measurement": "km"})
    hass.states.async_set(CHARGING_ENTITY, "off")
    hass.states.async_set(ACTUAL_CAPACITY_ENTITY, "70.0")
    entry = make_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_diagnostics_contains_expected_sections(hass: HomeAssistant) -> None:
    """Top-level structure: entry, sources, capacities, tracker, histories."""
    entry = await _setup(hass)
    data = await async_get_config_entry_diagnostics(hass, entry)
    assert {
        "version",
        "entry",
        "sources",
        "capacities",
        "tracker",
        "histories",
    }.issubset(data)


async def test_diagnostics_redacts_title_and_unique_id(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    data = await async_get_config_entry_diagnostics(hass, entry)
    assert data["entry"]["title"] == "**REDACTED**"
    assert data["entry"]["unique_id"] == "**REDACTED**"
    # entry_id is the random HA-assigned UUID, not user-set — keep it
    # so the same entry can be cross-referenced across multiple dumps.
    assert data["entry"]["entry_id"] == entry.entry_id


async def test_diagnostics_captures_source_entity_states(
    hass: HomeAssistant,
) -> None:
    entry = await _setup(hass)
    data = await async_get_config_entry_diagnostics(hass, entry)
    assert data["sources"]["soc"] == {
        "entity_id": SOC_ENTITY,
        "state": "50",
        "unit_of_measurement": None,
    }
    assert data["sources"]["range"]["state"] == "200"
    assert data["sources"]["range"]["unit_of_measurement"] == "km"
    assert data["sources"]["mileage"]["state"] == "10000"


async def test_diagnostics_includes_resolved_capacities(
    hass: HomeAssistant,
) -> None:
    entry = await _setup(hass)
    data = await async_get_config_entry_diagnostics(hass, entry)
    assert data["capacities"]["factory"]["value_kwh"] == 77.0
    assert data["capacities"]["actual"]["value_kwh"] == 70.0


async def test_diagnostics_reflects_tracker_baseline(
    hass: HomeAssistant,
) -> None:
    """After a charge cycle, the tracker baseline shows up in the dump."""
    entry = await _setup(hass)
    hass.states.async_set(CHARGING_ENTITY, "on")
    await hass.async_block_till_done()
    hass.states.async_set(CHARGING_ENTITY, "off")
    await hass.async_block_till_done()

    data = await async_get_config_entry_diagnostics(hass, entry)
    baseline = data["tracker"]["baseline"]
    assert baseline is not None
    assert baseline[BASELINE_MILEAGE_KM] == 10000.0
    assert baseline[BASELINE_SOC_PERCENT] == 50.0
    assert BASELINE_TIMESTAMP in baseline
    assert data["tracker"]["is_charging"] is False


async def test_diagnostics_history_summary_has_samples(
    hass: HomeAssistant,
) -> None:
    """Histories should report at least the initial-snapshot sample."""
    entry = await _setup(hass)
    data = await async_get_config_entry_diagnostics(hass, entry)
    mileage = data["histories"]["mileage"]
    soc = data["histories"]["soc"]
    assert mileage is not None and mileage["sample_count"] >= 1
    assert soc is not None and soc["sample_count"] >= 1
    assert mileage["latest"]["value"] == 10000.0
    assert soc["latest"]["value"] == 50.0


async def test_diagnostics_runs_when_tracker_absent(hass: HomeAssistant) -> None:
    """Without charging+mileage sensors, the tracker isn't built — the
    diagnostics dump must still succeed with `None` placeholders."""
    hass.states.async_set(SOC_ENTITY, "50")
    hass.states.async_set(RANGE_ENTITY, "200", {"unit_of_measurement": "km"})
    hass.states.async_set(ACTUAL_CAPACITY_ENTITY, "70.0")

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
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["tracker"] == {
        "is_charging": None,
        "baseline": None,
        "last_session": None,
    }
    assert diag["histories"]["mileage"] is None
    assert diag["histories"]["soc"]["sample_count"] >= 1


# --------------------------------------------------------------------------- #
# Sensor attribute redaction: unique_id must never leak via attributes        #
# --------------------------------------------------------------------------- #
#
# `unique_id` is in the diagnostics TO_REDACT set because it concatenates
# source entity_ids that may embed VIN, license plate, or other identifying
# detail. The redaction guards the diagnostics dump — but the same identifier
# could just as well leak via a sensor's `extra_state_attributes`, which is
# fully visible in the HA UI and the REST API. This test enumerates every
# sensor the integration creates and asserts that none of their attributes
# contain the entry's unique_id (or any individual sensor's unique_id) as a
# substring, recursively through nested dicts/lists.


def _contains_string(value: object, needle: str) -> bool:
    """Recursive substring check across nested dicts/lists/tuples."""
    if isinstance(value, str):
        return needle in value
    if isinstance(value, dict):
        return any(
            _contains_string(k, needle) or _contains_string(v, needle)
            for k, v in value.items()
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_contains_string(item, needle) for item in value)
    return False


async def test_sensor_attributes_do_not_leak_unique_id(
    hass: HomeAssistant,
) -> None:
    """No `extra_state_attributes` value should embed the entry or sensor unique_id.

    Drives the integration through a full charge cycle so attribute dicts
    that only populate after a baseline exists (e.g. measured-range
    sensors) are exercised, not just the cold-start no-data path.
    """
    entry = await _setup(hass)
    # Run a complete cycle so tracker-linked sensors have data to attribute.
    hass.states.async_set(CHARGING_ENTITY, "on")
    await hass.async_block_till_done()
    hass.states.async_set(SOC_ENTITY, "80")
    await hass.async_block_till_done()
    hass.states.async_set(CHARGING_ENTITY, "off")
    await hass.async_block_till_done()

    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    sensors = [
        ent for ent in registry.entities.values()
        if ent.config_entry_id == entry.entry_id
    ]
    assert sensors, "Setup didn't register any entities — test misconfigured."

    entry_unique_id = entry.unique_id
    assert entry_unique_id, "Test entry must have a unique_id set."

    needles = {entry_unique_id, *(ent.unique_id for ent in sensors if ent.unique_id)}

    leaks: list[tuple[str, str, object]] = []
    for ent in sensors:
        state = hass.states.get(ent.entity_id)
        if state is None:
            continue
        for needle in needles:
            if _contains_string(dict(state.attributes), needle):
                leaks.append((ent.entity_id, needle, dict(state.attributes)))

    assert not leaks, (
        "Sensor attributes leak unique_id values:\n"
        + "\n".join(
            f"  {entity_id}: contains {needle!r} in {attrs!r}"
            for entity_id, needle, attrs in leaks
        )
    )


async def test_sensor_attributes_do_not_leak_unique_id_when_unique_id_embeds_identifier(
    hass: HomeAssistant,
) -> None:
    """Stronger variant: force an obviously-identifying string into the unique_id.

    The default test unique_id is constructed from entity_ids, which are
    relatively bland strings. Real-world unique_ids can embed VINs. We
    swap in an unmistakable marker so a recursive substring match would
    catch even a sliced-up leak (e.g. a sensor copying the last 8 chars).
    """
    hass.states.async_set(SOC_ENTITY, "50")
    hass.states.async_set(RANGE_ENTITY, "200", {"unit_of_measurement": "km"})
    hass.states.async_set(MILEAGE_ENTITY, "10000", {"unit_of_measurement": "km"})
    hass.states.async_set(CHARGING_ENTITY, "off")
    hass.states.async_set(ACTUAL_CAPACITY_ENTITY, "70.0")

    marker = "VINWVWZZZAUZNW123456"
    # `make_entry`'s default unique_id is built from entity_ids; here we
    # need a recognisable marker, so construct the MockConfigEntry directly.
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=CONFIG_ENTRY_VERSION,
        data=base_entry_data(),
        title="Test Car",
        unique_id=marker,
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    for ent in registry.entities.values():
        if ent.config_entry_id != entry.entry_id:
            continue
        state = hass.states.get(ent.entity_id)
        if state is None:
            continue
        assert not _contains_string(dict(state.attributes), marker), (
            f"{ent.entity_id} leaks unique_id marker via attributes: "
            f"{dict(state.attributes)!r}"
        )
