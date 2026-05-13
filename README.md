# MySkoda Insights

A Home Assistant custom integration that consumes sensors from
[`homeassistant-myskoda`](https://github.com/skodaconnect/homeassistant-myskoda)
and exposes up to **25 additional derived sensors** per vehicle.

## Features

### Instantaneous sensors (always present)

| Entity | Formula | Unit |
|---|---|---|
| Full battery range | `range / soc × 100` | km |
| Efficiency (factory capacity, kWh/100 km) | `factory_kwh × soc / range_km` | kWh/100 km |
| Efficiency (factory capacity, km/kWh) | `range_km / (factory_kwh × soc)` | km/kWh |
| Efficiency (actual capacity, kWh/100 km) | `actual_kwh × soc / range_km` | kWh/100 km |
| Efficiency (actual capacity, km/kWh) | `range_km / (actual_kwh × soc)` | km/kWh |

### Charge-tracker sensors (requires charging + mileage sensors)

| Entity | Notes |
|---|---|
| Measured full range | `distance_since_charge / soc_consumed × 100` — real-world range based on the last charge cycle |
| Measured efficiency (factory, kWh/100 km) | Measured variant against nameplate capacity |
| Measured efficiency (factory, km/kWh) | — |
| Measured efficiency (actual, kWh/100 km) | Measured variant against live actual capacity |
| Measured efficiency (actual, km/kWh) | — |
| Last charged | Timestamp of the last detected charge-end; mileage + SoC at that moment as attributes |

### Window sensors (requires mileage sensor)

Two windows: **rolling 7 days** (trailing 168 h) and **this calendar week** (local Monday 00:00).

| Entity | Unit |
|---|---|
| Distance driven (rolling 7 days) | km |
| Distance driven (this week) | km |
| Energy consumed (rolling 7 days, factory capacity) | kWh |
| Energy consumed (rolling 7 days, actual capacity) | kWh |
| Energy consumed (this week, factory capacity) | kWh |
| Energy consumed (this week, actual capacity) | kWh |
| Average efficiency (rolling 7 days, factory capacity, kWh/100 km) | kWh/100 km |
| Average efficiency (rolling 7 days, factory capacity, km/kWh) | km/kWh |
| Average efficiency (rolling 7 days, actual capacity, kWh/100 km) | kWh/100 km |
| Average efficiency (rolling 7 days, actual capacity, km/kWh) | km/kWh |
| Average efficiency (this week, factory capacity, kWh/100 km) | kWh/100 km |
| Average efficiency (this week, factory capacity, km/kWh) | km/kWh |
| Average efficiency (this week, actual capacity, kWh/100 km) | kWh/100 km |
| Average efficiency (this week, actual capacity, km/kWh) | km/kWh |

Energy-consumed sensors sum only **downward** SoC steps in the window, so charging events
inside the window don't inflate the figure — the number reflects driving consumption only.

## Installation

1. Copy `custom_components/myskoda_insights` into your Home Assistant
   `config/custom_components/` directory (or install via HACS as a custom repository).
2. Restart Home Assistant.
3. **Settings → Devices & Services → + Add Integration → MySkoda Insights**.
4. Fill in the required fields (see Configuration below).

## Configuration

| Field | Required | Notes |
|---|---|---|
| Name | Yes | Display name for the integration entry |
| Battery percentage (SoC) sensor | Yes | e.g. `sensor.<vehicle>_battery_percentage` |
| Remaining electric range sensor | Yes | e.g. `sensor.<vehicle>_range` |
| Factory-new battery capacity (kWh) | Yes | Nameplate capacity; 77 kWh for an Enyaq 85 |
| Live actual battery capacity entity | Yes | An `input_number` helper or sensor in kWh. Change its value to update all actual-capacity sensors live without reloading the integration. |
| Charging-state sensor (optional) | No | Enables charge-tracker sensors. May be a `sensor` or `binary_sensor`. |
| Mileage / odometer sensor (optional) | No | Enables charge-tracker and window sensors. |

### Setting up the actual capacity entity

Create a **Settings → Devices & Services → Helpers → Number** helper:

- **Name:** e.g. `Enyaq actual battery capacity`
- **Minimum / Maximum:** 1 – 100 kWh
- **Step:** 0.1
- **Unit:** kWh

Set the helper's value to your battery's current real-world capacity (measured via a
calibration charge or a SoH tool). The integration reads this live, so you can update it
from a dashboard slider or an automation without reconfiguring the integration.

## Migration from v0.6

The **actual remaining capacity** field changed from a fixed number to a live entity
reference in v0.7. When upgrading:

1. Home Assistant will log a warning with the old kWh value and flag the entry as needing
   reconfiguration.
2. Create an `input_number` helper as described above and set its value to the logged
   number.
3. Open the integration card → **Reconfigure** and select the new helper.

## Architecture notes

- All derived sensors share the `MySkodaDerivedSensor` base class and recompute on
  `async_track_state_change_event` for their source entities.
- The `ChargeTracker` captures `(odometer, SoC, timestamp)` on the trailing edge of
  charging sessions and persists them via `homeassistant.helpers.storage.Store`.
- `MileageHistory` and `SocHistory` maintain rolling 8-day deques of samples, also
  persisted via `Store`, used by all window sensors.
- The `CapacitySource` abstraction (`FixedCapacity` / `EntityCapacity`) allows sensors
  to call `.current()` per recalculation. `EntityCapacity` sensors additionally subscribe
  to their source entity's state changes so they recompute the instant the helper moves.
