"""Pure formula helpers shared across the sensor modules.

These are split out so they can be unit/property-tested without
spinning up a ChargeTracker or any HA glue (most of them).
`_post_charge_window` is the exception — it takes `hass` and a tracker
— but lives here because the math it encodes is the same shape.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.bev_insights.const import (
    BASELINE_MILEAGE_KM,
    BASELINE_SOC_PERCENT,
    TEMPERATURE_BANDS,
    UNIT_KM_PER_KWH,
    UNIT_KWH_PER_100KM,
    UNIT_VARIANT_KM_PER_KWH,
)
from custom_components.bev_insights.tracker import ChargeTracker
from custom_components.bev_insights.util import read_distance_km, read_float


def _unit_variant_props(unit_variant: str) -> tuple[str, str, int]:
    """Return (HA unit string, icon, suggested precision) per unit variant."""
    if unit_variant == UNIT_VARIANT_KM_PER_KWH:
        return UNIT_KM_PER_KWH, "mdi:speedometer", 2
    return UNIT_KWH_PER_100KM, "mdi:lightning-bolt", 1


def _human_unit(unit_variant: str) -> str:
    """Friendly label used as the fallback entity name."""
    if unit_variant == UNIT_VARIANT_KM_PER_KWH:
        return "km/kWh"
    return "kWh/100 km"


def _efficiency_value(
    capacity_kwh: float,
    soc_percent: float | None,
    distance_km: float | None,
    unit_variant: str,
) -> float | None:
    """Compute one efficiency figure or return None for invalid inputs.

    `soc_percent` and `distance_km` describe a *consumption sample*:
      - For the car-prediction variant: current SoC and current range.
      - For the measured variant: SoC consumed and distance driven
        since the last charge end.

    The math is identical in both cases, which is why this helper is shared.
    """
    if (
        soc_percent is None
        or distance_km is None
        or soc_percent <= 0
        or distance_km <= 0
        or capacity_kwh <= 0
    ):
        return None

    soc_percent = min(soc_percent, 100.0)
    # Energy represented by `soc_percent` of the configured capacity.
    energy_kwh = capacity_kwh * soc_percent / 100.0

    if unit_variant == UNIT_VARIANT_KM_PER_KWH:
        return round(distance_km / energy_kwh, 3)
    # Default: kWh per 100 km
    return round(energy_kwh / distance_km * 100.0, 2)


def _post_charge_window(
    tracker: ChargeTracker,
    hass: HomeAssistant,
    mileage_entity: str,
    soc_entity: str,
    min_distance_km: float,
    min_soc_percent: float,
) -> tuple[float, float] | None:
    """Compute (distance_km, soc_consumed) since the last charge end.

    Returns `None` when the sensor should be unavailable. Encodes the
    full set of guards shared by `MeasuredFullRangeSensor` and
    `MeasuredEfficiencySensor`:

    1. The vehicle is currently charging — SoC is rising back toward the
       baseline and the ratio diverges.
    2. No charge session has ended yet (tracker has no baseline).
    3. The baseline lacks one of the required fields.
    4. The live source entities aren't reporting usable values.
    5. The post-charge drive hasn't crossed both threshold floors.
    """
    if tracker.is_charging:
        return None
    baseline = tracker.baseline
    if baseline is None:
        return None
    baseline_mileage = baseline.get(BASELINE_MILEAGE_KM)
    baseline_soc = baseline.get(BASELINE_SOC_PERCENT)
    if baseline_mileage is None or baseline_soc is None:
        return None
    current_mileage = read_distance_km(hass, mileage_entity)
    current_soc = read_float(hass, soc_entity)
    if current_mileage is None or current_soc is None:
        return None
    distance_km = current_mileage - baseline_mileage
    soc_consumed = baseline_soc - current_soc
    if distance_km < min_distance_km or soc_consumed < min_soc_percent:
        return None
    return distance_km, soc_consumed


def _local_week_start(now_utc: datetime, hass: HomeAssistant) -> datetime:
    """Return the local Monday 00:00 of the week containing `now_utc`, in UTC.

    Uses Home Assistant's configured time zone so the week boundary
    matches the user's locale, not server UTC.
    """
    local_tz = dt_util.get_time_zone(hass.config.time_zone) or dt_util.UTC
    local = now_utc.astimezone(local_tz)
    monday_local = local - timedelta(days=local.weekday())
    monday_local = monday_local.replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return monday_local.astimezone(dt_util.UTC)


def _window_cutoff(
    hass: HomeAssistant, window_key: str, now_utc: datetime
) -> datetime:
    """Return the cutoff timestamp for a named window."""
    if window_key == "this_week":
        return _local_week_start(now_utc, hass)
    # Default: rolling 7 days
    return now_utc - timedelta(days=7)


def _temperature_band(temp_c: float) -> str:
    """Return the band key (from `TEMPERATURE_BANDS`) a temperature falls in.

    Bands are half-open `[lower, upper)`; a None bound is open-ended. The
    bands tile the real line, so exactly one always matches.
    """
    for key, lower, upper in TEMPERATURE_BANDS:
        if (lower is None or temp_c >= lower) and (upper is None or temp_c < upper):
            return key
    # Bands are exhaustive; the last one is open-ended upward.
    return TEMPERATURE_BANDS[-1][0]


def _local_day_windows(
    start_utc: datetime, end_utc: datetime, hass: HomeAssistant
) -> list[tuple[datetime, datetime]]:
    """Split `[start_utc, end_utc)` into per-local-calendar-day UTC windows.

    Each returned `(day_start, day_end)` pair is clamped to the requested
    range, so the first and last days may be partial. Boundaries are local
    midnights converted back to UTC, so a day means the user's calendar day
    rather than a UTC day. Returns an empty list when `end_utc <= start_utc`.
    """
    if end_utc <= start_utc:
        return []
    local_tz = dt_util.get_time_zone(hass.config.time_zone) or dt_util.UTC
    windows: list[tuple[datetime, datetime]] = []
    cursor = start_utc
    while cursor < end_utc:
        local = cursor.astimezone(local_tz)
        next_local_midnight = (local + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        day_end = min(next_local_midnight.astimezone(dt_util.UTC), end_utc)
        windows.append((cursor, day_end))
        cursor = day_end
    return windows
