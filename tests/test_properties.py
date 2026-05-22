"""Property-based tests for pure formula helpers.

These complement the unit tests with random inputs: each `@given` test
asserts an invariant that should hold for *every* combination Hypothesis
picks, not only the cases the hand-written tests happened to cover.

Scoped to pure functions so the tests stay fast and don't need a `hass`
fixture (Hypothesis + async + the HA test loop is awkward to integrate).
"""
from __future__ import annotations

from hypothesis import assume, given, strategies as st

from custom_components.bev_insights.const import (
    UNIT_VARIANT_KM_PER_KWH,
    UNIT_VARIANT_KWH_PER_100KM,
)
from custom_components.bev_insights.sensor import _efficiency_value

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
