# Agent guide — BEV Insights

Contributor-facing notes for AI coding agents and humans extending this
integration. For user-facing setup, see `README.md`.

## What this is

A Home Assistant custom integration that consumes a small set of source
entities (SoC, range, optional charging-state, optional odometer) and
exposes derived EV insights (efficiency, measured range, weekly
distance/energy, state of health, vampire-drain ratio, charge count,
days-to-low-SoC projection, etc.) — currently **41 sensors per
fully-wired config entry** (`tests/test_setup_smoke.py::EXPECTED_SUFFIXES`
is the canonical list and the smoke test asserts the count).

Originally developed against the `homeassistant-myskoda` integration on
a Škoda Enyaq 85; renamed to `bev_insights` in v1.0.0 to reflect that
the design is integration-agnostic. The legacy `myskoda_insights` domain
is only referenced by the one-time storage-migration code in
`__init__.py::_migrate_legacy_storage`.

In addition to sensors, the integration ships:
- **Diagnostics dump** (`diagnostics.py`): JSON snapshot of config,
  tracker baseline + last session, history summaries, capacity values,
  source-entity states.
- **Repairs panel issues** (`repairs.py`): one issue per missing source
  entity, with a Configure-pointer; clears automatically when the entity
  is back.
- **Recorder backfill** (`backfill.py`): on first install, primes the
  8-day SoC and mileage history deques from HA's recorder so window
  sensors become useful immediately instead of waiting a week.

## Commands

Everything runs from the repo root with a virtualenv that has
`requirements_test.txt` installed.

```bash
pytest                    # full test suite + coverage report (~15s, 185+ tests)
pytest --no-cov           # skip coverage for fast iteration
ruff check custom_components tests
mypy                      # config-driven, scopes to custom_components/
python -m py_compile custom_components/bev_insights/sensor/*.py
```

Coverage is wired into pytest's default addopts via `pyproject.toml`
(`[tool.pytest.ini_options].addopts`). The terminal report shows
uncovered lines inline so CI logs and local runs both answer "what's
not tested" without an extra artifact. Pass `--no-cov` if you want
zero-overhead test runs while iterating.

CI runs `lint` (ruff), `mypy`, `pytest`, and `hassfest` in parallel.
The pytest matrix is four rows:
- py3.11 + `pytest-homeassistant-custom-component==0.13.100` (HA ~2024.7
  floor — what `hacs.json` declares as the minimum).
- py3.12 + latest PHACC (stable HA).
- py3.13 + latest PHACC (stable HA).
- py3.14 + latest PHACC + HA installed from `core@dev` — marked
  `experimental: true` / `continue-on-error: true` so upstream breakage
  doesn't block PRs; cached as a wheel keyed on the dev branch SHA.

The same workflow exists under both `.github/workflows/tests.yml` (for
GitHub Actions) and `.gitea/workflows/tests.yml` (for the Gitea mirror).
The `hassfest` job runs only on GitHub — `hacs/action` 401s on Gitea.
The Gitea workflow mirrors to GitHub on green-only pushes to main.

## Deploying to Home Assistant

Manual deploy from Gitea Actions via SSH/rsync — workflow at
`.gitea/workflows/deploy-to-ha.yml`, triggered from the Gitea UI
(`Actions → deploy-to-ha → Run workflow`). Does **not** restart HA;
do that yourself after verifying the diff in `/config/`.

Required Gitea Actions secrets (Repo settings → Actions → Secrets):

| Secret | Notes |
|---|---|
| `HA_HOST` | Host or IP of the HA instance. |
| `HA_USER` | SSH user — typically `root` for the HA `SSH & Web Terminal` add-on. |
| `HA_SSH_PORT` | Optional; defaults to `22`. The HA SSH add-on often uses a non-standard port. |
| `HA_SSH_KEY` | Full private key (including `BEGIN`/`END` lines). Authorize the matching public key on the SSH add-on. |

