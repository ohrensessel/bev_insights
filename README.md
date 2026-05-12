# MySkoda Insights

A small Home Assistant custom integration that consumes sensors from
[`homeassistant-myskoda`](https://github.com/skodaconnect/homeassistant-myskoda)
and exposes additional derived sensors.

## Sensors

| Entity | Unit |
|---|---|
| Full battery range | km |

**Full battery range** — `current_range / current_soc × 100`, the car's range
prediction extrapolated to 100% SoC.

## Installation

1. Copy `custom_components/myskoda_insights` into your Home Assistant
   `config/custom_components/` directory (or install via HACS as a custom
   repository).
2. Restart Home Assistant.
3. **Settings → Devices & Services → + Add Integration → MySkoda Insights**.
4. Fill in:
   - **Battery percentage (SoC) sensor** — e.g. `sensor.<vehicle>_battery_percentage`
   - **Remaining electric range sensor** — e.g. `sensor.<vehicle>_range`

## Adding more derived sensors

Open `sensor.py` and:

1. Subclass `MySkodaDerivedSensor`.
2. Implement `_recalculate()` to set `self._attr_native_value` (and
   `self._attr_available`).
3. Append an instance to the `entities` list in `async_setup_entry`.
