# Agent guide — MySkoda Insights

Contributor-facing notes for AI coding agents and humans extending this
integration. For user-facing setup, see `README.md`.

## What this is

A Home Assistant custom integration that consumes sensors from the
upstream `homeassistant-myskoda` integration and exposes derived EV
insights (efficiency, measured range, weekly distance/energy, state of
health, etc.) — currently 29 sensors per fully-wired config entry.

## Commands

Everything runs from the repo root with a virtualenv that has
`requirements_test.txt` installed.

```bash
pytest                    # full test suite (~5s, 97 tests)
ruff check custom_components tests
mypy                      # config-driven, scopes to custom_components/
python -m py_compile custom_components/myskoda_insights/*.py
```

CI (`.github/workflows/tests.yml`) runs three jobs in parallel:
`lint` (ruff), `mypy`, and `pytest` (3.12 + 3.13 matrix).

## Code map

| File | Responsibility |
|---|---|
| `__init__.py` | `async_setup_entry` wires tracker + histories + capacity sources; `async_migrate_entry` handles v1→v2. |
| `sensor.py` | Every sensor class. Base: `MySkodaDerivedSensor`. Tracker-linked sensors use `_TrackerLinkedMixin`. Window sensors use `_WindowedSensor`. |
| `tracker.py` | `ChargeTracker` (charge-end baseline + last session), `EntityHistory` base, `MileageHistory`, `SocHistory`. 8-day rolling deques persisted via `Store`. |
| `capacity.py` | `CapacitySource` ABC → `FixedCapacity` (nameplate kWh) and `EntityCapacity` (live `input_number` / sensor). |
| `util.py` | `read_float`, `read_distance_km` (unit-aware), `is_charging`. |
| `const.py` | All constants; per-entry dispatcher signal name builders. |
| `config_flow.py` | v2 schema, `async_step_user` + `async_step_reconfigure`. |

## Sensor pattern

Every derived sensor:

1. Subclasses `MySkodaDerivedSensor` (and `_TrackerLinkedMixin` if it
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
   The shared formula lives in `_efficiency_value()` in `sensor.py`.

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

1. Add the class in `sensor.py`, subclassing `MySkodaDerivedSensor`
   (and `_TrackerLinkedMixin` if relevant).
2. Set a stable `_attr_unique_id` suffix; add it to `EXPECTED_SUFFIXES`
   in `tests/test_setup_smoke.py`.
3. Instantiate it in `async_setup_entry` (`sensor.py`), gated on the
   same prerequisites as related sensors.
4. Add formula tests in `tests/test_sensors.py` (instantaneous) or
   `tests/test_window_sensors.py` (window-based).
5. Add a translation key to `translations/` if user-visible.
6. Update `CHANGELOG.md` and bump the version in `manifest.json`.
7. Run `pytest && ruff check custom_components tests && mypy`.

## What to avoid

- Don't extrapolate from zero or report guessed values — stay
  `unavailable` until real data is present.
- Don't bake fixed kWh values into sensors — go through `CapacitySource`
  so the actual-capacity helper can update live.
- Don't add per-method docstrings on overridden `_recalculate`/`__init__`
  just to silence a linter — the project ignores those rules deliberately.
- Don't write to `entry.data` directly; use the config flow's
  reconfigure step and let HA reload the entry.
