"""Constants for the MySkoda Insights integration."""
from __future__ import annotations

DOMAIN = "myskoda_insights"

# Config entry keys
CONF_NAME = "name"
CONF_SOC_SENSOR = "soc_sensor"
CONF_RANGE_SENSOR = "range_sensor"
CONF_CAPACITY_FACTORY = "capacity_factory_kwh"
# Stored as a plain float (kWh) in the config entry.
CONF_CAPACITY_ACTUAL = "capacity_actual_kwh"
CONF_CHARGING_SENSOR = "charging_sensor"
CONF_MILEAGE_SENSOR = "mileage_sensor"

DEFAULT_NAME = "MySkoda Insights"
# 77 kWh is the gross capacity of a typical Skoda Enyaq 85; users override
# this for their own vehicle.
DEFAULT_CAPACITY_KWH = 77.0

# Custom units for efficiency. There's no standard HA unit constant for
# energy-per-distance / distance-per-energy at this scale.
UNIT_KWH_PER_100KM = "kWh/100 km"
UNIT_KM_PER_KWH = "km/kWh"

# Identifiers used inside unique_ids and translation keys for capacity variants
VARIANT_FACTORY = "factory"
VARIANT_ACTUAL = "actual"

# Identifiers used inside unique_ids and translation keys for unit variants
UNIT_VARIANT_KWH_PER_100KM = "kwh_per_100km"
UNIT_VARIANT_KM_PER_KWH = "km_per_kwh"

# Storage / dispatcher
STORAGE_VERSION = 1
STORAGE_KEY_PREFIX = f"{DOMAIN}.charge_tracker"
MILEAGE_HISTORY_KEY_PREFIX = f"{DOMAIN}.mileage_history"
SOC_HISTORY_KEY_PREFIX = f"{DOMAIN}.soc_history"

# How many days of samples to retain (a bit more than a week so the
# rolling 7-day window always has data on both sides of the cutoff).
MILEAGE_HISTORY_DAYS = 8
SOC_HISTORY_DAYS = 8

# Dispatcher signal sent when a charge-end event updates the baseline.
def signal_baseline_updated(entry_id: str) -> str:
    return f"{DOMAIN}_baseline_updated_{entry_id}"


def signal_mileage_history_updated(entry_id: str) -> str:
    return f"{DOMAIN}_mileage_history_updated_{entry_id}"


def signal_soc_history_updated(entry_id: str) -> str:
    return f"{DOMAIN}_soc_history_updated_{entry_id}"


# Baseline dict keys (what we persist via Store)
BASELINE_MILEAGE_KM = "mileage_km"
BASELINE_SOC_PERCENT = "soc_percent"
BASELINE_TIMESTAMP = "timestamp"
