<p align="center">
  <img src="custom_components/bev_insights/brand/logo.png" alt="BEV Insights" width="420">
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

A Home Assistant custom integration that derives **up to 47 additional sensors** for a
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
| Session log (diagnostic) | Newest-first list of the last 20 completed sessions (start/end SoC + timestamps) as attributes; state is the count. |

The measured-range and measured-efficiency sensors are suppressed during charging and for
the first 20 km / 2 % SoC after a charge end — below those thresholds the calculation is
dominated by SoC quantization noise.

### Window sensors

Historical aggregates over five periods: **rolling 7 days** (trailing 168 h), **this
calendar week** (local Monday 00:00), **this month** (local 1st 00:00), **this year**
(local Jan 1 00:00), and a **vs. last week** delta chip. The 7-day and calendar-week
variants come from an in-memory deque of source-entity samples; the monthly / yearly
variants query HA's long-term `statistics` table directly. Most need the mileage sensor;
a few work with SoC alone (noted below).

| Entity | Unit | Needs mileage? |
|---|---|---|
| Distance driven (rolling 7 days) | km | yes |
| Distance driven (this week) | km | yes |
| Distance driven (this month) | km | yes |
| Distance driven (this year) | km | yes |
| Distance driven (vs. last week) | km | yes |
| Energy consumed (rolling 7 days, factory + actual capacity) | kWh | no |
| Energy consumed (this week, factory + actual capacity) | kWh | no |
| Energy consumed (vs. last week, factory + actual capacity) | kWh | no |
| Average efficiency (rolling 7 days × {factory, actual} × {kWh/100 km, km/kWh}) | kWh/100 km or km/kWh | yes |
| Average efficiency (this week × {factory, actual} × {kWh/100 km, km/kWh}) | kWh/100 km or km/kWh | yes |
| Standstill consumption (rolling 7 days, factory + actual capacity) | kWh | yes |
| Standstill consumption (this week, factory + actual capacity) | kWh | yes |
| Standstill ratio (rolling 7 days, this week) | % | yes |
| Charge count (rolling 7 days, this week) | charges | no |
| Days to low SoC | d | no |
| Idle time | h | yes |

Energy-consumed sensors sum only **downward** SoC steps in the window, so charging events
inside the window don't inflate the figure — the number reflects driving consumption only.

Standstill-consumption sensors split that driving figure further: for each SoC drop in the
window they check whether the odometer advanced. Intervals where the car didn't move are
counted as parked vampire drain; intervals with movement are attributed to driving and
excluded. The sum of driving consumption and standstill consumption equals total energy
consumed in the window. The standstill **ratio** sensor expresses the vampire-drain share
as a percentage of total consumption — useful for spotting unusually high parasitic drain.

The charge-count sensors record how many distinct charging sessions completed in the
window. A "session" is a contiguous run of upward SoC steps totalling ≥ 5 %, which filters
quantization noise.

Days-to-low-SoC projects how many days remain until SoC reaches a configurable threshold
(default 20 %), based on the rolling-7-day average consumption rate.

The **idle-time** sensor reports hours since the odometer last changed. The mileage
history dedupes consecutive identical samples, so the latest sample's timestamp is
always "when the odometer last moved" even if the upstream entity keeps firing state-
change events while parked. Useful next to the standstill-consumption sensors when you
want to ask "how long has the car been sat, and how much SoC has it lost during that
time?".

On a fresh install the window sensors fall back to the oldest available sample as the
window anchor and expose `partial_window_data: true` in their attributes until enough
history has accumulated. Two backfills run on first setup to shorten that wait:

- **History backfill (v1.4.0+):** the integration walks HA's recorder for the prior
  `history_days` (default 15) of SoC and mileage state changes and primes the deques,
  so the window sensors typically light up immediately rather than after a week of
  live recording.
- **Tracker-baseline backfill (v1.6.0+):** the integration also walks the
  charging-state entity for the most recent off → on → off cycle and adopts it as the
  `ChargeTracker` baseline (and `last_session`), so measured range / measured
  efficiency / last-charge-added / average charging power populate on day one instead
  of waiting for the next live charge end.

Both backfills are best-effort and silently no-op if the recorder isn't available or
the queried entity has no history.

