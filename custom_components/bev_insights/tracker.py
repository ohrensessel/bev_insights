"""Charge-end tracker and entity history classes.

ChargeTracker watches the configured charging-state entity. When it sees the
entity transition out of the "charging" state, it captures the current
odometer reading and state of charge — that pair forms the baseline used by
the "measured full range" sensor.

EntityHistory is a generic rolling-window store for timestamped float samples
from a single HA entity. MileageHistory specialises it for odometer readings.

All persisted data survives Home Assistant restarts via
`homeassistant.helpers.storage.Store`.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

if TYPE_CHECKING:
    # Added to homeassistant.core in HA 2024.x; the integration's declared
    # minimum predates it. Import only when type-checking so we can keep
    # the precise annotation without breaking runtime imports on older HA.
    from homeassistant.core import EventStateChangedData

from .const import (
    BASELINE_MILEAGE_KM,
    BASELINE_SOC_PERCENT,
    BASELINE_TIMESTAMP,
    LAST_SESSION_KEY,
    MILEAGE_HISTORY_DAYS,
    MILEAGE_HISTORY_KEY_PREFIX,
    SESSION_END_SOC_PERCENT,
    SESSION_END_TIMESTAMP,
    SESSION_LOG_KEY,
    SESSION_LOG_MAX,
    SESSION_START_SOC_PERCENT,
    SESSION_START_TIMESTAMP,
    SOC_HISTORY_DAYS,
    SOC_HISTORY_KEY_PREFIX,
    STANDSTILL_MOVEMENT_THRESHOLD_KM,
    STORAGE_KEY_PREFIX,
    STORAGE_VERSION,
    signal_baseline_updated,
    signal_mileage_history_updated,
    signal_soc_history_updated,
)
from .util import (
    INVALID_STATES,
    _DISTANCE_TO_KM,
    is_charging,
    read_distance_km,
    read_float,
)

_LOGGER = logging.getLogger(__name__)


class ChargeTracker:
    """Tracks the last charge-end event for one vehicle/config entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        charging_entity: str,
        mileage_entity: str,
        soc_entity: str,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self._charging_entity = charging_entity
        self._mileage_entity = mileage_entity
        self._soc_entity = soc_entity
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{STORAGE_KEY_PREFIX}.{entry.entry_id}"
        )
        self._baseline: dict[str, Any] | None = None
        # Most recently completed charging session (set on the falling edge
        # when we have a matching rising-edge sample). Persisted.
        self._last_session: dict[str, Any] | None = None
        # Rolling log of the last SESSION_LOG_MAX completed sessions.
        self._session_log: deque[dict[str, Any]] = deque(maxlen=SESSION_LOG_MAX)
        # In-memory only: SoC + timestamp captured on the rising edge of the
        # current charging session. Cleared when the session ends or HA
        # restarts mid-charge (in which case that one cycle won't produce a
        # complete `last_session`).
        self._pending_start: dict[str, Any] | None = None
        self._unsub: Callable[[], None] | None = None

    # ------------------------------------------------------------------ #
    # Lifecycle                                                          #
    # ------------------------------------------------------------------ #

    async def async_load(self) -> None:
        """Load persisted baseline + last_session from disk, if any."""
        data = await self._store.async_load()
        if not isinstance(data, dict):
            return
        if BASELINE_MILEAGE_KM in data:
            self._baseline = {
                BASELINE_MILEAGE_KM: data[BASELINE_MILEAGE_KM],
                BASELINE_SOC_PERCENT: data.get(BASELINE_SOC_PERCENT),
                BASELINE_TIMESTAMP: data.get(BASELINE_TIMESTAMP),
            }
            _LOGGER.debug(
                "Loaded charge-end baseline for %s: %s",
                self.entry.entry_id,
                self._baseline,
            )
        last_session = data.get(LAST_SESSION_KEY)
        if isinstance(last_session, dict):
            self._last_session = last_session
        raw_log = data.get(SESSION_LOG_KEY)
        if isinstance(raw_log, list):
            for item in raw_log[-SESSION_LOG_MAX:]:
                if isinstance(item, dict):
                    self._session_log.append(item)

    @callback
    def async_start(self) -> None:
        """Subscribe to charging-state changes."""
        self._unsub = async_track_state_change_event(
            self.hass,
            [self._charging_entity],
            self._on_charging_state_changed,
        )

    async def async_stop(self) -> None:
        """Tear down listeners."""
        if self._unsub:
            self._unsub()
            self._unsub = None

    # ------------------------------------------------------------------ #
    # Public read API                                                    #
    # ------------------------------------------------------------------ #

    @property
    def baseline(self) -> dict[str, Any] | None:
        """Return the persisted baseline dict, or None if never charged."""
        return self._baseline

    @property
    def last_session(self) -> dict[str, Any] | None:
        """Return the most recent completed charge session, or None.

        Populated only when a full off→on→off cycle has been observed.
        Contains `start_soc_percent`, `end_soc_percent`, `start_timestamp`,
        `end_timestamp`.
        """
        return self._last_session

    @property
    def session_log(self) -> list[dict[str, Any]]:
        """Return the session log as a list (oldest first)."""
        return list(self._session_log)

    @property
    def is_charging(self) -> bool:
        """Return True if the vehicle is currently charging.

        Read live from the charging-state entity so the answer reflects the
        present moment, not the last edge transition the tracker observed.
        """
        # The bare `is_charging` below resolves to the module-level function
        # imported at the top of this file, not to this property — Python
        # looks up unqualified names in module scope.
        return is_charging(self.hass.states.get(self._charging_entity))

    # ------------------------------------------------------------------ #
    # State-change handling                                              #
    # ------------------------------------------------------------------ #

    @callback
    def _on_charging_state_changed(
        self, event: Event[EventStateChangedData]
    ) -> None:
        """React to charging-state transitions.

        Rising edge (off → on) → record start SoC for "kWh added".
        Falling edge (on → off) → capture the end baseline and finalise
        the completed session.
        """
        old_state = event.data.get("old_state")
        new_state = event.data.get("new_state")
        was_charging = is_charging(old_state)
        is_now_charging = is_charging(new_state)
        if not was_charging and is_now_charging:
            self._capture_pending_start()
        elif was_charging and not is_now_charging:
            self._capture_baseline()

    @callback
    def _capture_pending_start(self) -> None:
        """Record SoC + timestamp at the start of a charge session."""
        soc = read_float(self.hass, self._soc_entity)
        if soc is None:
            _LOGGER.debug(
                "Charge start detected but SoC unavailable; "
                "last-charge-added will be unavailable for this cycle"
            )
            self._pending_start = None
            return
        self._pending_start = {
            SESSION_START_SOC_PERCENT: soc,
            SESSION_START_TIMESTAMP: dt_util.utcnow().isoformat(),
        }
        _LOGGER.info("Charge start captured at %.1f%% SoC", soc)

    @callback
    def _capture_baseline(self) -> None:
        """Read mileage + SoC and persist them as the new baseline."""
        mileage = read_distance_km(self.hass, self._mileage_entity)
        soc = read_float(self.hass, self._soc_entity)
        if mileage is None or soc is None:
            _LOGGER.warning(
                "Charge end detected but mileage or SoC unavailable "
                "(mileage=%s, soc=%s); not updating baseline",
                mileage,
                soc,
            )
            return

        end_ts = dt_util.utcnow().isoformat()
        self._baseline = {
            BASELINE_MILEAGE_KM: mileage,
            BASELINE_SOC_PERCENT: soc,
            BASELINE_TIMESTAMP: end_ts,
        }
        _LOGGER.info(
            "Charge end captured: %.1f km @ %.1f%% SoC", mileage, soc
        )

        # If we observed the rising edge of this session, finalise it as a
        # complete `last_session` so the "kWh added" sensors can read it.
        if self._pending_start is not None:
            self._last_session = {
                SESSION_START_SOC_PERCENT: self._pending_start[
                    SESSION_START_SOC_PERCENT
                ],
                SESSION_START_TIMESTAMP: self._pending_start[
                    SESSION_START_TIMESTAMP
                ],
                SESSION_END_SOC_PERCENT: soc,
                SESSION_END_TIMESTAMP: end_ts,
            }
            self._session_log.append(self._last_session)
            self._pending_start = None

        self.hass.async_create_task(self._store.async_save(self._persisted_payload()))
        async_dispatcher_send(
            self.hass, signal_baseline_updated(self.entry.entry_id)
        )

    def _persisted_payload(self) -> dict[str, Any]:
        """Build the dict written to Store.

        Baseline keys sit at the top level (backwards-compatible with v0.7
        on-disk format); the completed session goes under `last_session`.
        """
        payload: dict[str, Any] = dict(self._baseline or {})
        if self._last_session is not None:
            payload[LAST_SESSION_KEY] = self._last_session
        if self._session_log:
            payload[SESSION_LOG_KEY] = list(self._session_log)
        return payload


