"""Base class and tracker-baseline-listening mixin for derived sensors."""
from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_track_state_change_event

from custom_components.bev_insights.const import DOMAIN, signal_baseline_updated

if TYPE_CHECKING:
    # Added to homeassistant.core in HA 2024.x; the declared minimum
    # predates it. Type-only import so we keep the precise annotation
    # without breaking runtime imports on older HA versions.
    from homeassistant.core import EventStateChangedData


class BevDerivedSensor(SensorEntity):
    """Base class for sensors that recompute when source entities change."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, source_entities: list[str]) -> None:
        self._entry = entry
        self._source_entities = source_entities
        self._attr_available = False

    @property
    def device_info(self) -> DeviceInfo:
        """Group all derived sensors of one config entry under one device."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=self._entry.title,
            manufacturer="BEV Insights",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to state changes of source entities."""

        if self._source_entities:

            @callback
            def _state_listener(event: Event[EventStateChangedData]) -> None:
                self._recalculate()
                self.async_write_ha_state()

            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, self._source_entities, _state_listener
                )
            )

        # Compute an initial value as soon as we're added to hass.
        self._recalculate()

    @callback
    def _recalculate(self) -> None:
        """Override in subclasses to update self._attr_native_value."""
        raise NotImplementedError


class _TrackerLinkedMixin:
    """Adds a subscription to baseline-updated dispatcher signals.

    Mixin used by sensors that need to recompute when the ChargeTracker
    writes a new baseline. Expects the host class to define `self._entry`
    and the standard HA entity API (`hass`, `async_on_remove`,
    `_recalculate`, `async_write_ha_state`).
    """

    # Attributes/methods supplied by the host class. Declared here as
    # PEP 526 annotations (no assignment) so mypy can resolve them on the
    # mixin without creating runtime attributes that would shadow the
    # host's implementations under MRO.
    _entry: ConfigEntry
    hass: HomeAssistant
    async_on_remove: Callable[[Callable[[], None]], None]
    async_write_ha_state: Callable[[], None]
    _recalculate: Callable[[], None]

    def _subscribe_baseline_updates(self) -> None:
        @callback
        def _baseline_listener() -> None:
            self._recalculate()
            self.async_write_ha_state()

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_baseline_updated(self._entry.entry_id),
                _baseline_listener,
            )
        )


__all__ = ["BevDerivedSensor", "_TrackerLinkedMixin"]
