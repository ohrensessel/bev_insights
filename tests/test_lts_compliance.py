"""Machine-checked LTS / device-class compliance for every entity.

The snapshot test in `test_snapshots.py` pins the *current* schema —
any change shows up as a diff. This file complements that by asserting
the schema is *valid* against HA's live constraint tables:

  homeassistant.components.sensor.const.DEVICE_CLASS_STATE_CLASSES
  homeassistant.components.sensor.const.DEVICE_CLASS_UNITS

so when HA tightens those tables (as it did for ENERGY in 2024 and is
likely to continue), CI fails immediately instead of leaving warnings
to surface in user logs.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.components.sensor.const import (
    DEVICE_CLASS_STATE_CLASSES,
    DEVICE_CLASS_UNITS,
)
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bev_insights.const import CONFIG_ENTRY_VERSION, DOMAIN

from .common import (
    ACTUAL_CAPACITY_ENTITY,
    CHARGING_ENTITY,
    MILEAGE_ENTITY,
    RANGE_ENTITY,
    SOC_ENTITY,
    base_entry_data,
)


async def _setup_all_sensors(hass: HomeAssistant) -> MockConfigEntry:
    """Boot a fully-wired entry so every conditional sensor is created."""
    hass.states.async_set(SOC_ENTITY, "50")
    hass.states.async_set(RANGE_ENTITY, "200", {"unit_of_measurement": "km"})
    hass.states.async_set(MILEAGE_ENTITY, "10000", {"unit_of_measurement": "km"})
    hass.states.async_set(CHARGING_ENTITY, "off")
    hass.states.async_set(ACTUAL_CAPACITY_ENTITY, "70.0")
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=CONFIG_ENTRY_VERSION,
        data=base_entry_data(),
        title="LTS compliance",
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _collect_entities(
    hass: HomeAssistant, entry: MockConfigEntry
) -> list[dict[str, Any]]:
    """Return one row per entity owned by `entry`."""
    registry = hass.data["entity_registry"]
    rows: list[dict[str, Any]] = []
    for ent in registry.entities.values():
        if ent.config_entry_id != entry.entry_id:
            continue
        state = hass.states.get(ent.entity_id)
        attrs = state.attributes if state else {}
        rows.append(
            {
                "entity_id": ent.entity_id,
                "unique_id": ent.unique_id,
                "device_class": ent.device_class or attrs.get("device_class"),
                "state_class": attrs.get("state_class"),
                "unit": attrs.get("unit_of_measurement"),
            }
        )
    return rows


async def test_state_class_allowed_for_device_class(hass: HomeAssistant) -> None:
    """For every device-classed entity, state_class must be in the allow-set.

    HA only enforces this via a warning (it was supposed to start
    raising in Core 2023.6 but didn't), so without this test a
    regression would silently degrade users' LTS series.
    """
    entry = await _setup_all_sensors(hass)
    violations: list[str] = []
    for row in _collect_entities(hass, entry):
        device_class = row["device_class"]
        state_class = row["state_class"]
        if device_class is None:
            continue
        # Coerce string device_class to the enum so the dict lookup works.
        try:
            dc_enum = SensorDeviceClass(device_class)
        except ValueError:
            violations.append(f"{row['entity_id']}: unknown device_class {device_class!r}")
            continue
        allowed = DEVICE_CLASS_STATE_CLASSES.get(dc_enum)
        if allowed is None:
            # Device class has no state-class constraint.
            continue
        if state_class is None and allowed:
            # No state class set at all is fine — HA only complains when
            # one is set AND it's outside the allowed set.
            continue
        if state_class is None:
            continue
        try:
            sc_enum = SensorStateClass(state_class)
        except ValueError:
            violations.append(
                f"{row['entity_id']}: unknown state_class {state_class!r}"
            )
            continue
        if sc_enum not in allowed:
            violations.append(
                f"{row['entity_id']} ({dc_enum.value}): "
                f"state_class {sc_enum.value!r} not in "
                f"{sorted(v.value for v in allowed)}"
            )
    assert not violations, "Invalid device/state-class combinations:\n  " + "\n  ".join(
        violations
    )


async def test_unit_allowed_for_device_class(hass: HomeAssistant) -> None:
    """For every device-classed entity, the unit must be in the allow-set.

    HA validates this at runtime; getting it wrong drops the sensor
    from Energy/LTS aggregations. Some device classes accept any unit
    (their entry is `None` in DEVICE_CLASS_UNITS, or they're absent).
    """
    entry = await _setup_all_sensors(hass)
    violations: list[str] = []
    for row in _collect_entities(hass, entry):
        device_class = row["device_class"]
        unit = row["unit"]
        if device_class is None:
            continue
        try:
            dc_enum = SensorDeviceClass(device_class)
        except ValueError:
            continue
        allowed = DEVICE_CLASS_UNITS.get(dc_enum)
        if allowed is None:
            # No unit constraint for this device class.
            continue
        # HA stores units in DEVICE_CLASS_UNITS as either strings or
        # StrEnum members; both compare equal to the unit string an
        # entity reports, so a plain `in` check suffices.
        if unit not in allowed:
            violations.append(
                f"{row['entity_id']} ({dc_enum.value}): unit {unit!r} not allowed"
            )
    assert not violations, "Invalid device-class/unit combinations:\n  " + "\n  ".join(
        violations
    )


async def test_total_sensors_eligible_for_last_reset(hass: HomeAssistant) -> None:
    """Every TOTAL sensor must publish `last_reset` in its attributes.

    TOTAL without last_reset means HA's recorder can't detect cycle
    boundaries — the sum statistic accumulates forever and is
    effectively useless. (TOTAL_INCREASING is fine without it because
    HA detects resets from value drops instead.)
    """
    entry = await _setup_all_sensors(hass)
    violations: list[str] = []
    for row in _collect_entities(hass, entry):
        if row["state_class"] != SensorStateClass.TOTAL:
            continue
        state = hass.states.get(row["entity_id"])
        # Some TOTAL sensors only set last_reset when they have a real
        # value to report (e.g. LastChargeAddedSensor needs a session).
        # If the sensor is unavailable, last_reset may legitimately be
        # missing; only enforce when the sensor is showing a value.
        if state is None or state.state in ("unavailable", "unknown"):
            continue
        if state.attributes.get("last_reset") is None:
            violations.append(
                f"{row['entity_id']}: state_class=TOTAL but last_reset is unset"
            )
    assert not violations, "TOTAL sensors missing last_reset:\n  " + "\n  ".join(
        violations
    )
