"""Constants for the MySkoda Insights integration."""
from __future__ import annotations

DOMAIN = "myskoda_insights"

# Config entry keys
CONF_NAME = "name"
CONF_SOC_SENSOR = "soc_sensor"
CONF_RANGE_SENSOR = "range_sensor"
CONF_CHARGING_SENSOR = "charging_sensor"
CONF_MILEAGE_SENSOR = "mileage_sensor"
CONF_CAPACITY_FACTORY = "capacity_factory_kwh"
# v1: a number (kWh).  v2+: an entity_id whose state is read as the live
# actual remaining capacity in kWh.
CONF_CAPACITY_ACTUAL_ENTITY = "capacity_actual_entity"

# Schema version of the config entry payload. Bumped when the shape of
# `entry.data` changes incompatibly so `async_migrate_entry` can repair
# entries created by older versions.
CONFIG_ENTRY_VERSION = 2

DEFAULT_NAME = "MySkoda Insights"
# 77 kWh is the gross capacity of a typical Skoda Enyaq 85; users override
# this for their own vehicle.
DEFAULT_CAPACITY_KWH = 77.0

# Lower bounds before the post-charge measured range/efficiency is considered
# trustworthy. SoC typically has 1% resolution, so a few km of driving may
# yield a 0-1% delta whose ratio is dominated by quantization noise. The
# distance floor and the SoC-consumed floor are complementary: a long drive
# with little SoC change (slow downhill) and a short drive with a big SoC
# change (cold start, accessories) are both filtered out.
MIN_MEASURED_RANGE_KM = 20.0
MIN_MEASURED_RANGE_SOC_PERCENT = 2.0

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
# Format the per-entry signal name with `signal_baseline_updated(entry_id)`.
def signal_baseline_updated(entry_id: str) -> str:
    """Return the per-entry dispatcher signal fired on charge-end baseline update."""
    return f"{DOMAIN}_baseline_updated_{entry_id}"


def signal_mileage_history_updated(entry_id: str) -> str:
    """Return the per-entry dispatcher signal fired on a new mileage sample."""
    return f"{DOMAIN}_mileage_history_updated_{entry_id}"


def signal_soc_history_updated(entry_id: str) -> str:
    """Return the per-entry dispatcher signal fired on a new SoC sample."""
    return f"{DOMAIN}_soc_history_updated_{entry_id}"


# Baseline dict keys (what we persist via Store)
BASELINE_MILEAGE_KM = "mileage_km"
BASELINE_SOC_PERCENT = "soc_percent"
BASELINE_TIMESTAMP = "timestamp"

# Last-completed-charge-session keys. Stored alongside the baseline in the
# same Store payload (so adding them doesn't break v0.7 baseline files —
# old files just lack the "last_session" key).
LAST_SESSION_KEY = "last_session"
SESSION_START_SOC_PERCENT = "start_soc_percent"
SESSION_END_SOC_PERCENT = "end_soc_percent"
SESSION_START_TIMESTAMP = "start_timestamp"
SESSION_END_TIMESTAMP = "end_timestamp"