class EntityHistory:
    """Rolling window of timestamped float samples for one HA entity.

    Records (timestamp, value) tuples whenever the source entity changes,
    prunes samples older than `max_age_days` and persists the rest across
    HA restarts. Subclasses define how to read the entity (`_read`) and
    what dispatcher signal to fire on update (`_signal`).
    """

    # --- subclass hooks ------------------------------------------------ #

    _label: str = "value"
    _value_key: str = "value"

    def _read(self) -> float | None:  # pragma: no cover - overridden
        raise NotImplementedError

    def _signal(self) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    # --- impl ---------------------------------------------------------- #

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        source_entity: str,
        storage_key_prefix: str,
        max_age_days: int,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self._source_entity = source_entity
        self._store: Store[dict[str, Any]] = Store(
            hass,
            STORAGE_VERSION,
            f"{storage_key_prefix}.{entry.entry_id}",
        )
        self._samples: deque[tuple[datetime, float]] = deque()
        self._unsub: Callable[[], None] | None = None
        self._max_age = timedelta(days=max_age_days)

    # Lifecycle --------------------------------------------------------- #

    async def async_load(self) -> None:
        data = await self._store.async_load()
        if not isinstance(data, dict):
            return
        raw = data.get("samples") or []
        now = dt_util.utcnow()
        restored: list[tuple[datetime, float]] = []
        for item in raw:
            try:
                ts = dt_util.parse_datetime(item["timestamp"])
                value = float(item[self._value_key])
            except (KeyError, TypeError, ValueError):
                continue
            if ts is None or now - ts > self._max_age:
                continue
            restored.append((ts, value))
        restored.sort(key=lambda s: s[0])
        self._samples.extend(restored)
        _LOGGER.debug(
            "Loaded %d %s samples for %s",
            len(self._samples),
            self._label,
            self.entry.entry_id,
        )

    @callback
    def async_start(self) -> None:
        self._unsub = async_track_state_change_event(
            self.hass,
            [self._source_entity],
            self._on_state_changed,
        )
        self._record_current()

    async def async_stop(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None

    # Read API ---------------------------------------------------------- #

    def delta_since(self, cutoff: datetime) -> float | None:
        """Return `latest - baseline`, or None if there are no samples.

        Baseline is the newest sample at or before `cutoff`. When no such
        sample exists (e.g. fresh install), falls back to the oldest
        available sample so the sensor shows a partial value rather than
        staying unavailable indefinitely.
        """
        if not self._samples:
            return None
        latest_value = self._samples[-1][1]
        baseline_value: float | None = None
        for ts, value in self._samples:
            if ts <= cutoff:
                baseline_value = value
            else:
                break
        if baseline_value is None:
            baseline_value = self._samples[0][1]
        return self._postprocess_delta(latest_value - baseline_value)

    def _postprocess_delta(self, raw_delta: float) -> float:
        """Hook for subclasses to clamp / re-sign the raw delta."""
        return raw_delta

    @property
    def has_data(self) -> bool:
        return bool(self._samples)

    @property
    def sample_count(self) -> int:
        return len(self._samples)

    @property
    def oldest_sample(self) -> tuple[datetime, float] | None:
        return self._samples[0] if self._samples else None

    @property
    def latest_sample(self) -> tuple[datetime, float] | None:
        return self._samples[-1] if self._samples else None

    def has_pre_window_sample(self, cutoff: datetime) -> bool:
        """Return True if at least one sample predates the window cutoff."""
        return bool(self._samples) and self._samples[0][0] <= cutoff

    async def async_backfill(self, states: list[State]) -> None:
        """Insert historical State objects into the deque on first install.

        Only runs when the deque is currently empty — a no-op otherwise so
        re-loading the integration never overwrites real data. Non-numeric and
        out-of-max-age entries are skipped. Consecutive duplicates are dropped
        by the same rule as live recording.
        """
        if self._samples:
            return
        now = dt_util.utcnow()
        for state in states:
            if state.state in INVALID_STATES:
                continue
            if now - state.last_updated > self._max_age:
                continue
            value = self._backfill_parse(state)
            if value is None:
                continue
            if self._samples and self._samples[-1][1] == value:
                continue
            self._samples.append((state.last_updated, value))
        if self._samples:
            self._prune(now)
            await self._persist()

    def _backfill_parse(self, state: State) -> float | None:
        """Parse a historical State for backfill. Override in subclasses."""
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return None

    def value_at(self, ts: datetime) -> float | None:
        """Return the most recent sample value at or before `ts`, or None."""
        result: float | None = None
        for sample_ts, value in self._samples:
            if sample_ts <= ts:
                result = value
            else:
                break
        return result

    # Internals --------------------------------------------------------- #

    @callback
    def _on_state_changed(
        self, event: Event[EventStateChangedData]
    ) -> None:
        self._record_current()

    @callback
    def _record_current(self) -> None:
        value = self._read()
        if value is None:
            return
        now = dt_util.utcnow()

        if self._samples and self._samples[-1][1] == value:
            return

        self._samples.append((now, value))
        self._prune(now)
        self.hass.async_create_task(self._persist())
        async_dispatcher_send(self.hass, self._signal())

    def _prune(self, now: datetime) -> None:
        cutoff = now - self._max_age
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    async def _persist(self) -> None:
        payload = {
            "samples": [
                {"timestamp": ts.isoformat(), self._value_key: value}
                for ts, value in self._samples
            ]
        }
        await self._store.async_save(payload)


class MileageHistory(EntityHistory):
    """Rolling window of odometer samples in km."""

    _label = "mileage"
    _value_key = "mileage_km"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        mileage_entity: str,
        max_age_days: int = MILEAGE_HISTORY_DAYS,
    ) -> None:
        super().__init__(
            hass,
            entry,
            source_entity=mileage_entity,
            storage_key_prefix=MILEAGE_HISTORY_KEY_PREFIX,
            max_age_days=max_age_days,
        )

    def _read(self) -> float | None:
        return read_distance_km(self.hass, self._source_entity)

    def _signal(self) -> str:
        return signal_mileage_history_updated(self.entry.entry_id)

    def distance_since(self, cutoff: datetime) -> float | None:
        return self.delta_since(cutoff)

    def _postprocess_delta(self, raw_delta: float) -> float:
        return max(0.0, raw_delta)

    def _backfill_parse(self, state: State) -> float | None:
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            return None
        unit = state.attributes.get("unit_of_measurement") or "km"
        return value * _DISTANCE_TO_KM.get(unit, 1.0)