The deploy rsyncs `custom_components/bev_insights/` into
`/homeassistant/custom_components/bev_insights/` — the path the
Advanced SSH & Web Terminal community add-on mounts the HA config dir
at. (The official SSH & Web Terminal add-on uses `/config/` instead;
change the workflow if you switch add-ons.) `--delete` is on, and
`__pycache__`/`*.pyc` are excluded.

## Code map

| File | Responsibility |
|---|---|
| `__init__.py` | `async_setup_entry` wires tracker + histories + capacity sources; `async_migrate_entry` handles v1→v2 config-entry schema; `_migrate_legacy_storage` adopts pre-v1.0 `myskoda_insights.*` storage on first setup; kicks off the recorder backfill. |
| `sensor/` | Sensor package (split from a monolithic `sensor.py` in v1.5.0). See breakdown below. |
| `tracker.py` | `ChargeTracker` (charge-end baseline + last session, plus `is_charging` property), `EntityHistory` base, `MileageHistory`, `SocHistory`. 8-day rolling deques persisted via `Store` with **debounced writes** (10 s window) to keep disk churn down. |
| `capacity.py` | `CapacitySource` ABC → `FixedCapacity` (nameplate kWh) and `EntityCapacity` (live `input_number` / sensor). |
| `util.py` | `read_float`, `read_distance_km` (unit-aware), `is_charging`. |
| `const.py` | All constants; per-entry dispatcher signal name builders; `LEGACY_DOMAIN` and `MIN_MEASURED_RANGE_*` thresholds. |
| `config_flow.py` | v2 schema, `async_step_user` + `async_step_reconfigure`. |
| `backfill.py` | Reads the prior 8 days of source-entity states from HA's recorder on first install and primes the history deques. Wrapped in try/except so missing/older recorder APIs are silently ignored. |
| `diagnostics.py` | `async_get_config_entry_diagnostics` — returns redacted JSON snapshot for the Diagnostics download. |
| `repairs.py` | Files / clears Repairs-panel issues for missing source entities; checked on setup and periodically. |

### `sensor/` package layout

| File | Responsibility |
|---|---|
| `__init__.py` | Platform entry point (`async_setup_entry`) — instantiates every entity class. Re-exports the class names and the `_*` formula helpers so existing imports keep working. |
| `base.py` | `BevDerivedSensor` (subscribes to source-entity state changes) + `_TrackerLinkedMixin` (subscribes to `signal_baseline_updated`). |
| `formulas.py` | Pure helpers (`_efficiency_value`, `_unit_variant_props`, `_post_charge_window`, `_local_week_start`, `_window_cutoff`). |
| `instantaneous.py` | `FullBatteryRangeSensor`, `StateOfHealthSensor`, `EfficiencySensor` — only need live source entities. |
| `tracker_linked.py` | `MeasuredFullRangeSensor`, `MeasuredEfficiencySensor`, `LastChargedSensor`, `TimeSinceLastChargeSensor`, `SessionLogSensor`, `LastChargeAddedSensor`, `AverageChargingPowerSensor` — driven by `ChargeTracker` baselines. |
| `distance.py` | `DistanceRolling7DaysSensor`, `DistanceThisWeekSensor`, `DaysToLowSocSensor`. |
| `window.py` | `_WindowedSensor` base + `EnergyConsumedWindowSensor`, `StandstillConsumptionWindowSensor`, `StandstillRatioWindowSensor`, `ChargeCountWindowSensor`, `AverageEfficiencyWindowSensor`. |

## Sensor pattern

Every derived sensor:

1. Subclasses `BevDerivedSensor` (and `_TrackerLinkedMixin` if it
   needs charge-end baseline / last-session data).
2. Declares `_attr_unique_id = f"{entry.entry_id}_<suffix>"` — the
   suffix is what tests match against via `_find_state(hass, suffix)`.
3. Implements `_recalculate()` to update `_attr_native_value` and
   `_attr_available`. Source-entity changes trigger this automatically
   through the base class's state-change listener.
4. Goes to `_attr_available = False` rather than reporting a stale or
   guessed value whenever a source is missing.
