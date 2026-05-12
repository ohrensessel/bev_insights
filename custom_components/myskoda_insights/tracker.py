"""Charge-end tracker.

Watches the configured charging-state entity. When it sees the entity
transition out of the "charging" state, it captures the current odometer
reading and state of charge — that pair forms the baseline used by the
"measured full range" sensor.

The baseline is persisted via `homeassistant.helpers.storage.Store` so it
survives Home Assistant restarts.

Sensors subscribe to baseline updates via the dispatcher signal returned by
`signal_baseline_updated(entry_id)`.
"""
from __future__ import annotations

import logging
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
    STORAGE_KEY_PREFIX,
    STORAGE_VERSION,
    signal_baseline_updated,
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
