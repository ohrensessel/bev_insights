"""Tests for the `_local_week_start` helper.

The function returns the local-timezone Monday-00:00 of the week
containing `now_utc`, converted back to UTC. The user's HA timezone
determines the local week boundary, not server UTC.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from homeassistant.core import HomeAssistant
import pytest

from custom_components.bev_insights.sensor import _local_week_start


@pytest.mark.parametrize(
    ("now_utc", "expected_iso"),
    [
        # Wednesday 2026-05-13 12:00 UTC → Monday 2026-05-11 00:00 UTC
        (datetime(2026, 5, 13, 12, 0, tzinfo=UTC), "2026-05-11T00:00:00+00:00"),
        # Sunday 2026-05-17 23:30 UTC → Monday 2026-05-11 00:00 UTC (still that week)
        (datetime(2026, 5, 17, 23, 30, tzinfo=UTC), "2026-05-11T00:00:00+00:00"),
        # Monday at exactly 00:00 maps to itself.
        (datetime(2026, 5, 11, 0, 0, tzinfo=UTC), "2026-05-11T00:00:00+00:00"),
        # One second before Monday 00:00 → the previous Monday.
        (datetime(2026, 5, 10, 23, 59, 59, tzinfo=UTC), "2026-05-04T00:00:00+00:00"),
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
    now_utc = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)
    result = _local_week_start(now_utc, hass)
    assert result.isoformat() == "2026-05-10T15:00:00+00:00"


async def test_local_week_start_handles_dst_offset(hass: HomeAssistant) -> None:
    """Europe/Berlin is UTC+2 in May (CEST). Monday 00:00 local = Sun 22:00 UTC."""
    hass.config.time_zone = "Europe/Berlin"
    now_utc = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)
    result = _local_week_start(now_utc, hass)
    assert result.isoformat() == "2026-05-10T22:00:00+00:00"


async def test_local_week_start_is_at_most_seven_days_back(
    hass: HomeAssistant,
) -> None:
    hass.config.time_zone = "UTC"
    now_utc = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)
    result = _local_week_start(now_utc, hass)
    assert timedelta(0) <= now_utc - result < timedelta(days=7)


# --------------------------------------------------------------------------- #
# DST transitions                                                             #
# --------------------------------------------------------------------------- #
#
# A naive implementation that decomposed the local datetime and rebuilt it as
# `datetime(..., tzinfo=local_tz)` would silently lose the DST offset that was
# in effect at Monday 00:00 of the week. We assert against UTC ISO strings so
# any such off-by-one-hour bug is visible.


async def test_local_week_start_on_spring_forward_sunday(
    hass: HomeAssistant,
) -> None:
    """Spring-forward Sunday in Europe/Berlin: Sun 2026-03-29.

    At 01:59 local the clocks jump to 03:00 (CET → CEST). The week's Monday
    (2026-03-23) is firmly in CET (UTC+1), so Monday 00:00 local = Sunday
    2026-03-22 23:00 UTC — regardless of where on the changeover Sunday we
    sample from.
    """
    hass.config.time_zone = "Europe/Berlin"
    expected = "2026-03-22T23:00:00+00:00"
    # Before, during, and after the DST jump (which happens at 01:00 UTC).
    # Stay below 22:00 UTC on changeover Sunday — past that, local time has
    # already rolled into Monday (the next week).
    for now_utc in (
        datetime(2026, 3, 29, 0, 30, tzinfo=UTC),  # 01:30 local CET
        datetime(2026, 3, 29, 1, 30, tzinfo=UTC),  # 03:30 local CEST
        datetime(2026, 3, 29, 12, 0, tzinfo=UTC),  # 14:00 local CEST
        datetime(2026, 3, 29, 21, 30, tzinfo=UTC),  # 23:30 local CEST
    ):
        result = _local_week_start(now_utc, hass)
        assert result.isoformat() == expected, (
            f"DST spring-forward bug at {now_utc.isoformat()}: got {result.isoformat()}"
        )


async def test_local_week_start_on_fall_back_sunday(
    hass: HomeAssistant,
) -> None:
    """Fall-back Sunday in Europe/Berlin: Sun 2026-10-25.

    Clocks shift 03:00 → 02:00 local (CEST → CET). The week's Monday
    (2026-10-19) is in CEST (UTC+2), so Monday 00:00 local = Sunday
    2026-10-18 22:00 UTC — even though `now_utc` sits in CET territory by
    the end of the day.
    """
    hass.config.time_zone = "Europe/Berlin"
    expected = "2026-10-18T22:00:00+00:00"
    for now_utc in (
        datetime(2026, 10, 25, 0, 30, tzinfo=UTC),  # 02:30 local CEST
        datetime(2026, 10, 25, 1, 30, tzinfo=UTC),  # 02:30 local CET (ambiguous hr)
        datetime(2026, 10, 25, 12, 0, tzinfo=UTC),  # 13:00 local CET
        datetime(2026, 10, 25, 22, 30, tzinfo=UTC),  # 23:30 local CET
    ):
        result = _local_week_start(now_utc, hass)
        assert result.isoformat() == expected, (
            f"DST fall-back bug at {now_utc.isoformat()}: got {result.isoformat()}"
        )


async def test_local_week_start_across_gmt_bst_changeover_weeks(
    hass: HomeAssistant,
) -> None:
    """Week-start anchoring on either side of the GMT→BST changeover.

    Europe/London switches GMT→BST at 01:00 UTC on Sunday 2026-03-29. We
    sample the Tuesday *before* (still GMT, UTC+0) and the Tuesday *after*
    (now BST, UTC+1) and confirm each week anchors on its own local-midnight
    Monday — the pre-DST week's Monday stays at 00:00 UTC, the post-DST
    week's Monday shifts to 23:00 UTC the previous day.
    """
    hass.config.time_zone = "Europe/London"
    now_utc = datetime(2026, 3, 24, 12, 0, tzinfo=UTC)  # Tuesday post-DST? no, pre.
    # 2026-03-24 is Tuesday and still BEFORE the Sunday changeover, so
    # London is on GMT (UTC+0). Monday 00:00 local = Monday 00:00 UTC.
    result = _local_week_start(now_utc, hass)
    assert result.isoformat() == "2026-03-23T00:00:00+00:00"

    # And one week later (Tue after DST) the same week-start logic in BST.
    now_utc = datetime(2026, 3, 31, 12, 0, tzinfo=UTC)
    # Monday 2026-03-30 00:00 local is BST (UTC+1) = Sunday 2026-03-29 23:00 UTC.
    result = _local_week_start(now_utc, hass)
    assert result.isoformat() == "2026-03-29T23:00:00+00:00"
