"""Tests for the `ChargeTracker` class."""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bev_insights.const import (
    BASELINE_MILEAGE_KM,
    BASELINE_SOC_PERCENT,
    BASELINE_TIMESTAMP,
    DOMAIN,
    SESSION_END_SOC_PERCENT,
    SESSION_LOG_MAX,
    SESSION_START_SOC_PERCENT,
    signal_baseline_updated,
)
from custom_components.bev_insights.tracker import ChargeTracker

CHARGING = "binary_sensor.car_charging"
MILEAGE = "sensor.car_mileage"
SOC = "sensor.car_soc"


def _entry() -> MockConfigEntry:
    return MockConfigEntry(domain=DOMAIN, data={}, entry_id="tracker_test")


async def _make_tracker(hass: HomeAssistant) -> ChargeTracker:
    tracker = ChargeTracker(
        hass,
        _entry(),
        charging_entity=CHARGING,
        mileage_entity=MILEAGE,
        soc_entity=SOC,
    )
    await tracker.async_load()
    tracker.async_start()
    return tracker


async def test_baseline_is_none_when_never_charged(hass: HomeAssistant) -> None:
    tracker = await _make_tracker(hass)
    assert tracker.baseline is None
    await tracker.async_stop()


async def test_charge_end_captures_baseline(hass: HomeAssistant) -> None:
    hass.states.async_set(MILEAGE, "12345.6")
    hass.states.async_set(SOC, "82.0")
    hass.states.async_set(CHARGING, "on")
    tracker = await _make_tracker(hass)
    await hass.async_block_till_done()

    # Trailing edge: charging → off.
    hass.states.async_set(CHARGING, "off")
    await hass.async_block_till_done()

    assert tracker.baseline is not None
    assert tracker.baseline[BASELINE_MILEAGE_KM] == 12345.6
    assert tracker.baseline[BASELINE_SOC_PERCENT] == 82.0
    assert BASELINE_TIMESTAMP in tracker.baseline
    await tracker.async_stop()


