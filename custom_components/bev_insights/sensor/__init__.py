"""Sensor platform entry point for BEV Insights.

Each sensor reads values from existing source entities (SoC, range,
optionally charging-state and mileage) and recomputes itself whenever
those sources change state. Capacity-dependent sensors are instantiated
once per configured battery capacity (factory-new vs. actual remaining).

This package was split out of a single monolithic `sensor.py`; the
sub-modules group sensors by what they depend on:
    - base.py            : shared base class + tracker mixin
    - formulas.py        : pure helper functions
    - instantaneous.py   : sensors needing only live source entities
    - tracker_linked.py  : sensors driven by ChargeTracker baselines
    - distance.py        : odometer-history-driven distance & projection sensors
    - window.py          : rolling-7-day / calendar-week window sensors
"""
from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from custom_components.bev_insights.capacity import CapacitySource
from custom_components.bev_insights.const import (
    CONF_CHARGING_SENSOR,
    CONF_MILEAGE_SENSOR,
    CONF_RANGE_SENSOR,
    CONF_SOC_SENSOR,
    DOMAIN,
    UNIT_VARIANT_KM_PER_KWH,
    UNIT_VARIANT_KWH_PER_100KM,
    VARIANT_ACTUAL,
    VARIANT_FACTORY,
)
from custom_components.bev_insights.tracker import (
    ChargeTracker,
    MileageHistory,
    SocHistory,
)

from .deltas import DistanceWeekDeltaSensor, EnergyConsumedWeekDeltaSensor
from .distance import (
    DaysToLowSocSensor,
    DistanceRolling7DaysSensor,
    DistanceThisWeekSensor,
    IdleTimeSensor,
)

# Re-exported so existing imports (tests, future external use) keep
# resolving against `custom_components.bev_insights.sensor.…`.
from .formulas import (
    _efficiency_value,
    _human_unit,
    _local_week_start,
    _post_charge_window,
    _unit_variant_props,
    _window_cutoff,
)
from .instantaneous import EfficiencySensor, FullBatteryRangeSensor, StateOfHealthSensor
from .long_term import DistanceThisMonthSensor, DistanceThisYearSensor
from .tracker_linked import (
    AverageChargingPowerSensor,
    LastChargeAddedSensor,
    LastChargedSensor,
    MeasuredEfficiencySensor,
    MeasuredFullRangeSensor,
    SessionLogSensor,
    TimeSinceLastChargeSensor,
)
from .window import (
    AverageEfficiencyWindowSensor,
    ChargeCountWindowSensor,
    EnergyConsumedWindowSensor,
    StandstillConsumptionWindowSensor,
    StandstillRatioWindowSensor,
)

