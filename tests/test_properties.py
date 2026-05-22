"""Property-based tests for formula helpers.

These complement the unit tests with random inputs: each `@given` test
asserts an invariant that should hold for *every* combination Hypothesis
picks, not only the cases the hand-written tests happened to cover.

The first block covers pure functions; the second uses the `hass`
fixture with `HealthCheck.function_scoped_fixture` suppressed because
every example fully overwrites the entity state it reads, so fixture
re-use is benign here.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from homeassistant.core import HomeAssistant
from hypothesis import HealthCheck, assume, given, settings, strategies as st

from custom_components.bev_insights.const import (
    BASELINE_MILEAGE_KM,
    BASELINE_SOC_PERCENT,
    UNIT_VARIANT_KM_PER_KWH,
    UNIT_VARIANT_KWH_PER_100KM,
)
from custom_components.bev_insights.sensor import _efficiency_value, _post_charge_window
from custom_components.bev_insights.util import read_distance_km, read_float

# Physically plausible BEV ranges.
# - SoC ≥ 1 % is the typical sensor resolution; below that the rounded
#   kWh/100 km output can underflow to 0.00, which is a known harmless
#   edge case for absurd input combos (e.g. 200 km on 0.01 kWh).
# - Capacity ≥ 20 kWh covers anything from a city EV upward.
# - Distance kept under 1500 km so the realistic-rate bound holds.
SOC = st.floats(
    min_value=1.0, max_value=100.0, allow_nan=False, allow_infinity=False
)
DISTANCE = st.floats(
    min_value=1.0, max_value=1500.0, allow_nan=False, allow_infinity=False
)
CAPACITY = st.floats(
    min_value=20.0, max_value=200.0, allow_nan=False, allow_infinity=False
)


# --------------------------------------------------------------------------- #
# _efficiency_value: positivity                                               #
# --------------------------------------------------------------------------- #


@given(soc=SOC, distance=DISTANCE, capacity=CAPACITY)
def test_efficiency_kwh_per_100km_is_positive(
    soc: float, distance: float, capacity: float
) -> None:
    value = _efficiency_value(
        capacity_kwh=capacity,
        soc_percent=soc,
        distance_km=distance,
        unit_variant=UNIT_VARIANT_KWH_PER_100KM,
    )
    assert value is not None
    assert value > 0.0


@given(soc=SOC, distance=DISTANCE, capacity=CAPACITY)
def test_efficiency_km_per_kwh_is_positive(
    soc: float, distance: float, capacity: float
) -> None:
    value = _efficiency_value(
        capacity_kwh=capacity,
        soc_percent=soc,
        distance_km=distance,
        unit_variant=UNIT_VARIANT_KM_PER_KWH,
    )
    assert value is not None
    assert value > 0.0


# --------------------------------------------------------------------------- #
# _efficiency_value: reciprocal property                                      #
# --------------------------------------------------------------------------- #


@given(soc=SOC, distance=DISTANCE, capacity=CAPACITY)
def test_efficiency_variants_are_reciprocal(
    soc: float, distance: float, capacity: float
) -> None:
    """kWh/100 km × km/kWh must always be ~100 — they're the same rate."""
    kwh_100 = _efficiency_value(
        capacity_kwh=capacity,
        soc_percent=soc,
        distance_km=distance,
        unit_variant=UNIT_VARIANT_KWH_PER_100KM,
    )
    km_kwh = _efficiency_value(
        capacity_kwh=capacity,
        soc_percent=soc,
        distance_km=distance,
        unit_variant=UNIT_VARIANT_KM_PER_KWH,
    )
    assert kwh_100 is not None and km_kwh is not None
    # Each variant is rounded (2 / 3 decimals); when either rounded
    # value is very small the rounding floor dominates the product's
    # relative error. Restrict the property to physically plausible BEV
    # efficiencies — i.e. both values ≥ 1 — where the reciprocal holds
    # within ~1 %.
    assume(kwh_100 >= 1.0 and km_kwh >= 1.0)
    product = kwh_100 * km_kwh
    assert abs(product - 100.0) / 100.0 < 0.02


# --------------------------------------------------------------------------- #
# _efficiency_value: scaling invariant                                        #
# --------------------------------------------------------------------------- #


@given(
    soc=st.floats(
        min_value=1.0, max_value=50.0, allow_nan=False, allow_infinity=False
    ),
    distance=DISTANCE,
    capacity=CAPACITY,
    scale=st.floats(
        min_value=0.5, max_value=2.0, allow_nan=False, allow_infinity=False
    ),
)
def test_efficiency_is_invariant_under_proportional_scaling(
    soc: float, distance: float, capacity: float, scale: float
) -> None:
    """Efficiency is a rate: scaling SoC and distance together doesn't change it.

    Constrain SoC × scale ≤ 100 so we don't hit the function's clamp.
    """
    scaled_soc = soc * scale
    if scaled_soc > 100.0:
        return
    base = _efficiency_value(
        capacity_kwh=capacity,
        soc_percent=soc,
        distance_km=distance,
        unit_variant=UNIT_VARIANT_KWH_PER_100KM,
    )
    scaled = _efficiency_value(
        capacity_kwh=capacity,
        soc_percent=scaled_soc,
        distance_km=distance * scale,
        unit_variant=UNIT_VARIANT_KWH_PER_100KM,
    )
    assert base is not None and scaled is not None
    # Same rounding caveat as the reciprocal test — relative tolerance.
    assert abs(base - scaled) / base < 0.02