5. If capacity-dependent, is instantiated **four times**
   (2 capacities × 2 units = factory/actual × kWh/100km / km/kWh).
   The shared formula lives in `_efficiency_value()` in `sensor/formulas.py`.

Measured-range and measured-efficiency sensors additionally check
`tracker.is_charging` and the `MIN_MEASURED_RANGE_KM` /
`MIN_MEASURED_RANGE_SOC_PERCENT` floors before computing — all of that
guard logic is centralised in `_post_charge_window()` in
`sensor/formulas.py`.

## Histories and windows

`MileageHistory` and `SocHistory` are thin `EntityHistory` subclasses
that record `(timestamp, value)` tuples on each state change, prune
samples older than 8 days, and persist via `Store`. They fire
per-entry dispatcher signals:

```python
signal_mileage_history_updated(entry_id)
signal_soc_history_updated(entry_id)
signal_baseline_updated(entry_id)
```

Window sensors subscribe to those signals plus an `async_track_time_change`
hourly tick so the rolling window keeps rolling when nothing else moves.
On fresh installs (no pre-window anchor sample), `delta_since` and
`consumed_since` fall back to the oldest available sample as the anchor
and the sensor exposes `partial_window_data: true` in attributes.

The **recorder backfill** (`backfill.py`) runs once on first setup and
primes the deques with up to 8 days of historic samples from HA's
built-in recorder, so window sensors typically light up immediately
instead of waiting for live recording. Wrapped in try/except so an
absent/older recorder API is silently ignored.

## Legacy-storage migration

`_migrate_legacy_storage` runs as the first step of `async_setup_entry`.
It:

1. Globs `.storage/` for `myskoda_insights.{charge_tracker,mileage_history,soc_history}.*`
   files (file-system discovery — works in production with real disk I/O).
2. For each match, loads the legacy data through the `Store` API and
   re-writes it under `bev_insights.<suffix>.<current_entry_id>`. Skips
   any entry where the new key already holds data (idempotent).
3. Calls `Store.async_remove` on the legacy key so it goes away cleanly
   in both production (deletes file) and tests (clears mock dict).

The split between disk-glob and Store-API is what makes this testable
under `pytest_homeassistant_custom_component`'s mocked storage: the
test pre-creates a placeholder disk file (for the glob) **and** seeds
`hass_storage` (for the Store load).

## Test patterns

- `tests/common.py` — `make_entry()`, `base_entry_data()`, canonical
  entity-ID constants. Use these instead of building `MockConfigEntry`
  by hand.
- `tests/test_sensors.py::_find_state(hass, suffix)` — locate an
  entity by **unique-id suffix** through the registry. Entity-id slugs
  depend on title + translations, but unique IDs are stable.
- Window-sensor math tests **seed `history._samples` directly** and
  fire the dispatcher signal to trigger recompute — see
  `tests/test_window_sensors.py`. This bypasses the entity-listener
  path so the formulas can be exercised against known data.
- `DistanceThisWeekSensor` listens to the **mileage entity directly**
  (not the dispatcher signal) — to trigger its recompute in tests,
  set the mileage entity state.
- Tests touching `Store` data accept `hass_storage` as a fixture
  parameter; that activates the in-memory mock dict.
- `tests/test_snapshots.py` — schema snapshot pinning every entity's
  `(device_class, state_class, unit, entity_category)` triple plus the
  diagnostics dump. Update with `pytest --snapshot-update`.
- `tests/test_lts_compliance.py` — programmatic check that every
  device-classed entity satisfies HA's
  `DEVICE_CLASS_STATE_CLASSES` / `DEVICE_CLASS_UNITS` tables, and that
  every `state_class=TOTAL` sensor with a live value publishes
  `last_reset`. Catches HA tightening these rules in future releases.
- **Snapshot dir override**: `tests/conftest.py` defines our own
  `snapshot` fixture that pins the directory to `tests/snapshots/` via
  a tiny `AmberSnapshotExtension` subclass. We can't rely on the HA
  test plugin's `snapshot` fixture override surviving on every matrix
  row (it broke on the py3.14 + HA-dev row); the conftest override
  wins because conftest fixtures beat plugin fixtures.

