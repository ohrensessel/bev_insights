# Changelog

All notable changes to BEV Insights (formerly MySkoda Insights) are
documented here. Format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
versioning follows [SemVer](https://semver.org/spec/v2.0.0.html).

## [1.3.0]

### Added
- **Days-to-low-SoC estimate sensor** (`days_to_low_soc`): projects how many
  days remain until the battery reaches a configurable low-SoC threshold
  (default 20 %, tuneable via options). Formula: `(current_soc − threshold) /
  daily_avg_consumption` where the daily average is the rolling 7-day figure.
  Requires only the SoC sensor (always available). New option:
  `low_soc_threshold_percent`.
- **Charge count window sensors** (2 sensors: rolling 7 days, this week):
  counts distinct charging sessions observed in the SoC history. A session is
  a contiguous upward SoC run totalling ≥ 5 %, filtering quantization noise.
  The this-week variant carries `SensorStateClass.TOTAL` for clean LTS curves.
- **Session log sensor** (`session_log`, diagnostic): surfaces the last 20
  completed charging sessions from the `ChargeTracker`. State = session count,
  attributes include a newest-first list with start/end SoC and timestamps.
  Only created when both charging-state and mileage sensors are configured.
  The log is persisted across HA restarts.
- **Standstill-ratio window sensors** (2 sensors: rolling 7 days, this week):
  reports the fraction of total battery consumption attributable to standstill
  (vampire) drain: `standstill_consumed / total_consumed × 100 %`. Requires
  both SoC and mileage histories.

### Changed
- Total sensor count per fully-wired config entry: **35 → 41**.

## [1.2.0]

### Added
- **Configurable standstill movement threshold** (`standstill_movement_threshold_km`,
  default 0.1 km). Exposed in the options flow alongside the existing
  measured-range floors. Integrations that report mileage in whole kilometres
  should raise this to 1.0 km to avoid misclassifying slow-moving intervals
  as standstill drain.
- **State-attributes labels** for all 35 sensor entities in both `en.json`
  and `de.json`. Opaque dict keys (`baseline_mileage_km`,
  `soc_consumed_standstill_percent`, `partial_window_data`, etc.) now show
  human-readable labels in the HA entity detail panel.
- **`tests/test_translations.py`**: four pytest checks that verify de.json
  mirrors en.json's entity-key set, state-attributes keys, required top-level
  sections, and options-flow data keys. Runs as part of the normal suite.
- **`hassfest` CI job** via `hacs/action@v2` in both GitHub and Gitea
  workflow files. Validates manifest fields, translation schema, and
  integration structure on every push.

## [1.1.0]

### Added
- **Standstill consumption sensors (vampire drain):** 4 new sensors
  (2 windows × 2 capacity variants) that report kWh consumed while the
  car was parked. For each downward SoC step in the window, the sensor
  cross-references the odometer: steps where mileage didn't advance are
  attributed to standstill drain; steps with movement are excluded as
  driving consumption. Requires both the mileage and SoC sensors to be
  configured. The `this_week` variant is `state_class=TOTAL`; the
  rolling-7-day variant omits a state class (sliding windows can't be
  accumulators).
- **German translations (`de.json`):** full translation of all config-flow
  labels, options, issues, and entity names.
- **Energy Dashboard step-by-step guide** in README.

### Changed
- Total sensor count per fully-wired config entry: **31 → 35**.

## [1.0.0]

### Changed
- **Breaking: renamed domain from `myskoda_insights` to `bev_insights`**.
  The integration was always driven entirely by generic source entities
  (SoC, range, charging-state, odometer), so the new name better reflects
  what it actually does. The myskoda integration remains the only
  upstream confirmed to work; reports from users of other BEV
  integrations are welcome.
- Base class renamed `MySkodaDerivedSensor` → `BevDerivedSensor`.
- Display strings, manufacturer label, and translation keys updated
  accordingly. The `LEGACY_DOMAIN` constant retains the old name so the
  storage migration below can find legacy files.

### Added
- **One-time legacy-storage migration.** On first setup of `bev_insights`,
  any `.storage/myskoda_insights.*` files found in HA's storage directory
  are read through the `Store` API and re-written under the new domain
  prefix keyed to the new config entry's `entry_id`. The legacy entries
  are then removed. Persisted state surviving the rename: charge baseline,
  last completed session, and 8 days of SoC + mileage history. Tested
  end-to-end against `pytest_homeassistant_custom_component`'s mocked
  storage.
- HACS metadata (`hacs.json`) and `manifest.json` URLs point at the new
  repository.

### Migration steps for existing users

1. Update the integration files.
2. Restart HA. The old `MySkoda Insights` entry will fail to load
   (its domain no longer exists in code) — leave it alone for now.
3. Add the new `BEV Insights` integration with the same source entities;
   the storage migration runs automatically and a warning is logged.
4. Delete the orphaned old entry and any stale entities in the entity
   registry.

## [0.9.0]

### Added
- **State of Health sensor:** `actual / factory × 100` (%). Single
  value per entry — no unit/capacity doubling. Recomputes whenever
  the actual-capacity helper changes.
- **Time since last charge sensor:** hours elapsed since the most
  recent charge end (`DURATION` device class, unit `h`). Ticks once an
  hour so dashboards and automations can read it directly without a
  template sensor — useful for rules like "notify if not charged in 5
  days".

### Changed
- Total sensor count per fully-wired config entry: **27 → 29**.

## [0.8.0]

### Added
- **Last charge added sensors:** two new entities (factory and actual
  capacity), each reporting the kWh delivered during the most recently
  completed charging session. Computed as
  `capacity * (end_soc - start_soc) / 100`. Negative deltas clamp to 0.
- `ChargeTracker` now also captures SoC + timestamp on the **rising**
  edge of a charging session. Combined with the existing falling-edge
  capture, a full off→on→off cycle is persisted as `last_session`
  alongside the existing baseline.

### Changed
- On-disk format for the tracker store gains an optional `last_session`
  top-level key. v0.7 payloads load unchanged (no migration needed); the
  new key is written from the next completed cycle onward.
- Total sensor count per fully-wired config entry: **25 → 27**.

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