The **monthly and yearly distance sensors** take a different route: they query HA's
long-term `statistics` table (which is retained indefinitely, independently of the
recorder purge window) for the odometer reading at the start of the current month / year.
Both are `state_class=TOTAL` with `last_reset` aligned to the period start, so HA's LTS
produces one clean sum per period. They require the upstream mileage entity to publish a
`state_class` so HA records statistics for it; without that they stay unavailable.

The **vs. last week** chips (distance and energy) compare *this week so far* against
*last week up to the same elapsed time* — Wednesday 14:30 this week is compared to
Wednesday 14:30 last week. Positive values mean "more than last week at the same point",
negative means "less". The default `history_days` is 15 so last week's start sample is
always inside the deque; users who set `history_days` below 15 see `partial_window_data:
true` and may get inaccurate comparisons later in the week.

## When sensors become available

A condensed map of what each sensor cluster needs before it stops reporting
`unavailable`. If something on your dashboard is blank, this is usually why.

| Trigger | Sensors |
|---|---|
| **Immediately** (SoC + range sources exist; capacity helper for some) | Full battery range, Efficiency (×4), State of Health, Session log (shows 0 until first cycle), Charge count (shows 0 until first charge in window) |
| **First charge end detected** (trailing edge of any charging session) | Last charged, Time since last charge |
| **First full charge cycle completes** (off → on → off) | Last charge added (×2), Average charging power (×2) |
| **First charge end + enough post-charge driving** (≥ 20 km / 2 % SoC, tuneable) | Measured full range, Measured efficiency (×4) |
| **SoC / mileage history accumulates** (or is backfilled from HA's recorder on first install — v1.4.0+) | Distance driven, Energy consumed, Average efficiency, Standstill consumption + ratio, Days to low SoC, Idle time |

Once a sensor has populated, going back to `unavailable` usually means a source entity
went away (renamed, integration unloaded, restored without it). The **Repairs** panel
surfaces fix-it cards for two classes of problem:

- **Missing source entity** (v1.5.0+) — a configured SoC, range, charging, mileage or
  capacity entity is no longer registered with HA.
- **Value-level sanity issues** (v1.6.0+) — actual-capacity helper outside the
  plausible 5–200 kWh range, SoC source outside 0–100 %, mileage going backwards by
  more than 1 km, or a range / mileage entity reporting an unrecognised distance unit.

All issues clear automatically as soon as the underlying condition resolves.

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

The **rolling-7-day** variants of `energy_consumed_*`, `standstill_consumption_*`, and
`charge_count_*` carry `state_class=MEASUREMENT` (no device class on the energy ones —
a sliding window isn't an accumulator and HA's Energy Dashboard would mis-attribute the
totals). HA still records min/max/mean Long-Term Statistics for them, so you can trend
the rolling figures in the Statistics card, but they don't feed the Energy Dashboard.

### Tuning the integration

Open the integration card and click **Configure** to access the options form:

| Option | Default | Notes |
|---|---|---|
| Minimum distance after charging | 20 km | Measured range / efficiency sensors stay unavailable until at least this much distance has been driven since the last charge end. |
| Minimum SoC consumed after charging | 2 % | And until at least this much SoC has been consumed. Both floors apply — they trade off against each other. |
| History retention window | 15 days | How many days of SoC and odometer samples to retain. The rolling-7-day sensors need at least 7 days; the **vs. last week** chips need at least 14. Going below 7 leaves the rolling-7-day sensors permanently in `partial_window_data: true` mode; going below 15 has the same effect on the vs-last-week sensors later in the week. |

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
last session, rolling SoC and mileage histories) is migrated automatically on first setup
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
  charging sessions and persists them via `homeassistant.helpers.storage.Store`. On
  first install it walks the recorder for the most recent off → on → off cycle and
  adopts that as the baseline (and `last_session`) so the tracker-linked sensors don't
  have to wait for the next live charge.
- `MileageHistory` and `SocHistory` maintain rolling deques of samples
  (`history_days` default = 15), persisted via `Store` with debounced writes (10 s
  window) to keep disk churn down. On first install they're primed from HA's recorder
  so the window sensors typically light up immediately.
- The `CapacitySource` abstraction (`FixedCapacity` / `EntityCapacity`) allows sensors
  to call `.current()` per recalculation. `EntityCapacity` sensors additionally subscribe
  to their source entity's state changes so they recompute the instant the helper moves.
- Monthly / yearly distance sensors are statistics-backed: they query HA's long-term
  `statistics` table for the odometer reading at the start of the current period rather
  than retaining months of samples in memory.

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
