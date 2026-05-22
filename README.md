<p align="center">
  <img src="brand/logo.png" alt="BEV Insights" width="420">
</p>

<p align="center">
  <a href="https://github.com/ohrensessel/bev_insights/actions/workflows/tests.yml">
    <img src="https://github.com/ohrensessel/bev_insights/actions/workflows/tests.yml/badge.svg" alt="tests">
  </a>
  <a href="https://codecov.io/gh/ohrensessel/bev_insights">
    <img src="https://codecov.io/gh/ohrensessel/bev_insights/branch/main/graph/badge.svg" alt="coverage">
  </a>
</p>

# BEV Insights

A Home Assistant custom integration that derives **up to 41 additional sensors** for a
battery-electric vehicle from a small set of source entities: battery percentage (SoC),
remaining range, an optional charging-state indicator, and an optional odometer reading.

> **Tested with:** the [`homeassistant-myskoda`](https://github.com/skodaconnect/homeassistant-myskoda)
> integration (Škoda Enyaq). The source entities are integration-agnostic — any
> integration that exposes the four entities listed above should work in principle.
> **Reports welcome** from users on other car integrations: please open an issue
> describing your setup and whether the derived sensors show sensible values.

## Features

### Instantaneous sensors (always present)

| Entity | Formula | Unit |
|---|---|---|
| Full battery range | `range / soc × 100` | km |
| State of Health | `actual_capacity / factory_capacity × 100` | % |
| Efficiency (factory capacity, kWh/100 km) | `factory_kwh × soc / range_km` | kWh/100 km |
| Efficiency (factory capacity, km/kWh) | `range_km / (factory_kwh × soc)` | km/kWh |
| Efficiency (actual capacity, kWh/100 km) | `actual_kwh × soc / range_km` | kWh/100 km |
| Efficiency (actual capacity, km/kWh) | `range_km / (actual_kwh × soc)` | km/kWh |

### Charge-tracker sensors (requires charging + mileage sensors)

| Entity | Notes |
|---|---|
| Measured full range | `distance_since_charge / soc_consumed × 100` — real-world range based on driving since the last charge end |
| Measured efficiency (factory, kWh/100 km) | Measured variant against nameplate capacity |
| Measured efficiency (factory, km/kWh) | — |
| Measured efficiency (actual, kWh/100 km) | Measured variant against live actual capacity |
| Measured efficiency (actual, km/kWh) | — |
| Last charged | Timestamp of the last detected charge-end; mileage + SoC at that moment as attributes |
| Time since last charge | Hours elapsed since the most recent charge end (advances hourly) |
| Last charge added (factory, actual) | kWh added during the most recent completed charging session |
| Average charging power (factory, actual) | kW averaged across the most recent completed session — `kWh_added / duration`. Useful for distinguishing AC vs. DC fast charging and spotting throttled chargers. |

The measured-range and measured-efficiency sensors are suppressed during charging and for
the first 20 km / 2 % SoC after a charge end — below those thresholds the calculation is
dominated by SoC quantization noise.

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
| Standstill consumption (rolling 7 days, factory capacity) | kWh |
| Standstill consumption (rolling 7 days, actual capacity) | kWh |
| Standstill consumption (this week, factory capacity) | kWh |
| Standstill consumption (this week, actual capacity) | kWh |

Energy-consumed sensors sum only **downward** SoC steps in the window, so charging events
inside the window don't inflate the figure — the number reflects driving consumption only.

Standstill-consumption sensors split that driving figure further: for each SoC drop in the
window they check whether the odometer advanced. Intervals where the car didn't move are
counted as parked vampire drain; intervals with movement are attributed to driving and
excluded. The sum of driving consumption and standstill consumption equals total energy
consumed in the window.

On a fresh install the window sensors fall back to the oldest available sample as the
window anchor and expose `partial_window_data: true` in their attributes until enough
history has accumulated.

## Installation

### Via HACS (custom repository)

1. **HACS → Integrations → Custom repositories** → add `https://github.com/ohrensessel/bev_insights`
   as an *Integration*.
2. Install BEV Insights from the HACS UI, then restart Home Assistant.
3. **Settings → Devices & Services → + Add Integration → BEV Insights**.

### Manual

1. Copy `custom_components/bev_insights` into your Home Assistant
   `config/custom_components/` directory.
2. Restart Home Assistant.
3. **Settings → Devices & Services → + Add Integration → BEV Insights**.

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

### Home Assistant Energy Dashboard

Two of the integration's sensors are shaped to drop straight into HA's
**Energy Dashboard** as individual-device consumption sources:

| Use as | Sensor |
|---|---|
| **Energy delivered to the car** per charging session | `sensor.<title>_last_charge_added_actual_capacity` |
| **Energy spent driving** week-by-week | `sensor.<title>_energy_consumed_this_week_actual_capacity` |

**Step-by-step setup:**

1. Go to **Settings → Dashboards → Energy**.
2. Under *Individual devices*, click **Add device**.
3. In the entity picker, search for your entry title (e.g. `Enyaq`).
4. Select `Last charge added (actual capacity)` for per-session charging energy, **or**
   select `Energy consumed (this week, actual capacity)` for weekly driving consumption.
5. Click **Save** and wait for Long-Term Statistics to accumulate the first data point.

Both sensors declare `device_class=ENERGY` + `state_class=TOTAL` with `last_reset`
advanced on each session end (for last-charge-added) or each Monday 00:00
(for this-week consumption), so HA's Long-Term Statistics correctly
accumulates them rather than treating the reset-to-zero as data corruption.

The *actual* capacity variants are usually the better choice for an aging battery; switch
to the *factory* variants if you trust the nameplate number more.

The rolling-7-day variants deliberately omit a state class because a sliding window isn't
an accumulator — they show up in dashboards as regular numeric sensors but won't feed the
Energy Dashboard's running totals. The standstill-consumption sensors follow the same
convention and are not suitable for the Energy Dashboard either.

### Tuning the integration

Open the integration card and click **Configure** to access the options form:

| Option | Default | Notes |
|---|---|---|
| Minimum distance after charging | 20 km | Measured range / efficiency sensors stay unavailable until at least this much distance has been driven since the last charge end. |
| Minimum SoC consumed after charging | 2 % | And until at least this much SoC has been consumed. Both floors apply — they trade off against each other. |
| History retention window | 8 days | How many days of SoC and odometer samples to retain. Going below 7 leaves the rolling-7-day sensors permanently in `partial_window_data: true` mode. |

Defaults are good for typical use; the floors filter out post-charge noise dominated by SoC quantization (~1% on most BEVs).

### Setting up the actual capacity entity

Create a **Settings → Devices & Services → Helpers → Number** helper:

- **Name:** e.g. `Enyaq actual battery capacity`
- **Minimum / Maximum:** 1 – 100 kWh
- **Step:** 0.1
- **Unit:** kWh

Set the helper's value to your battery's current real-world capacity (measured via a
calibration charge or a SoH tool). The integration reads this live, so you can update it
from a dashboard slider or an automation without reconfiguring the integration.

## Migration

### From `myskoda_insights` (v0.x) to `bev_insights` (v1.0)

v1.0 renames the integration to a generic `bev_insights` domain. Existing installs need to
re-create the config entry under the new domain, but the persisted state (charge baseline,
last session, 8 days of SoC and mileage history) is migrated automatically on first setup
of the new domain:

1. Update the integration files (HACS pulls the new version, manual installers copy the
   new `custom_components/bev_insights/` directory and remove the old `myskoda_insights/`
   one).
2. Restart Home Assistant. The old integration entry will fail to load (because its domain
   no longer exists in the code); ignore that for now.
3. **Settings → Devices & Services → + Add Integration → BEV Insights** and configure with
   the same source entities. On first setup the legacy `.storage/myskoda_insights.*` files
   are renamed under `bev_insights.*` and adopted by the new entry — you'll see a warning
   in the log confirming this.
4. Delete the old `MySkoda Insights` integration entry (now showing as "unknown integration")
   and any orphaned entities in the entity registry.

### Capacity v1 → v2 (legacy)

The **actual remaining capacity** field changed from a fixed number to a live entity
reference back in v0.7. When upgrading from a v1 config entry:

1. Home Assistant will log a warning with the old kWh value and flag the entry as needing
   reconfiguration.
2. Create an `input_number` helper as described above and set its value to the logged
   number.
3. Open the integration card → **Reconfigure** and select the new helper.

## Architecture notes

- All derived sensors share the `BevDerivedSensor` base class and recompute on
  `async_track_state_change_event` for their source entities.
- The `ChargeTracker` captures `(odometer, SoC, timestamp)` on the trailing edge of
  charging sessions and persists them via `homeassistant.helpers.storage.Store`.
- `MileageHistory` and `SocHistory` maintain rolling 8-day deques of samples, also
  persisted via `Store`, used by all window sensors.
- The `CapacitySource` abstraction (`FixedCapacity` / `EntityCapacity`) allows sensors
  to call `.current()` per recalculation. `EntityCapacity` sensors additionally subscribe
  to their source entity's state changes so they recompute the instant the helper moves.

## Reporting issues

When filing a bug, attaching the integration's diagnostics dump speeds up
triage considerably. On the BEV Insights integration card, click the
**⋮** menu → **Download diagnostics**. The downloaded JSON contains
the resolved configuration, the charge-tracker baseline + last session,
a summary of the rolling history buffers, and a snapshot of the source
entity states. Identifying fields (entry title, unique id) are redacted
automatically.

## License

[MIT](LICENSE) © Leo Krueger.
