"""Long-term statistics-backed distance sensors.

The window sensors in `window.py` are capped at the 8-day in-memory deque
because keeping a year of every-state-change samples in RAM is silly.
These sensors instead query HA's recorder *statistics* table, which is
retained indefinitely (separately from the recorder's purge window), to
expose monthly and yearly totals without growing the deque.

Caveats:
- Statistics are recorded by HA only for entities with a `state_class`
  declared by their upstream integration (typically `total_increasing`
  for an odometer). If the user's mileage entity lacks a state class the
  query returns nothing and these sensors stay unavailable — soft
  failure, no error surfaced.
- Statistics are aggregated hourly. The "start of the period" value is
  read from the first hourly row at or after the period boundary, so
  the reported total can be off by up to one hour. For a monthly /
  yearly figure that's noise.
- On first install the entity has no statistics yet covering the
  current period; the sensor stays unavailable until the recorder's
  hourly stats job has written enough rows. Real users wait at most an
  hour after install before the value lights up.
"""
from __future__ import annotations

from datetime import datetime
import logging
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfLength
from homeassistant.core import callback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.util import dt as dt_util

from custom_components.bev_insights.util import read_distance_km

from .base import BevDerivedSensor

_LOGGER = logging.getLogger(__name__)


class _LongTermDistanceSensor(BevDerivedSensor):
    """Base for monthly / yearly cumulative-distance sensors.

    Subclasses define `_period_start()` (datetime, in UTC) and supply a
    `_period_key` used in the unique-id suffix and translation key.

    The "baseline" mileage at the start of the current period is fetched
    asynchronously from the statistics table, cached for the period, and
    invalidated when the period rolls over. Live recompute on every
    mileage state-change subtracts the cached baseline from the current
    odometer reading — no further recorder I/O needed in the hot path.
    """

    _attr_device_class = SensorDeviceClass.DISTANCE
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = UnitOfLength.KILOMETERS
    _attr_suggested_display_precision = 1

    _period_key: str = ""

    def __init__(self, entry: ConfigEntry, mileage_entity: str) -> None:
        super().__init__(entry, source_entities=[mileage_entity])
        self._mileage_entity = mileage_entity
        self._cached_period_start: datetime | None = None
        self._cached_start_value: float | None = None
        self._attr_unique_id = f"{entry.entry_id}_distance_{self._period_key}"
        self._attr_translation_key = f"distance_{self._period_key}"

    def _period_start(self) -> datetime:
        """Return the UTC datetime that marks the start of the current period."""
        raise NotImplementedError

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        @callback
        def _on_tick(_now: datetime) -> None:
            # Hourly: re-check whether the cached period start is still
            # the current one (it won't be on the first tick of a new
            # month / year) and refresh the baseline if needed.
            self.hass.async_create_task(self._async_refresh_baseline())

        self.async_on_remove(
            async_track_time_change(self.hass, _on_tick, minute=0, second=0)
        )
        # First fetch right away so the sensor doesn't sit unavailable
        # for an hour after every reload.
        self.hass.async_create_task(self._async_refresh_baseline())

    async def _async_refresh_baseline(self) -> None:
        """Fetch the start-of-period mileage if the cache is stale."""
        period_start = self._period_start()
        if (
            self._cached_period_start == period_start
            and self._cached_start_value is not None
        ):
            return
        start_value = await self._fetch_value_at(period_start)
        if start_value is None:
            # Leave the cache as-is; next tick we'll try again.
            return
        self._cached_period_start = period_start
        self._cached_start_value = start_value
        self._recalculate()
        self.async_write_ha_state()

    async def _fetch_value_at(self, ts: datetime) -> float | None:
        """Look up the odometer reading at `ts` via the statistics table.

        Returns the `state` column of the first hourly statistics row at
        or after `ts`. Silently returns None when the recorder isn't
        loaded, the API raises, or the entity has no statistics yet —
        the calling code will retry on the next tick.
        """
        if "recorder" not in self.hass.config.components:
            return None
        try:
            from homeassistant.components.recorder import get_instance  # noqa: PLC0415
            from homeassistant.components.recorder.statistics import (  # noqa: PLC0415
                statistics_during_period,
            )

            def _fetch() -> Any:
                # `statistics_during_period` returns a TypedDict whose
                # row type isn't exposed publicly; treat the result as
                # an opaque mapping and pull fields by name below.
                return statistics_during_period(
                    self.hass,
                    ts,
                    None,
                    {self._mileage_entity},
                    "hour",
                    None,
                    {"state"},
                )

            stats = await get_instance(self.hass).async_add_executor_job(_fetch)
        except Exception:  # noqa: BLE001
            _LOGGER.debug(
                "Long-term distance: statistics query failed for %s",
                self._mileage_entity,
                exc_info=True,
            )
            return None
        rows = stats.get(self._mileage_entity, [])
        if not rows:
            return None
        first = rows[0]
        value = first.get("state")
        if value is None:
            return None
        return float(value)

    @callback
    def _recalculate(self) -> None:
        # `_recalculate` is called synchronously by the base class on
        # source-entity changes. Use the cached baseline only — async
        # refresh is driven by the hourly tick and on add.
        if self._cached_start_value is None:
            self._attr_available = False
            self._attr_native_value = None
            return
        period_start = self._period_start()
        if self._cached_period_start != period_start:
            # Period rolled over since the cache was set; stay
            # unavailable until the next async refresh repopulates it.
            self._attr_available = False
            self._attr_native_value = None
            return
        current = read_distance_km(self.hass, self._mileage_entity)
        if current is None:
            self._attr_available = False
            self._attr_native_value = None
            return
        self._attr_last_reset = period_start
        self._attr_available = True
        self._attr_native_value = round(
            max(0.0, current - self._cached_start_value), 1
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {
            "period": self._period_key,
            "period_start": self._period_start().isoformat(),
        }
        if self._cached_start_value is not None:
            attrs["baseline_mileage_km"] = round(self._cached_start_value, 1)
        return attrs


class DistanceThisMonthSensor(_LongTermDistanceSensor):
    """Kilometres driven since the start of the current calendar month.

    Resets to 0 at local midnight on the first day of each month. The
    `last_reset` attribute advances accordingly so HA's LTS produces
    one clean sum statistic per month.
    """

    _attr_icon = "mdi:calendar-month"
    _period_key = "this_month"

    def __init__(self, entry: ConfigEntry, mileage_entity: str) -> None:
        super().__init__(entry, mileage_entity)
        self._attr_name = "Distance driven (this month)"

    def _period_start(self) -> datetime:
        now_local = dt_util.now()
        start_local = now_local.replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        return dt_util.as_utc(start_local)


class DistanceThisYearSensor(_LongTermDistanceSensor):
    """Kilometres driven since January 1 of the current year (local time).

    Same pattern as `DistanceThisMonthSensor` but with a yearly cycle.
    """

    _attr_icon = "mdi:calendar"
    _period_key = "this_year"

    def __init__(self, entry: ConfigEntry, mileage_entity: str) -> None:
        super().__init__(entry, mileage_entity)
        self._attr_name = "Distance driven (this year)"

    def _period_start(self) -> datetime:
        now_local = dt_util.now()
        start_local = now_local.replace(
            month=1, day=1, hour=0, minute=0, second=0, microsecond=0
        )
        return dt_util.as_utc(start_local)


__all__ = ["DistanceThisMonthSensor", "DistanceThisYearSensor"]
