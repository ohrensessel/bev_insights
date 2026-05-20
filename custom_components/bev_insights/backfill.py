"""Recorder-based history backfill for EntityHistory on first install.

When the SoC or mileage history deque is empty (fresh install, or storage
cleared), this module queries HA's built-in recorder for the last N days of
state changes and primes the deque.  Everything is wrapped in try/except so
the integration loads normally even if the recorder is unavailable, on an
older HA version with a different API, or if the entity has no history.
"""
from __future__ import annotations

from datetime import timedelta
import logging
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

if TYPE_CHECKING:
    from .tracker import EntityHistory

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

        def _fetch():
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