# --------------------------------------------------------------------------- #
# _efficiency_value: SoC clamped at 100                                       #
# --------------------------------------------------------------------------- #


@given(
    distance=DISTANCE,
    capacity=CAPACITY,
    excess=st.floats(
        min_value=0.0, max_value=1000.0, allow_nan=False, allow_infinity=False
    ),
)
def test_efficiency_clamps_soc_above_100(
    distance: float, capacity: float, excess: float
) -> None:
    """SoC values above 100 should yield the same result as exactly 100."""
    at_100 = _efficiency_value(
        capacity_kwh=capacity,
        soc_percent=100.0,
        distance_km=distance,
        unit_variant=UNIT_VARIANT_KWH_PER_100KM,
    )
    above = _efficiency_value(
        capacity_kwh=capacity,
        soc_percent=100.0 + excess,
        distance_km=distance,
        unit_variant=UNIT_VARIANT_KWH_PER_100KM,
    )
    assert at_100 == above


# --------------------------------------------------------------------------- #
# _efficiency_value: invalid-input rejection                                  #
# --------------------------------------------------------------------------- #


@given(
    bad=st.one_of(
        st.none(),
        st.floats(
            max_value=0.0, allow_nan=False, allow_infinity=False
        ),  # 0 and negative
    )
)
def test_efficiency_returns_none_for_nonpositive_soc(bad: float | None) -> None:
    value = _efficiency_value(
        capacity_kwh=70.0,
        soc_percent=bad,
        distance_km=100.0,
        unit_variant=UNIT_VARIANT_KWH_PER_100KM,
    )
    assert value is None


@given(
    bad=st.one_of(
        st.none(),
        st.floats(max_value=0.0, allow_nan=False, allow_infinity=False),
    )
)
def test_efficiency_returns_none_for_nonpositive_distance(
    bad: float | None,
) -> None:
    value = _efficiency_value(
        capacity_kwh=70.0,
        soc_percent=50.0,
        distance_km=bad,
        unit_variant=UNIT_VARIANT_KWH_PER_100KM,
    )
    assert value is None


@given(
    bad=st.floats(max_value=0.0, allow_nan=False, allow_infinity=False),
)
def test_efficiency_returns_none_for_nonpositive_capacity(bad: float) -> None:
    value = _efficiency_value(
        capacity_kwh=bad,
        soc_percent=50.0,
        distance_km=100.0,
        unit_variant=UNIT_VARIANT_KWH_PER_100KM,
    )
    assert value is None


# --------------------------------------------------------------------------- #
# util.read_float / read_distance_km: round-trip through hass.states          #
#                                                                             #
# These tests touch `hass.states`, so they need the function-scoped `hass`    #
# fixture; the health check is suppressed because each example overwrites     #
# state cleanly (no accumulation) — see module docstring.                     #
# --------------------------------------------------------------------------- #


_SUPPRESS = settings(suppress_health_check=[HealthCheck.function_scoped_fixture])


def _looks_like_float(s: str) -> bool:
    """Helper for the garbage-string strategy filter."""
    try:
        float(s)
    except ValueError:
        return False
    return True


@_SUPPRESS
@given(value=st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False))
async def test_read_float_roundtrip(hass: HomeAssistant, value: float) -> None:
    """For any float, `str(x) → hass.states → read_float → x`."""
    hass.states.async_set("sensor.x", repr(value))
    assert read_float(hass, "sensor.x") == value


@_SUPPRESS
@given(
    garbage=st.text(min_size=1, max_size=20).filter(
        lambda s: not _looks_like_float(s)
    )
)
async def test_read_float_returns_none_for_garbage(
    hass: HomeAssistant, garbage: str
) -> None:
    """Any string that doesn't parse as a float yields None."""
    hass.states.async_set("sensor.x", garbage)
    assert read_float(hass, "sensor.x") is None


@_SUPPRESS
@given(
    miles=st.floats(min_value=0.0, max_value=1e5, allow_nan=False, allow_infinity=False)
)
async def test_read_distance_km_miles_conversion(
    hass: HomeAssistant, miles: float
) -> None:
    """Setting a `mi` reading round-trips through the 1.609344 factor."""
    hass.states.async_set(
        "sensor.d", repr(miles), {"unit_of_measurement": "mi"}
    )
    result = read_distance_km(hass, "sensor.d")
    assert result is not None
    assert abs(result - miles * 1.609344) < 1e-6