## Conventions

- Docstrings: ruff enforces pydocstyle but ignores `D102` and `D107` —
  class-level docstrings describe each entity; per-method docstrings
  on overridden `_recalculate`/`__init__` are repetitive busywork.
  Public **functions** (`signal_*_updated`, `_local_week_start`)
  do need docstrings.
- Type hints: production code is fully typed and checked under
  `mypy --strict`. Tests are intentionally out of mypy scope.
- `async_track_time_change` callbacks must accept a `datetime`
  argument; dispatcher callbacks don't. Use **two separate functions**
  rather than one `_=None` placeholder — strict mypy rejects the
  bare placeholder.
- `entry.data` is a `MappingProxyType`. Wrap with `dict(entry.data)`
  before passing to helpers typed as `dict[str, Any]`.
- Always verify maths with a small standalone Python script that
  mirrors the production formula before considering a feature done.

## Adding a new sensor (rough recipe)

1. Pick the right sub-module of `sensor/`:
   - Only depends on live source entities → `sensor/instantaneous.py`.
   - Needs `ChargeTracker` baseline or last session → `sensor/tracker_linked.py`.
   - Rolling 7-day or this-week window → `sensor/window.py` (subclass `_WindowedSensor`).
   - Driven by mileage history but not a window → `sensor/distance.py`.
2. Add the class, subclassing `BevDerivedSensor` (and `_TrackerLinkedMixin`
   or `_WindowedSensor` if relevant). Pure formula helpers go in
   `sensor/formulas.py`.
3. Set a stable `_attr_unique_id` suffix; add it to `EXPECTED_SUFFIXES`
   in `tests/test_setup_smoke.py` and update the `len(suffixes) == N`
   assertion.
4. Instantiate it in `sensor/__init__.py::async_setup_entry`, gated on
   the same prerequisites as related sensors. Re-export the class name
   from `sensor/__init__.py`'s `__all__`.
5. Add formula tests in `tests/test_sensors.py` (instantaneous) or
   `tests/test_window_sensors.py` (window-based).
6. Add a translation key to `translations/en.json` AND `translations/de.json`
   (`tests/test_translations.py` enforces de-vs-en parity).
7. Update `CHANGELOG.md` and bump the version in `manifest.json`.
8. **Snapshot tests will fail** until you accept them:
   `pytest --snapshot-update tests/test_snapshots.py`.
9. Run `pytest && ruff check custom_components tests && mypy`.

## What to avoid

- Don't extrapolate from zero or report guessed values — stay
  `unavailable` until real data is present.
- Don't bake fixed kWh values into sensors — go through `CapacitySource`
  so the actual-capacity helper can update live.
- Don't add per-method docstrings on overridden `_recalculate`/`__init__`
  just to silence a linter — the project ignores those rules deliberately.
- Don't write to `entry.data` directly; use the config flow's
  reconfigure step and let HA reload the entry.
- Don't reference the legacy `myskoda_insights` domain in new code —
  only `_migrate_legacy_storage` should know it exists.
- **LTS rules** (enforced by `tests/test_lts_compliance.py` against HA's
  live tables; HA logs a warning when violated):
  - `device_class=ENERGY` accepts only `TOTAL` or `TOTAL_INCREASING`
    — **never** `MEASUREMENT`. For a sliding window of kWh, drop the
    device class and use `MEASUREMENT` (see `EnergyConsumedWindowSensor`'s
    `rolling_7_days` branch for the pattern).
  - `device_class=POWER` accepts only `MEASUREMENT`.
  - `device_class=TIMESTAMP` rejects any `state_class`.
  - Every `state_class=TOTAL` sensor must set `_attr_last_reset` (else
    LTS treats it as a continuous accumulator forever). If the
    last-reset anchor can't be computed (e.g. corrupt session
    timestamp), go `unavailable` rather than reporting a value with a
    stale anchor — `LastChargeAddedSensor` does this.
- Don't set `_attr_last_reset` from inside a `_recalculate` that might
  bail with `unavailable` — assign it only after all guard clauses pass.