# Two windowed shapes: rolling 7 days, calendar week. Used for both
# kWh consumed and weekly average efficiency. `window_key` differentiates
# the cutoff calculation in the sensor.
_WINDOWS: tuple[tuple[str, str], ...] = (
    ("rolling_7_days", "Rolling 7 days"),
    ("this_week", "This week"),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up BEV Insights sensors from a config entry."""
    domain_data = hass.data[DOMAIN][entry.entry_id]
    data = domain_data["data"]
    tracker: ChargeTracker | None = domain_data.get("tracker")
    mileage_history: MileageHistory | None = domain_data.get("mileage_history")
    soc_history: SocHistory | None = domain_data.get("soc_history")
    capacity_factory: CapacitySource = domain_data["capacity_factory"]
    capacity_actual: CapacitySource = domain_data["capacity_actual"]

    soc_entity: str = data[CONF_SOC_SENSOR]
    range_entity: str = data[CONF_RANGE_SENSOR]

    entities: list[SensorEntity] = [
        FullBatteryRangeSensor(entry, soc_entity, range_entity),
        StateOfHealthSensor(entry, capacity_factory, capacity_actual),
    ]

    # Efficiency: 2 capacities × 2 units = 4 sensors
    for capacity, capacity_variant in (
        (capacity_factory, VARIANT_FACTORY),
        (capacity_actual, VARIANT_ACTUAL),
    ):
        for unit_variant in (
            UNIT_VARIANT_KWH_PER_100KM,
            UNIT_VARIANT_KM_PER_KWH,
        ):
            entities.append(
                EfficiencySensor(
                    entry,
                    soc_entity,
                    range_entity,
                    capacity=capacity,
                    capacity_variant=capacity_variant,
                    unit_variant=unit_variant,
                )
            )

    # Tracker-dependent sensors only if the user wired up the prerequisites.
    if tracker is not None:
        mileage_entity: str = data[CONF_MILEAGE_SENSOR]
        charging_entity: str = data[CONF_CHARGING_SENSOR]
        entities.append(
            MeasuredFullRangeSensor(
                entry, tracker, soc_entity, mileage_entity, charging_entity
            )
        )
        entities.append(LastChargedSensor(entry, tracker))
        entities.append(TimeSinceLastChargeSensor(entry, tracker))
        entities.append(SessionLogSensor(entry, tracker))

        # Last charge added (kWh) and average charging power (kW): one of
        # each per capacity variant. Both read from the same persisted
        # session record and listen to the same baseline-updated signal.
        for capacity, capacity_variant in (
            (capacity_factory, VARIANT_FACTORY),
            (capacity_actual, VARIANT_ACTUAL),
        ):
            entities.append(
                LastChargeAddedSensor(
                    entry,
                    tracker,
                    capacity=capacity,
                    capacity_variant=capacity_variant,
                )
            )
            entities.append(
                AverageChargingPowerSensor(
                    entry,
                    tracker,
                    capacity=capacity,
                    capacity_variant=capacity_variant,
                )
            )

        # Measured efficiency: 2 capacities × 2 units = 4 sensors
        for capacity, capacity_variant in (
            (capacity_factory, VARIANT_FACTORY),
            (capacity_actual, VARIANT_ACTUAL),
        ):
            for unit_variant in (
                UNIT_VARIANT_KWH_PER_100KM,
                UNIT_VARIANT_KM_PER_KWH,
            ):
                entities.append(
                    MeasuredEfficiencySensor(
                        entry,
                        tracker,
                        soc_entity,
                        mileage_entity,
                        charging_entity,
                        capacity=capacity,
                        capacity_variant=capacity_variant,
                        unit_variant=unit_variant,
                    )
                )

    # Mileage-history sensors only require the odometer; they work even
    # if no charging sensor was configured.
    if mileage_history is not None:
        entities.append(
            DistanceRolling7DaysSensor(entry, mileage_history)
        )
        entities.append(
            DistanceThisWeekSensor(
                entry, mileage_history, data[CONF_MILEAGE_SENSOR]
            )
        )
        # Monthly / yearly totals via the recorder statistics table —
        # no in-memory history needed beyond what the odometer entity
        # already publishes.
        entities.append(
            DistanceThisMonthSensor(entry, data[CONF_MILEAGE_SENSOR])
        )
        entities.append(
            DistanceThisYearSensor(entry, data[CONF_MILEAGE_SENSOR])
        )
        # Week-over-week distance delta — uses the same odometer entity.
        entities.append(
            DistanceWeekDeltaSensor(
                entry, mileage_history, data[CONF_MILEAGE_SENSOR]
            )
        )
        # Idle time — needs only the mileage history.
        entities.append(IdleTimeSensor(entry, mileage_history))

    # Days-to-low-SoC estimate — needs SoC history only.
    if soc_history is not None:
        entities.append(DaysToLowSocSensor(entry, soc_history, soc_entity))

    # Charge count per window — needs SoC history only.
    if soc_history is not None:
        for window_key, window_label in _WINDOWS:
            entities.append(
                ChargeCountWindowSensor(
                    entry,
                    soc_history,
                    window_key=window_key,
                    window_label=window_label,
                )
            )

    # kWh consumed per window — needs SoC history only.
    if soc_history is not None:
        for window_key, window_label in _WINDOWS:
            for capacity, capacity_variant in (
                (capacity_factory, VARIANT_FACTORY),
                (capacity_actual, VARIANT_ACTUAL),
            ):
                entities.append(
                    EnergyConsumedWindowSensor(
                        entry,
                        soc_history,
                        capacity=capacity,
                        capacity_variant=capacity_variant,
                        window_key=window_key,
                        window_label=window_label,
                    )
                )
        # Week-over-week energy delta — one per capacity variant.
        for capacity, capacity_variant in (
            (capacity_factory, VARIANT_FACTORY),
            (capacity_actual, VARIANT_ACTUAL),
        ):
            entities.append(
                EnergyConsumedWeekDeltaSensor(
                    entry,
                    soc_history,
                    capacity=capacity,
                    capacity_variant=capacity_variant,
                )
            )

    # Standstill (vampire drain) window sensors — needs both histories.
    if soc_history is not None and mileage_history is not None:
        for window_key, window_label in _WINDOWS:
            for capacity, capacity_variant in (
                (capacity_factory, VARIANT_FACTORY),
                (capacity_actual, VARIANT_ACTUAL),
            ):
                entities.append(
                    StandstillConsumptionWindowSensor(
                        entry,
                        soc_history,
                        mileage_history,
                        capacity=capacity,
                        capacity_variant=capacity_variant,
                        window_key=window_key,
                        window_label=window_label,
                    )
                )

    # Standstill ratio sensors — needs both histories.
    if soc_history is not None and mileage_history is not None:
        for window_key, window_label in _WINDOWS:
            entities.append(
                StandstillRatioWindowSensor(
                    entry,
                    soc_history,
                    mileage_history,
                    window_key=window_key,
                    window_label=window_label,
                )
            )

    # Weekly average efficiency — needs both histories together.
    if soc_history is not None and mileage_history is not None:
        for window_key, window_label in _WINDOWS:
            for capacity, capacity_variant in (
                (capacity_factory, VARIANT_FACTORY),
                (capacity_actual, VARIANT_ACTUAL),
            ):
                for unit_variant in (
                    UNIT_VARIANT_KWH_PER_100KM,
                    UNIT_VARIANT_KM_PER_KWH,
                ):
                    entities.append(
                        AverageEfficiencyWindowSensor(
                            entry,
                            soc_history,
                            mileage_history,
                            capacity=capacity,
                            capacity_variant=capacity_variant,
                            unit_variant=unit_variant,
                            window_key=window_key,
                            window_label=window_label,
                        )
                    )

    async_add_entities(entities)


__all__ = [
    "AverageChargingPowerSensor",
    "AverageEfficiencyWindowSensor",
    "ChargeCountWindowSensor",
    "DaysToLowSocSensor",
    "DistanceRolling7DaysSensor",
    "DistanceThisMonthSensor",
    "DistanceThisWeekSensor",
    "DistanceThisYearSensor",
    "DistanceWeekDeltaSensor",
    "EfficiencySensor",
    "EnergyConsumedWeekDeltaSensor",
    "EnergyConsumedWindowSensor",
    "FullBatteryRangeSensor",
    "IdleTimeSensor",
    "LastChargeAddedSensor",
    "LastChargedSensor",
    "MeasuredEfficiencySensor",
    "MeasuredFullRangeSensor",
    "SessionLogSensor",
    "StandstillConsumptionWindowSensor",
    "StandstillRatioWindowSensor",
    "StateOfHealthSensor",
    "TimeSinceLastChargeSensor",
    "_efficiency_value",
    "_human_unit",
    "_local_week_start",
    "_post_charge_window",
    "_unit_variant_props",
    "_window_cutoff",
    "async_setup_entry",
]