@_SUPPRESS
@given(
    meters=st.floats(min_value=0.0, max_value=1e8, allow_nan=False, allow_infinity=False)
)
async def test_read_distance_km_meters_conversion(
    hass: HomeAssistant, meters: float
) -> None:
    """Setting an `m` reading divides by 1000."""
    hass.states.async_set(
        "sensor.d", repr(meters), {"unit_of_measurement": "m"}
    )
    result = read_distance_km(hass, "sensor.d")
    assert result is not None
    assert abs(result - meters * 0.001) < 1e-6


# --------------------------------------------------------------------------- #
# _post_charge_window: delta + threshold math                                 #
#                                                                             #
# Avoid the real ChargeTracker (storage I/O) and instead pass a tiny stub —   #
# the function only touches `tracker.is_charging` and `tracker.baseline`.     #
# --------------------------------------------------------------------------- #


def _stub_tracker(*, is_charging: bool, baseline: dict[str, Any] | None) -> Any:
    """Minimal duck-typed stand-in for ChargeTracker."""
    return SimpleNamespace(is_charging=is_charging, baseline=baseline)


@_SUPPRESS
@given(
    # Capped at 5e5 km (realistic odometer max). Larger values would lose
    # precision when subtracted from a float64 baseline of similar size.
    baseline_km=st.floats(
        min_value=0.0, max_value=5e5, allow_nan=False, allow_infinity=False
    ),
    distance=st.floats(
        min_value=21.0, max_value=2000.0, allow_nan=False, allow_infinity=False
    ),
    baseline_soc=st.floats(
        min_value=20.0, max_value=100.0, allow_nan=False, allow_infinity=False
    ),
    soc_drop=st.floats(
        min_value=3.0, max_value=20.0, allow_nan=False, allow_infinity=False
    ),
)
async def test_post_charge_window_returns_correct_deltas(
    hass: HomeAssistant,
    baseline_km: float,
    distance: float,
    baseline_soc: float,
    soc_drop: float,
) -> None:
    """When thresholds are cleared, the function returns (distance, soc_drop)."""
    current_km = baseline_km + distance
    current_soc = baseline_soc - soc_drop
    # Strategies are tuned so current_soc stays in [0, 100]; skip the few
    # examples that drift outside via the realistic clip.
    assume(0.0 <= current_soc <= 100.0)
    hass.states.async_set("sensor.mileage", repr(current_km), {"unit_of_measurement": "km"})
    hass.states.async_set("sensor.soc", repr(current_soc))

    tracker = _stub_tracker(
        is_charging=False,
        baseline={
            BASELINE_MILEAGE_KM: baseline_km,
            BASELINE_SOC_PERCENT: baseline_soc,
        },
    )
    result = _post_charge_window(
        tracker,
        hass,
        mileage_entity="sensor.mileage",
        soc_entity="sensor.soc",
        min_distance_km=20.0,
        min_soc_percent=2.0,
    )
    assert result is not None
    got_distance, got_soc = result
    # baseline_km is up to 5e5; subtraction error scales with magnitude.
    assert abs(got_distance - distance) < 1e-3
    assert abs(got_soc - soc_drop) < 1e-6


@_SUPPRESS
@given(
    baseline_km=st.floats(
        min_value=0.0, max_value=5e5, allow_nan=False, allow_infinity=False
    ),
    distance=st.floats(
        min_value=0.0, max_value=19.0, allow_nan=False, allow_infinity=False
    ),
)
async def test_post_charge_window_rejects_below_distance_floor(
    hass: HomeAssistant, baseline_km: float, distance: float
) -> None:
    """Distance under min_distance_km → None, regardless of SoC drop."""
    hass.states.async_set(
        "sensor.mileage", repr(baseline_km + distance), {"unit_of_measurement": "km"}
    )
    hass.states.async_set("sensor.soc", "50.0")

    tracker = _stub_tracker(
        is_charging=False,
        baseline={BASELINE_MILEAGE_KM: baseline_km, BASELINE_SOC_PERCENT: 80.0},
    )
    result = _post_charge_window(
        tracker,
        hass,
        mileage_entity="sensor.mileage",
        soc_entity="sensor.soc",
        min_distance_km=20.0,
        min_soc_percent=2.0,
    )
    assert result is None


@_SUPPRESS
@given(
    is_charging=st.booleans(),
    has_baseline=st.booleans(),
)
async def test_post_charge_window_short_circuits(
    hass: HomeAssistant, is_charging: bool, has_baseline: bool
) -> None:
    """is_charging → always None; missing baseline → always None."""
    # Set valid live state; the function should still bail out early.
    hass.states.async_set("sensor.mileage", "10100", {"unit_of_measurement": "km"})
    hass.states.async_set("sensor.soc", "50.0")

    baseline = (
        {BASELINE_MILEAGE_KM: 10000.0, BASELINE_SOC_PERCENT: 80.0}
        if has_baseline
        else None
    )
    tracker = _stub_tracker(is_charging=is_charging, baseline=baseline)
    result = _post_charge_window(
        tracker,
        hass,
        mileage_entity="sensor.mileage",
        soc_entity="sensor.soc",
        min_distance_km=20.0,
        min_soc_percent=2.0,
    )
    if is_charging or not has_baseline:
        assert result is None
    else:
        assert result is not None
