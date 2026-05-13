"""Tests for the `_local_week_start` helper.

The function returns the local-timezone Monday-00:00 of the week
containing `now_utc`, converted back to UTC. The user's HA timezone
determines the local week boundary, not server UTC.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from homeassistant.core import HomeAssistant

from custom_components.myskoda_insights.sensor import _local_week_start


@pytest.mark.parametrize(
    ("now_utc", "expected_iso"),
    [
        # Wednesday 2026-05-13 12:00 UTC → Monday 2026-05-11 00:00 UTC
        (datetime(2026, 5, 13, 12, 0, tzinfo=timezone.utc), "2026-05-11T00:00:00+00:00"),
        # Sunday 2026-05-17 23:30 UTC → Monday 2026-05-11 00:00 UTC (still that week)
        (datetime(2026, 5, 17, 23, 30, tzinfo=timezone.utc), "2026-05-11T00:00:00+00:00"),
        # Monday at exactly 00:00 maps to itself.
        (datetime(2026, 5, 11, 0, 0, tzinfo=timezone.utc), "2026-05-11T00:00:00+00:00"),
        # One second before Monday 00:00 → the previous Monday.
        (datetime(2026, 5, 10, 23, 59, 59, tzinfo=timezone.utc), "2026-05-04T00:00:00+00:00"),
    ],
)
async def test_local_week_start_utc(
    hass: HomeAssistant, now_utc: datetime, expected_iso: str
) -> None:
    hass.config.time_zone = "UTC"
    result = _local_week_start(now_utc, hass)
    assert result.isoformat() == expected_iso


async def test_local_week_start_respects_timezone(hass: HomeAssistant) -> None:
    """A timezone east of UTC moves the Monday boundary earlier in UTC.

    In Asia/Tokyo (UTC+9), Monday 00:00 local = Sunday 15:00 UTC.
    For now_utc = Wednesday 2026-05-13 12:00 UTC (Wed 21:00 in Tokyo),
    we expect Sun 2026-05-10 15:00 UTC.
    """
    hass.config.time_zone = "Asia/Tokyo"
    now_utc = datetime(2026, 5, 13, 12, 0, tzinfo=timezone.utc)
    result = _local_week_start(now_utc, hass)
    assert result.isoformat() == "2026-05-10T15:00:00+00:00"


async def test_local_week_start_handles_dst_offset(hass: HomeAssistant) -> None:
    """Europe/Berlin is UTC+2 in May (CEST). Monday 00:00 local = Sun 22:00 UTC."""
    hass.config.time_zone = "Europe/Berlin"
    now_utc = datetime(2026, 5, 13, 12, 0, tzinfo=timezone.utc)
    result = _local_week_start(now_utc, hass)
    assert result.isoformat() == "2026-05-10T22:00:00+00:00"


async def test_local_week_start_is_at_most_seven_days_back(
    hass: HomeAssistant,
) -> None:
    hass.config.time_zone = "UTC"
    now_utc = datetime(2026, 5, 13, 12, 0, tzinfo=timezone.utc)
    result = _local_week_start(now_utc, hass)
    assert timedelta(0) <= now_utc - result < timedelta(days=7)
