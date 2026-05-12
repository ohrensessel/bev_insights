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

import logging
from collections import deque
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import (
    Event,
    EventStateChangedData,
    HomeAssistant,
    callback,
)
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    BASELINE_MILEAGE_KM,
    BASELINE_SOC_PERCENT,
    BASELINE_TIMESTAMP,
    MILEAGE_HISTORY_DAYS,
    MILEAGE_HISTORY_KEY_PREFIX,
    STORAGE_KEY_PREFIX,
    STORAGE_VERSION,
    signal_baseline_updated,
    signal_mileage_history_updated,
)
from .util import is_charging, read_distance_km, read_float

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
        self._unsub: callable | None = None

    # ------------------------------------------------------------------ #
    # Lifecycle                                                          #
    # ------------------------------------------------------------------ #

    async def async_load(self) -> None:
        """Load persisted baseline from disk, if any."""
        data = await self._store.async_load()
        if isinstance(data, dict) and BASELINE_MILEAGE_KM in data:
            self._baseline = data
            _LOGGER.debug(
                "Loaded charge-end baseline for %s: %s",
                self.entry.entry_id,
                self._baseline,
            )

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

    # ------------------------------------------------------------------ #
    # State-change handling                                              #
    # ------------------------------------------------------------------ #

    @callback
    def _on_charging_state_changed(
        self, event: Event[EventStateChangedData]
    ) -> None:
        """Capture baseline on the trailing edge of a charging session."""
        old_state = event.data.get("old_state")
        new_state = event.data.get("new_state")
        if is_charging(old_state) and not is_charging(new_state):
            self._capture_baseline()

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

        self._baseline = {
            BASELINE_MILEAGE_KM: mileage,
            BASELINE_SOC_PERCENT: soc,
            BASELINE_TIMESTAMP: dt_util.utcnow().isoformat(),
        }
        _LOGGER.info(
            "Charge end captured: %.1f km @ %.1f%% SoC", mileage, soc
        )

        self.hass.async_create_task(self._store.async_save(self._baseline))
        async_dispatcher_send(
            self.hass, signal_baseline_updated(self.entry.entry_id)
        )


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
        self._unsub: callable | None = None
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
        """Return `latest - baseline` where baseline is the newest sample
        at or before `cutoff`. Returns None when there's no such baseline.
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
            return None
        return self._postprocess_delta(latest_value - baseline_value)

    def _postprocess_delta(self, raw_delta: float) -> float:
        """Hook for subclasses to clamp / re-sign the raw delta."""
        return raw_delta

    @property
    def has_data(self) -> bool:
        return bool(self._samples)

    @property
    def oldest_sample(self) -> tuple[datetime, float] | None:
        return self._samples[0] if self._samples else None

    @property
    def latest_sample(self) -> tuple[datetime, float] | None:
        return self._samples[-1] if self._samples else None

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
    ) -> None:
        super().__init__(
            hass,
            entry,
            source_entity=mileage_entity,
            storage_key_prefix=MILEAGE_HISTORY_KEY_PREFIX,
            max_age_days=MILEAGE_HISTORY_DAYS,
        )

    def _read(self) -> float | None:
        return read_distance_km(self.hass, self._source_entity)

    def _signal(self) -> str:
        return signal_mileage_history_updated(self.entry.entry_id)

    def distance_since(self, cutoff: datetime) -> float | None:
        return self.delta_since(cutoff)

    def _postprocess_delta(self, raw_delta: float) -> float:
        return max(0.0, raw_delta)