class SocHistory(EntityHistory):
    """Rolling window of state-of-charge samples in percent.

    Used to compute kWh consumed over a window:
        kWh = capacity * soc_consumed_percent / 100
    where `soc_consumed_percent` sums up all the SoC decreases between
    sample points, ignoring the upward jumps caused by charging.
    """

    _label = "soc"
    _value_key = "soc_percent"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        soc_entity: str,
        max_age_days: int = SOC_HISTORY_DAYS,
    ) -> None:
        super().__init__(
            hass,
            entry,
            source_entity=soc_entity,
            storage_key_prefix=SOC_HISTORY_KEY_PREFIX,
            max_age_days=max_age_days,
        )

    def _read(self) -> float | None:
        value = read_float(self.hass, self._source_entity)
        if value is None:
            return None
        return min(max(value, 0.0), 100.0)

    def _signal(self) -> str:
        return signal_soc_history_updated(self.entry.entry_id)

    def _backfill_parse(self, state: State) -> float | None:
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            return None
        return min(max(value, 0.0), 100.0)

    def consumed_since(self, cutoff: datetime) -> float | None:
        """Return total SoC consumed (percent) since `cutoff`, or None.

        Walks the samples chronologically and sums the magnitude of each
        downward step. Upward steps (charging) are skipped.
        """
        if not self._samples:
            return None
        anchor_index: int | None = None
        for i, (ts, _) in enumerate(self._samples):
            if ts <= cutoff:
                anchor_index = i
            else:
                break
        if anchor_index is None:
            anchor_index = 0
        consumed = 0.0
        previous_value = self._samples[anchor_index][1]
        for _, value in list(self._samples)[anchor_index + 1 :]:
            if value < previous_value:
                consumed += previous_value - value
            previous_value = value
        return consumed

    def charge_count_since(self, cutoff: datetime, min_rise_percent: float = 5.0) -> int:
        """Count charging sessions that completed since `cutoff`.

        A session is a contiguous run of upward SoC steps whose total rise
        is at least `min_rise_percent`. The 5 % floor filters out the small
        upward ticks caused by SoC sensor quantization noise.
        """
        if not self._samples:
            return 0
        anchor_index: int | None = None
        for i, (ts, _) in enumerate(self._samples):
            if ts <= cutoff:
                anchor_index = i
            else:
                break
        if anchor_index is None:
            anchor_index = 0
        samples = list(self._samples)[anchor_index:]
        count = 0
        session_rise = 0.0
        in_charge = False
        for i in range(len(samples) - 1):
            _, soc_cur = samples[i]
            _, soc_next = samples[i + 1]
            delta = soc_next - soc_cur
            if delta > 0:
                session_rise += delta
                in_charge = True
            else:
                if in_charge and session_rise >= min_rise_percent:
                    count += 1
                session_rise = 0.0
                in_charge = False
        # Catch an open session at the end of the window.
        if in_charge and session_rise >= min_rise_percent:
            count += 1
        return count

    def standstill_consumed_since(
        self,
        cutoff: datetime,
        mileage: MileageHistory,
        threshold_km: float = STANDSTILL_MOVEMENT_THRESHOLD_KM,
    ) -> float | None:
        """Return SoC% consumed while the car was parked since `cutoff`, or None.

        Walks SoC sample intervals chronologically. An interval is counted as
        standstill if the odometer advanced by less than `threshold_km` over that
        period. Upward SoC steps (charging) are always skipped. Returns None when
        there is no SoC or mileage data to work with.
        """
        if not self._samples or not mileage.has_data:
            return None
        anchor_index: int | None = None
        for i, (ts, _) in enumerate(self._samples):
            if ts <= cutoff:
                anchor_index = i
            else:
                break
        if anchor_index is None:
            anchor_index = 0
        consumed = 0.0
        soc_samples = list(self._samples)[anchor_index:]
        for i in range(len(soc_samples) - 1):
            t_start, soc_start = soc_samples[i]
            t_end, soc_end = soc_samples[i + 1]
            soc_drop = soc_start - soc_end
            if soc_drop <= 0:
                continue
            mileage_start = mileage.value_at(t_start)
            mileage_end = mileage.value_at(t_end)
            if mileage_start is None or mileage_end is None:
                continue
            if mileage_end - mileage_start >= threshold_km:
                continue
            consumed += soc_drop
        return consumed