async def test_not_charging_to_not_charging_does_not_capture(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set(MILEAGE, "1000")
    hass.states.async_set(SOC, "50")
    hass.states.async_set(CHARGING, "off")
    tracker = await _make_tracker(hass)
    await hass.async_block_till_done()

    hass.states.async_set(CHARGING, "off")  # no-op transition
    await hass.async_block_till_done()
    assert tracker.baseline is None
    await tracker.async_stop()


async def test_charging_to_charging_does_not_capture(hass: HomeAssistant) -> None:
    """An intermediate 'charging' → 'charging' state echo must not fire."""
    hass.states.async_set(MILEAGE, "1000")
    hass.states.async_set(SOC, "50")
    hass.states.async_set(CHARGING, "on")
    tracker = await _make_tracker(hass)
    await hass.async_block_till_done()

    hass.states.async_set(CHARGING, "charging")
    await hass.async_block_till_done()
    assert tracker.baseline is None
    await tracker.async_stop()


async def test_charge_end_with_missing_sources_does_not_capture(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set(CHARGING, "on")
    tracker = await _make_tracker(hass)
    # mileage + soc states never set.

    hass.states.async_set(CHARGING, "off")
    await hass.async_block_till_done()
    assert tracker.baseline is None
    await tracker.async_stop()


async def test_full_cycle_records_last_session(hass: HomeAssistant) -> None:
    """off → on (start) → on → off (end) populates last_session."""
    hass.states.async_set(MILEAGE, "10000")
    hass.states.async_set(SOC, "40")
    hass.states.async_set(CHARGING, "off")
    tracker = await _make_tracker(hass)
    await hass.async_block_till_done()

    # Rising edge — captures start SoC.
    hass.states.async_set(CHARGING, "on")
    await hass.async_block_till_done()

    # During the session the SoC climbs.
    hass.states.async_set(SOC, "85")
    await hass.async_block_till_done()

    # Falling edge — captures end + finalises session.
    hass.states.async_set(CHARGING, "off")
    await hass.async_block_till_done()

    assert tracker.last_session is not None
    assert tracker.last_session[SESSION_START_SOC_PERCENT] == 40.0
    assert tracker.last_session[SESSION_END_SOC_PERCENT] == 85.0
    await tracker.async_stop()


async def test_falling_edge_without_rising_does_not_record_session(
    hass: HomeAssistant,
) -> None:
    """HA restart mid-charge: only the falling edge is observed → no session."""
    hass.states.async_set(MILEAGE, "10000")
    hass.states.async_set(SOC, "80")
    hass.states.async_set(CHARGING, "on")  # already on at tracker start
    tracker = await _make_tracker(hass)
    await hass.async_block_till_done()
    assert tracker.last_session is None

    hass.states.async_set(CHARGING, "off")
    await hass.async_block_till_done()

    assert tracker.baseline is not None  # baseline still captured
    assert tracker.last_session is None  # but no completed-session info
    await tracker.async_stop()


async def test_last_session_persists_across_reloads(hass: HomeAssistant) -> None:
    entry = _entry()
    hass.states.async_set(MILEAGE, "10000")
    hass.states.async_set(SOC, "30")
    hass.states.async_set(CHARGING, "off")

    tracker_a = ChargeTracker(
        hass,
        entry,
        charging_entity=CHARGING,
        mileage_entity=MILEAGE,
        soc_entity=SOC,
    )
    await tracker_a.async_load()
    tracker_a.async_start()
    hass.states.async_set(CHARGING, "on")
    await hass.async_block_till_done()
    hass.states.async_set(SOC, "90")
    await hass.async_block_till_done()
    hass.states.async_set(CHARGING, "off")
    await hass.async_block_till_done()
    assert tracker_a.last_session is not None
    await tracker_a.async_stop()

    tracker_b = ChargeTracker(
        hass,
        entry,
        charging_entity=CHARGING,
        mileage_entity=MILEAGE,
        soc_entity=SOC,
    )
    await tracker_b.async_load()
    assert tracker_b.last_session is not None
    assert tracker_b.last_session[SESSION_START_SOC_PERCENT] == 30.0
    assert tracker_b.last_session[SESSION_END_SOC_PERCENT] == 90.0


async def test_charge_end_fires_dispatcher_signal(hass: HomeAssistant) -> None:
    """Subscribers to `signal_baseline_updated` see exactly one callback per
    completed charge end.
    """
    entry = _entry()
    received: list[int] = []
    unsub = async_dispatcher_connect(
        hass, signal_baseline_updated(entry.entry_id), lambda: received.append(1)
    )

    hass.states.async_set(MILEAGE, "1000")
    hass.states.async_set(SOC, "50")
    hass.states.async_set(CHARGING, "on")
    tracker = ChargeTracker(
        hass,
        entry,
        charging_entity=CHARGING,
        mileage_entity=MILEAGE,
        soc_entity=SOC,
    )
    await tracker.async_load()
    tracker.async_start()
    await hass.async_block_till_done()
    assert received == []  # baseline not yet captured

    hass.states.async_set(CHARGING, "off")
    await hass.async_block_till_done()
    assert len(received) == 1
    unsub()
    await tracker.async_stop()


async def test_charge_start_with_missing_soc_skips_pending_start(
    hass: HomeAssistant,
) -> None:
    """If SoC is unavailable at the rising edge the session is dropped, so the
    eventual falling edge captures a baseline but no `last_session`."""
    entry = _entry()
    hass.states.async_set(MILEAGE, "1000")
    # SoC entity intentionally never set at start of session.
    hass.states.async_set(CHARGING, "off")
    tracker = ChargeTracker(
        hass,
        entry,
        charging_entity=CHARGING,
        mileage_entity=MILEAGE,
        soc_entity=SOC,
    )
    await tracker.async_load()
    tracker.async_start()
    await hass.async_block_till_done()

    hass.states.async_set(CHARGING, "on")  # rising edge — SoC missing
    await hass.async_block_till_done()
    assert tracker._pending_start is None

    # Now SoC becomes available and charging ends — baseline captures, but
    # since pending_start was never set, no last_session is finalised.
    hass.states.async_set(SOC, "90")
    await hass.async_block_till_done()
    hass.states.async_set(CHARGING, "off")
    await hass.async_block_till_done()

    assert tracker.baseline is not None
    assert tracker.last_session is None
    await tracker.async_stop()


# --------------------------------------------------------------------------- #
# Storage corruption: tolerate malformed on-disk payloads                     #
# --------------------------------------------------------------------------- #


def _seed_charge_tracker_storage(
    hass_storage: dict, entry_id: str, data: dict
) -> None:
    """Inject a ChargeTracker Store payload directly into the mock dict."""
    key = f"bev_insights.charge_tracker.{entry_id}"
    hass_storage[key] = {
        "version": 1,
        "minor_version": 1,
        "key": key,
        "data": data,
    }


async def test_charge_tracker_load_ignores_non_dict_payload(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    """A non-dict top-level payload (e.g. a string from a corrupted write)
    must not crash load — it's silently treated as 'no baseline'.
    """
    entry = _entry()
    key = f"bev_insights.charge_tracker.{entry.entry_id}"
    hass_storage[key] = {
        "version": 1,
        "minor_version": 1,
        "key": key,
        "data": "wat",
    }
    tracker = ChargeTracker(
        hass, entry, charging_entity=CHARGING, mileage_entity=MILEAGE, soc_entity=SOC
    )
    await tracker.async_load()
    assert tracker.baseline is None
    assert tracker.last_session is None


async def test_charge_tracker_load_ignores_non_dict_last_session(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    """`last_session` that isn't a dict (e.g. a list from a bad migration) is ignored."""
    entry = _entry()
    _seed_charge_tracker_storage(
        hass_storage,
        entry.entry_id,
        {
            BASELINE_MILEAGE_KM: 12345.0,
            BASELINE_SOC_PERCENT: 80.0,
            "timestamp": "2026-05-01T12:00:00+00:00",
            "last_session": ["not", "a", "dict"],
        },
    )
    tracker = ChargeTracker(
        hass, entry, charging_entity=CHARGING, mileage_entity=MILEAGE, soc_entity=SOC
    )
    await tracker.async_load()
    assert tracker.baseline is not None
    assert tracker.baseline[BASELINE_MILEAGE_KM] == 12345.0
    # The corrupt last_session was rejected; tracker continues without it.
    assert tracker.last_session is None


async def test_charge_tracker_load_filters_non_dict_session_log_entries(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    """Session-log items that aren't dicts (e.g. strings from a downgrade)
    are skipped; valid items still load."""
    entry = _entry()
    _seed_charge_tracker_storage(
        hass_storage,
        entry.entry_id,
        {
            "session_log": [
                "garbage_string",
                {"start_soc_percent": 30.0, "end_soc_percent": 80.0},
                None,
                42,
                {"start_soc_percent": 40.0, "end_soc_percent": 90.0},
            ]
        },
    )
    tracker = ChargeTracker(
        hass, entry, charging_entity=CHARGING, mileage_entity=MILEAGE, soc_entity=SOC
    )
    await tracker.async_load()
    log = tracker.session_log
    assert len(log) == 2
    assert log[0]["start_soc_percent"] == 30.0
    assert log[1]["start_soc_percent"] == 40.0


async def test_charge_tracker_load_accepts_baseline_with_missing_optional_keys(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    """A baseline payload that only has mileage_km loads with None for the
    optional fields rather than crashing on a KeyError."""
    entry = _entry()
    _seed_charge_tracker_storage(
        hass_storage,
        entry.entry_id,
        {BASELINE_MILEAGE_KM: 9999.0},  # SoC + timestamp absent
    )
    tracker = ChargeTracker(
        hass, entry, charging_entity=CHARGING, mileage_entity=MILEAGE, soc_entity=SOC
    )
    await tracker.async_load()
    assert tracker.baseline is not None
    assert tracker.baseline[BASELINE_MILEAGE_KM] == 9999.0
    assert tracker.baseline[BASELINE_SOC_PERCENT] is None
    assert tracker.baseline[BASELINE_TIMESTAMP] is None


async def test_charge_tracker_load_truncates_session_log_to_max(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    """An on-disk session log larger than SESSION_LOG_MAX keeps only the tail."""
    entry = _entry()
    too_many = [
        {"start_soc_percent": float(i), "end_soc_percent": float(i) + 10}
        for i in range(SESSION_LOG_MAX + 5)
    ]
    _seed_charge_tracker_storage(
        hass_storage, entry.entry_id, {"session_log": too_many}
    )
    tracker = ChargeTracker(
        hass, entry, charging_entity=CHARGING, mileage_entity=MILEAGE, soc_entity=SOC
    )
    await tracker.async_load()
    log = tracker.session_log
    assert len(log) == SESSION_LOG_MAX
    # We kept the *tail* — the latest sessions.
    assert log[-1]["start_soc_percent"] == float(SESSION_LOG_MAX + 4)


# --------------------------------------------------------------------------- #
# Lifecycle edge cases                                                        #
# --------------------------------------------------------------------------- #


async def test_async_stop_without_start_is_noop(hass: HomeAssistant) -> None:
    """Stopping a tracker that was never started must not raise."""
    tracker = ChargeTracker(
        hass, _entry(), charging_entity=CHARGING, mileage_entity=MILEAGE, soc_entity=SOC
    )
    await tracker.async_load()
    await tracker.async_stop()  # _unsub is None — should be a silent no-op.
    assert tracker.baseline is None


async def test_baseline_persists_across_reloads(hass: HomeAssistant) -> None:
    """A second tracker over the same entry sees the persisted baseline."""
    entry = _entry()
    hass.states.async_set(MILEAGE, "1000")
    hass.states.async_set(SOC, "60")
    hass.states.async_set(CHARGING, "on")

    tracker_a = ChargeTracker(
        hass,
        entry,
        charging_entity=CHARGING,
        mileage_entity=MILEAGE,
        soc_entity=SOC,
    )
    await tracker_a.async_load()
    tracker_a.async_start()
    hass.states.async_set(CHARGING, "off")
    await hass.async_block_till_done()
    assert tracker_a.baseline is not None
    await tracker_a.async_stop()

    # Recreate over same entry_id → should restore baseline from disk.
    tracker_b = ChargeTracker(
        hass,
        entry,
        charging_entity=CHARGING,
        mileage_entity=MILEAGE,
        soc_entity=SOC,
    )
    await tracker_b.async_load()
    assert tracker_b.baseline is not None
    assert tracker_b.baseline[BASELINE_MILEAGE_KM] == 1000.0
    assert tracker_b.baseline[BASELINE_SOC_PERCENT] == 60.0
