"""Recorder-based backfill for entity histories and the charge-end baseline.

When the SoC or mileage history deque is empty (fresh install, or storage
cleared), `async_backfill_from_recorder` queries HA's built-in recorder for the
last N days of state changes and primes the deque.

`async_backfill_tracker_from_recorder` is the equivalent for `ChargeTracker`:
walks the charging-state entity's history for the most recent off → on → off
cycle and adopts it as the baseline (and `last_session`, when both edges are
present), so measured-range / measured-efficiency / last-charge-added sensors
can produce values on day one instead of waiting for the next live charge.

Everything is wrapped in try/except so the integration loads normally even if
the recorder is unavailable, on an older HA version with a different API, or
if the entity has no history.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util

from .util import _DISTANCE_TO_KM, INVALID_STATES, is_charging

if TYPE_CHECKING:
    from .tracker import ChargeTracker, EntityHistory

_LOGGER = logging.getLogger(__name__)


async def async_backfill_from_recorder(
    hass: HomeAssistant,
    entity_history: EntityHistory,
    entity_id: str,
    days: int,
) -> None:
    """Prime entity_history from HA's recorder (best-effort, one-time).

    Skips silently when:
    - The history already has data (integration reloaded, not fresh install).
    - The recorder component is not loaded.
    - The recorder API is unavailable or raises (older HA, DB locked, etc.).
    - The entity has no recorded history in the requested window.
    """
    if entity_history.has_data:
        return

    if "recorder" not in hass.config.components:
        _LOGGER.debug(
            "BEV Insights backfill skipped for %s: recorder not loaded", entity_id
        )
        return

    start_time = dt_util.utcnow() - timedelta(days=days)

    try:
        from homeassistant.components.recorder import (  # noqa: PLC0415
            get_instance,
            history as rec_history,
        )

        def _fetch() -> list[State]:
            states = rec_history.state_changes_during_period(
                hass,
                start_time,
                entity_id=entity_id,
                no_attributes=False,
            )
            return list(states.get(entity_id, []))

        raw_states = await get_instance(hass).async_add_executor_job(_fetch)
    except Exception:  # noqa: BLE001
        _LOGGER.debug(
            "BEV Insights backfill failed for %s (recorder API unavailable or error)",
            entity_id,
            exc_info=True,
        )
        return

    if not raw_states:
        return

    await entity_history.async_backfill(raw_states)

    count = entity_history.sample_count
    if count:
        _LOGGER.info(
            "BEV Insights: backfilled %d samples for %s from recorder history",
            count,
            entity_id,
        )


def _value_at(states: list[State], ts: datetime) -> State | None:
    """Return the most recent State at or before `ts`, or None.

    `states` is assumed to be in chronological order — the order
    `state_changes_during_period` returns.
    """
    result: State | None = None
    for state in states:
        if state.last_updated <= ts:
            result = state
        else:
            break
    return result


def _parse_distance_km(state: State | None) -> float | None:
    """Parse a State for mileage in kilometres, applying the source unit."""
    if state is None or state.state in INVALID_STATES:
        return None
    try:
        value = float(state.state)
    except (TypeError, ValueError):
        return None
    unit = state.attributes.get("unit_of_measurement") or "km"
    return value * _DISTANCE_TO_KM.get(unit, 1.0)


def _parse_soc(state: State | None) -> float | None:
    """Parse a State as a SoC percentage, clamped to [0, 100]."""
    if state is None or state.state in INVALID_STATES:
        return None
    try:
        value = float(state.state)
    except (TypeError, ValueError):
        return None
    return min(max(value, 0.0), 100.0)


def _find_last_complete_cycle(
    charging_states: list[State],
) -> tuple[datetime | None, datetime | None]:
    """Return (start_ts, end_ts) of the most recent off→on→off cycle.

    Walks the chronological list of charging-state samples and locates
    the latest falling edge (charging → not charging), then walks back to
    the rising edge (not charging → charging) that opened the session. If
    both are found, return both timestamps. If no rising edge precedes the
    falling edge (e.g. history begins mid-charge), return `(None, end_ts)`
    — the baseline is still recoverable, just without a `last_session`.

    Returns `(None, None)` if no falling edge is found at all.
    """
    end_ts: datetime | None = None
    end_idx: int | None = None
    for i in range(len(charging_states) - 1, 0, -1):
        prev = charging_states[i - 1]
        curr = charging_states[i]
        if is_charging(prev) and not is_charging(curr):
            end_ts = curr.last_updated
            end_idx = i
            break
    if end_ts is None or end_idx is None:
        return None, None

    start_ts: datetime | None = None
    for j in range(end_idx - 1, 0, -1):
        prev = charging_states[j - 1]
        curr = charging_states[j]
        if not is_charging(prev) and is_charging(curr):
            start_ts = curr.last_updated
            break
    return start_ts, end_ts


async def async_backfill_tracker_from_recorder(
    hass: HomeAssistant,
    tracker: ChargeTracker,
    charging_entity: str,
    mileage_entity: str,
    soc_entity: str,
    days: int,
) -> None:
    """Adopt a historical charge-end as the tracker baseline (best-effort).

    Skips silently when:
    - The tracker already has a baseline (reload, not fresh install).
    - The recorder component is not loaded.
    - The recorder API raises (older HA, DB locked, etc.).
    - No off → on → off transition is visible in the requested window.
    - Mileage or SoC is not recorded around the charge-end timestamp.

    When a rising edge can be paired with the falling edge, also synthesises
    a `last_session` so the kWh-added / average-power sensors light up
    immediately.
    """
    if tracker.baseline is not None:
        return

    if "recorder" not in hass.config.components:
        _LOGGER.debug("Tracker backfill skipped: recorder not loaded")
        return

    start_time = dt_util.utcnow() - timedelta(days=days)

    try:
        from homeassistant.components.recorder import (  # noqa: PLC0415
            get_instance,
            history as rec_history,
        )

        def _fetch(entity_id: str) -> list[State]:
            states = rec_history.state_changes_during_period(
                hass,
                start_time,
                entity_id=entity_id,
                no_attributes=False,
            )
            return list(states.get(entity_id, []))

        instance = get_instance(hass)
        charging_states = await instance.async_add_executor_job(
            _fetch, charging_entity
        )
        mileage_states = await instance.async_add_executor_job(
            _fetch, mileage_entity
        )
        soc_states = await instance.async_add_executor_job(_fetch, soc_entity)
    except Exception:  # noqa: BLE001
        _LOGGER.debug(
            "Tracker backfill failed (recorder API unavailable or error)",
            exc_info=True,
        )
        return

    if not charging_states:
        return

    start_ts, end_ts = _find_last_complete_cycle(charging_states)
    if end_ts is None:
        return

    mileage_km = _parse_distance_km(_value_at(mileage_states, end_ts))
    soc_at_end = _parse_soc(_value_at(soc_states, end_ts))
    if mileage_km is None or soc_at_end is None:
        _LOGGER.debug(
            "Tracker backfill skipped: mileage=%s soc=%s at end_ts=%s",
            mileage_km,
            soc_at_end,
            end_ts.isoformat(),
        )
        return

    start_soc: float | None = None
    if start_ts is not None:
        start_soc = _parse_soc(_value_at(soc_states, start_ts))

    await tracker.async_backfill_baseline(
        mileage_km=mileage_km,
        soc_percent=soc_at_end,
        end_ts=end_ts,
        start_soc_percent=start_soc,
        start_ts=start_ts if start_soc is not None else None,
    )
