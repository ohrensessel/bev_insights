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

DEFAULT_NAME = "MySkoda Insights"
# 77 kWh is the gross capacity of a typical Skoda Enyaq 85; users override
# this for their own vehicle.
DEFAULT_CAPACITY_KWH = 77.0

# Custom units for efficiency.
UNIT_KWH_PER_100KM = "kWh/100 km"

# Identifiers used inside unique_ids and translation keys for capacity variants
VARIANT_FACTORY = "factory"
VARIANT_ACTUAL = "actual"
