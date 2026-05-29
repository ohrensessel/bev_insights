"""Constants for the BEV Insights integration."""
from __future__ import annotations

DOMAIN = "bev_insights"

# Domain this integration was published under prior to v1.0.0. Referenced
# only by the one-time storage migration in __init__.py; nothing else
# should depend on it.
LEGACY_DOMAIN = "myskoda_insights"

# Config entry keys
CONF_NAME = "name"
CONF_SOC_SENSOR = "soc_sensor"
CONF_RANGE_SENSOR = "range_sensor"
CONF_CHARGING_SENSOR = "charging_sensor"
CONF_MILEAGE_SENSOR = "mileage_sensor"
# Optional outside-air-temperature sensor. When set, the integration
# correlates driving efficiency against the daily-average temperature to
# surface cold-weather range loss.
CONF_OUTSIDE_TEMP_SENSOR = "outside_temp_sensor"
CONF_CAPACITY_FACTORY = "capacity_factory_kwh"
# v1: a number (kWh).  v2+: an entity_id whose state is read as the live
# actual remaining capacity in kWh.
CONF_CAPACITY_ACTUAL_ENTITY = "capacity_actual_entity"

# Options-flow keys. Stored in `entry.options`, not `entry.data` — they
# don't change what the integration is configured against (those are the
# sensors above), only how strictly / how far back it analyses.
CONF_MIN_MEASURED_RANGE_KM = "min_measured_range_km"
CONF_MIN_MEASURED_RANGE_SOC_PERCENT = "min_measured_range_soc_percent"
CONF_HISTORY_DAYS = "history_days"
CONF_STANDSTILL_MOVEMENT_THRESHOLD_KM = "standstill_movement_threshold_km"
CONF_LOW_SOC_THRESHOLD_PERCENT = "low_soc_threshold_percent"

# Schema version of the config entry payload. Bumped when the shape of
# `entry.data` changes incompatibly so `async_migrate_entry` can repair
# entries created by older versions.
CONFIG_ENTRY_VERSION = 2

DEFAULT_NAME = "BEV Insights"
# 77 kWh is the gross capacity of a typical Skoda Enyaq 85; users override
# this for their own vehicle. The default is a sensible starting point for
# the integration the project was originally built against; it is not
# Skoda-specific.
DEFAULT_CAPACITY_KWH = 77.0

# Lower bounds before the post-charge measured range/efficiency is considered
# trustworthy. SoC typically has 1% resolution, so a few km of driving may
# yield a 0-1% delta whose ratio is dominated by quantization noise. The
# distance floor and the SoC-consumed floor are complementary: a long drive
# with little SoC change (slow downhill) and a short drive with a big SoC
# change (cold start, accessories) are both filtered out.
MIN_MEASURED_RANGE_KM = 20.0
MIN_MEASURED_RANGE_SOC_PERCENT = 2.0
# Odometer movement below this threshold during a SoC interval is treated as
# "parked" for the standstill consumption sensor. 0.1 km handles integrations
# that report mileage in whole kilometres (each sample either stays flat or
# jumps by ≥1 km), while still ignoring true creep-in-traffic.
STANDSTILL_MOVEMENT_THRESHOLD_KM = 0.1
# SoC level treated as "empty" for the days-to-low-SoC estimate. 20% gives
# a comfortable margin above the real floor and matches what most BEV drivers
# use as a practical minimum before seeking a charger.
LOW_SOC_THRESHOLD_PERCENT = 20.0

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
TEMPERATURE_HISTORY_KEY_PREFIX = f"{DOMAIN}.temperature_history"

# How many days of samples to retain. 15 days covers two full calendar
# weeks so the week-over-week delta sensors always have last week's
# start sample regardless of the day. Older releases used 8 (just past
# the rolling-7-day window); existing users who explicitly set
# `history_days` in options keep their value.
MILEAGE_HISTORY_DAYS = 15
SOC_HISTORY_DAYS = 15
TEMPERATURE_HISTORY_DAYS = 15

# Temperature bands (°C) the efficiency-vs-temperature sensor groups daily
# averages into. Each entry is (key, lower_inclusive, upper_exclusive); a
# None bound means open-ended. Chosen for a temperate climate: a sub-zero
# band (heaviest range loss), two mild bands, and a warm band.
TEMPERATURE_BANDS: tuple[tuple[str, float | None, float | None], ...] = (
    ("below_0", None, 0.0),
    ("0_to_10", 0.0, 10.0),
    ("10_to_20", 10.0, 20.0),
    ("above_20", 20.0, None),
)

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


def signal_temperature_history_updated(entry_id: str) -> str:
    """Return the per-entry dispatcher signal fired on a new temperature sample."""
    return f"{DOMAIN}_temperature_history_updated_{entry_id}"


# Baseline dict keys (what we persist via Store)
BASELINE_MILEAGE_KM = "mileage_km"
BASELINE_SOC_PERCENT = "soc_percent"
BASELINE_TIMESTAMP = "timestamp"

# Last-completed-charge-session keys. Stored alongside the baseline in the
# same Store payload (so adding them doesn't break v0.7 baseline files —
# old files just lack the "last_session" key).
LAST_SESSION_KEY = "last_session"
SESSION_LOG_KEY = "session_log"
SESSION_LOG_MAX = 20
SESSION_START_SOC_PERCENT = "start_soc_percent"
SESSION_END_SOC_PERCENT = "end_soc_percent"
SESSION_START_TIMESTAMP = "start_timestamp"
SESSION_END_TIMESTAMP = "end_timestamp"
