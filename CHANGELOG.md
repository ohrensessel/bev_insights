# Changelog

All notable changes to MySkoda Insights are documented here. Format
loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
versioning follows [SemVer](https://semver.org/spec/v2.0.0.html).

## [0.7.0]

### Changed
- **Breaking:** the actual remaining capacity is now an entity reference
  instead of a fixed number. Users create an `input_number` helper (or
  any sensor that produces a kWh value) and select it via the config
  flow. Changing the helper's value live-updates every actual-capacity
  sensor without an integration reload.
- Config entry schema bumped to v2. `async_migrate_entry` handles v1 →
  v2: logs the old kWh value as a warning, strips the old key, and
  flags the entry for user reconfiguration in the UI.

### Added
- New `capacity.py` module with `CapacitySource` abstraction
  (`FixedCapacity`, `EntityCapacity`). Sensors call `.current()` per
  recalculation and go unavailable if the source returns `None`, rather
  than carrying stale values.
- Sensors now subscribe to the capacity entity's state changes (when
  reactive) so they recompute the instant the helper's value moves.
- `capacity_source` attribute on every capacity-dependent sensor for
  diagnostics.

## [0.6.0]

### Added
- **8 average-efficiency-over-window sensors:** 2 windows × 2 capacities
  × 2 units (kWh/100 km, km/kWh). Computed from `SocHistory` and
  `MileageHistory` over either the rolling 7-day window or the local
  calendar week.
- **4 energy-consumed-per-window sensors:** 2 windows × 2 capacities, in
  kWh. Sums only downward SoC steps in the window so charging events
  inside the window are correctly ignored (the figure reflects driving
  consumption, not net SoC change).
- New `SocHistory` class with `consumed_since(cutoff)` that walks
  samples chronologically and sums the magnitude of each downward step.

### Changed
- `MileageHistory` refactored into a generic `EntityHistory` base class.
  `MileageHistory` and `SocHistory` are thin subclasses (~30 lines each)
  that supply a value reader and a dispatcher signal name. Each history
  is persisted in its own `Store` file.
- Total sensor count now up to **25 per config entry**.

## [0.5.0]

### Added
- **Distance driven (rolling 7 days)** — trailing 168-hour window.
  Updates on odometer changes and ticks once an hour so the window rolls
  even when the car is parked.
- **Distance driven (this week)** — resets to zero at local Monday 00:00
  in the HA-configured timezone via `async_track_time_change`. Exposes
  `partial_week_data: true` in attributes when there isn't yet a sample
  from before the most recent Monday.
- New `MileageHistory` class: rolling 8-day window of `(timestamp,
  mileage_km)` samples, persisted via `Store`, with deduplication of
  identical readings and automatic pruning of stale entries.

## [0.4.0]

### Added
- **Dual unit variants** for every efficiency sensor: each value now
  exists in both **kWh/100 km** and **km/kWh** form. Doubled the
  existing 2 efficiency + 2 measured-efficiency sensors to 8 total.
- New `UNIT_KM_PER_KWH` constant, new `_unit_variant_props()` and
  `_human_unit()` helpers, new `unit_variant` field on unique IDs and
  translation keys.

### Changed
- Extracted the single source of truth for the efficiency math into a
  private `_efficiency_value(capacity, soc_percent, distance_km,
  unit_variant)` helper used by both the instantaneous and measured
  variants. Verified consistency: `kWh/100 km × km/kWh ≈ 100` across all
  eight efficiency sensors.

## [0.3.0]

### Added
- **Charge tracking.** New `ChargeTracker` class watches a configured
  charging-state entity and captures `(odometer, SoC, timestamp)` on the
  trailing edge of a charging session. Detection uses `event.old_state`
  / `event.new_state` so it works even if HA restarts mid-charge.
  Baseline persists across HA restarts via
  `homeassistant.helpers.storage.Store`. Notifies sensors of updates via
  `async_dispatcher_send`.
- **Measured full range** sensor — `distance_since_charge / soc_consumed
  × 100`. Stays unavailable until there's actual driving data to compute
  from (no extrapolation from zero).
- **Last charged** diagnostic sensor — timestamp of the last detected
  charge end, with mileage/SoC at end as attributes. Marked
  `EntityCategory.DIAGNOSTIC` to keep it out of the default view.
- Optional **charging-state sensor** and **mileage/odometer sensor**
  config-flow inputs. The charge-tracker-dependent sensors are only
  created when both fields are filled in.
- New `_TrackerLinkedMixin` so future sensors that need the charge-end
  baseline are one line to wire up.

### Changed
- Pulled state-reading helpers (`read_float`, `read_distance_km`,
  `is_charging`) into a shared `util.py`.

## [0.2.0]

### Added
- **Two efficiency sensors:** one against the factory-new capacity, one
  against the user-supplied actual remaining capacity. Computed as
  `capacity × soc / range_km` (kWh/100 km).
- Two new config-flow fields: **factory-new battery capacity (kWh)** and
  **current remaining battery capacity (kWh)**. Both editable via
  Reconfigure on the integration card.

### Fixed
- **Unit-aware distance reading.** If a user is on imperial mode HA
  hands us miles in `state.state`; now normalised to km internally so
  the kWh/100 km figure stays correct.

### Changed
- All sensors now group under a single virtual device via `DeviceInfo`,
  so they're tidy in the UI instead of scattered.

## [0.1.0]

### Added
- Initial release.
- **Full battery range** sensor — `current_range / current_soc × 100`,
  the car's range prediction extrapolated to 100% SoC.
- Config flow with `EntitySelector` fields for the SoC and range
  sensors. Entry naming follows `sensor.<vehicle_model>_<entity_key>`
  from the upstream `homeassistant-myskoda` integration.
- Event-driven recalculation via `async_track_state_change_event`.
- Base class `MySkodaDerivedSensor` to make adding further derived
  sensors straightforward.
