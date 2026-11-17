"""Unit tests for monitoring.anomaly_detector.

All tests inject the module's seams (fetch / post / sleep / now) and point
the state file at ``tmp_path`` — no test touches real time, real HTTP, or the
real filesystem outside tmp_path. Mirrors tests/test_recovery.py style.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from pv_inverter_master.monitoring import anomaly_detector as ad
from pv_inverter_master.monitoring.anomaly_detector import (
    DEAD_INVERTER,
    MODBUS_HANG,
    MULTI_CLIENT,
    RADIO_GAP,
    STALE_POLL,
    WEATHER_OR_SITE,
    DetectorConfig,
    DetectorState,
    ProductionRecord,
    RadioGapRecord,
    apply_cooldown,
    autofix_modbus_hang,
    build_location_map,
    classify_anomalies,
    load_state,
    run_detection,
    save_state,
    solar_elevation_deg,
)


# A test location: Munich-ish, 48.0N / 11.0E.
LAT, LON = 48.0, 11.0
# Solar noon at 11.0E is roughly 11:16 UTC (12:00 local minus 44 min for
# the +11° longitude). Pick a summer day so the sun is well up.
NOON_UTC = datetime(2026, 6, 21, 11, 16, 0, tzinfo=timezone.utc)
# Solar midnight ~ 23:16 UTC.
MIDNIGHT_UTC = datetime(2026, 6, 21, 23, 16, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _cfg(tmp_path: Path) -> DetectorConfig:
    cfg = DetectorConfig()
    cfg.state_path = tmp_path / "anomaly-state.json"
    return cfg


def _inverter(
    device_id: str,
    *,
    dtype: str = "sungrow",
    power_w: float = 100.0,
    connection_state: str = "connected",
    location_id: str = "loc1",
    # Default = fresh relative to NOON_UTC (the fixed test clock). The old
    # default (epoch 1e6, i.e. 1970) made every fixture implicitly STALE —
    # which encoded the pre-v8.3.4 bug where DEAD ignored data freshness.
    last_successful_poll_at: float | None = None,
    poll_interval: float = 30.0,
    last_poll_error: str | None = None,
    enabled: bool = True,
    source_stale: bool = False,
) -> dict[str, Any]:
    if last_successful_poll_at is None:
        last_successful_poll_at = NOON_UTC.timestamp()
    return {
        "id": device_id,
        "type": dtype,
        "enabled": enabled,
        "connection_state": connection_state,
        "power_w": power_w,
        "location_id": location_id,
        "poll_interval": poll_interval,
        "last_poll_at": last_successful_poll_at,
        "last_successful_poll_at": last_successful_poll_at,
        "last_poll_error": last_poll_error,
        "source_stale": source_stale,
    }


def _locations(*entries: dict[str, Any]) -> dict[str, Any]:
    return {"locations": list(entries)}


def _make_fetch(devices: dict[str, Any], locations: dict[str, Any]):
    """Return a fetch(url, timeout) that routes by endpoint."""

    def fetch(url: str, timeout: float):
        if url.endswith("/api/devices"):
            return devices
        if url.endswith("/api/locations"):
            return locations
        return None

    return fetch


# --------------------------------------------------------------------------
# solar_elevation_deg
# --------------------------------------------------------------------------


def test_solar_elevation_positive_at_noon():
    elev = solar_elevation_deg(LAT, LON, NOON_UTC)
    assert elev > 0


def test_solar_elevation_negative_at_midnight():
    elev = solar_elevation_deg(LAT, LON, MIDNIGHT_UTC)
    assert elev < 0


def test_solar_elevation_naive_datetime_treated_as_utc():
    aware = solar_elevation_deg(LAT, LON, NOON_UTC)
    naive = solar_elevation_deg(LAT, LON, NOON_UTC.replace(tzinfo=None))
    assert abs(aware - naive) < 1e-6


# --------------------------------------------------------------------------
# location map
# --------------------------------------------------------------------------


def test_build_location_map_joins_lat_lon():
    payload = _locations({"id": "loc1", "name": "Home", "lat": LAT, "lon": LON})
    m = build_location_map(payload)
    assert m["loc1"] == (LAT, LON)


def test_build_location_map_none_coords():
    payload = _locations({"id": "loc1", "name": "Home", "lat": None, "lon": None})
    m = build_location_map(payload)
    assert m["loc1"] == (None, None)


# --------------------------------------------------------------------------
# DEAD_INVERTER / WEATHER_OR_SITE / night gate
# --------------------------------------------------------------------------


def test_dead_inverter_flagged_with_producing_sibling_in_daylight(tmp_path):
    cfg = _cfg(tmp_path)
    devices = {
        "devices": [
            _inverter("dead", power_w=0.0),
            _inverter("alive", power_w=500.0),
        ]
    }
    loc_map = build_location_map(_locations({"id": "loc1", "lat": LAT, "lon": LON}))
    anomalies = classify_anomalies(devices, loc_map, cfg, NOON_UTC)
    types = {(a["device_id"], a["type"]) for a in anomalies}
    assert ("dead", DEAD_INVERTER) in types


def test_dead_inverter_not_flagged_at_night(tmp_path):
    cfg = _cfg(tmp_path)
    devices = {
        "devices": [
            _inverter("dead", power_w=0.0),
            _inverter("alive", power_w=500.0),
        ]
    }
    loc_map = build_location_map(_locations({"id": "loc1", "lat": LAT, "lon": LON}))
    anomalies = classify_anomalies(devices, loc_map, cfg, MIDNIGHT_UTC)
    dead = [a for a in anomalies if a["type"] in (DEAD_INVERTER, WEATHER_OR_SITE)]
    assert dead == []


def test_all_site_zero_in_daylight_is_weather_not_dead(tmp_path):
    cfg = _cfg(tmp_path)
    devices = {
        "devices": [
            _inverter("a", dtype="sungrow", power_w=0.0, last_successful_poll_at=NOON_UTC.timestamp()),
            _inverter("b", dtype="solaredge", power_w=0.0, last_successful_poll_at=NOON_UTC.timestamp()),
        ]
    }
    loc_map = build_location_map(_locations({"id": "loc1", "lat": LAT, "lon": LON}))
    anomalies = classify_anomalies(devices, loc_map, cfg, NOON_UTC)
    types = {a["type"] for a in anomalies}
    assert WEATHER_OR_SITE in types
    assert DEAD_INVERTER not in types


def test_dead_not_flagged_when_location_has_no_coords(tmp_path):
    cfg = _cfg(tmp_path)
    devices = {
        "devices": [
            _inverter("dead", power_w=0.0),
            _inverter("alive", power_w=500.0),
        ]
    }
    loc_map = build_location_map(_locations({"id": "loc1", "lat": None, "lon": None}))
    anomalies = classify_anomalies(devices, loc_map, cfg, NOON_UTC)
    dead = [a for a in anomalies if a["type"] in (DEAD_INVERTER, WEATHER_OR_SITE)]
    assert dead == []


# --------------------------------------------------------------------------
# STALE_POLL
# --------------------------------------------------------------------------


def test_stale_poll_disconnected(tmp_path):
    cfg = _cfg(tmp_path)
    devices = {"devices": [_inverter("d1", connection_state="disconnected")]}
    anomalies = classify_anomalies(devices, {}, cfg, NOON_UTC)
    assert any(a["type"] == STALE_POLL for a in anomalies)


def test_stale_poll_old_successful_poll(tmp_path):
    cfg = _cfg(tmp_path)
    # last_successful_poll_at far in the past relative to now_utc.
    now_utc = datetime.fromtimestamp(2_000_000.0, tz=timezone.utc)
    devices = {
        "devices": [
            _inverter("d1", last_successful_poll_at=1_000_000.0, poll_interval=30.0)
        ]
    }
    anomalies = classify_anomalies(devices, {}, cfg, now_utc)
    assert any(a["type"] == STALE_POLL for a in anomalies)


def test_stale_poll_with_error(tmp_path):
    cfg = _cfg(tmp_path)
    devices = {"devices": [_inverter("d1", last_poll_error="timeout reading")]}
    anomalies = classify_anomalies(devices, {}, cfg, NOON_UTC)
    assert any(a["type"] == STALE_POLL for a in anomalies)


def test_healthy_device_not_stale(tmp_path):
    cfg = _cfg(tmp_path)
    now_utc = datetime.fromtimestamp(1_000_010.0, tz=timezone.utc)
    devices = {"devices": [_inverter("d1", last_successful_poll_at=1_000_000.0)]}
    anomalies = classify_anomalies(devices, {}, cfg, now_utc)
    assert not any(a["type"] == STALE_POLL for a in anomalies)


# --------------------------------------------------------------------------
# MODBUS_HANG classification
# --------------------------------------------------------------------------


def test_modbus_hang_sungrow_no_response(tmp_path):
    cfg = _cfg(tmp_path)
    devices = {
        "devices": [_inverter("sg", dtype="sungrow", connection_state="reconnecting", last_poll_error="No response from slave")]
    }
    anomalies = classify_anomalies(devices, {}, cfg, NOON_UTC)
    assert any(a["type"] == MODBUS_HANG for a in anomalies)


def test_modbus_hang_sungrow_io_error(tmp_path):
    cfg = _cfg(tmp_path)
    devices = {
        "devices": [_inverter("sg", dtype="sungrow", connection_state="reconnecting", last_poll_error="Modbus input/output error")]
    }
    anomalies = classify_anomalies(devices, {}, cfg, NOON_UTC)
    assert any(a["type"] == MODBUS_HANG for a in anomalies)


def test_modbus_hang_solaredge_no_response(tmp_path):
    # SE30K incident 2026-06-10: a SolarEdge SunSpec/Modbus server hang has
    # the same signature as a Sungrow hang and must classify as MODBUS_HANG
    # (critical), not just STALE_POLL (warning).
    cfg = _cfg(tmp_path)
    devices = {
        "devices": [
            _inverter("se", dtype="solaredge", connection_state="reconnecting", last_poll_error="No response from slave")
        ]
    }
    anomalies = classify_anomalies(devices, {}, cfg, NOON_UTC)
    assert any(a["type"] == MODBUS_HANG for a in anomalies)
    # The independent STALE_POLL rule still fires too (error is truthy).
    assert any(a["type"] == STALE_POLL for a in anomalies)


def test_modbus_hang_not_on_single_transient_timeout(tmp_path):
    """A lone Modbus timeout on an otherwise-connected, fresh device must NOT
    classify as MODBUS_HANG (single-sample guard against flapping criticals:
    the resolved->active transition in apply_cooldown bypasses the cooldown).
    It still surfaces as STALE_POLL/warning via the poll-error branch."""
    cfg = _cfg(tmp_path)
    now_utc = datetime.fromtimestamp(1_000_010.0, tz=timezone.utc)
    devices = {
        "devices": [
            _inverter("se", dtype="solaredge", connection_state="connected",
                      last_successful_poll_at=1_000_000.0,
                      last_poll_error="Modbus Error: [Input/Output] No response")
        ]
    }
    anomalies = classify_anomalies(devices, {}, cfg, now_utc)
    assert not any(a["type"] == MODBUS_HANG for a in anomalies)
    assert any(a["type"] == STALE_POLL for a in anomalies)


def test_modbus_hang_fires_at_night_when_corroborated(tmp_path):
    """A corroborated hang (non-connected + Modbus signature) classifies as
    MODBUS_HANG regardless of sun position — the healthy SE30K stays
    Modbus-reachable at night, so night_mode is never a benign nightly state
    on this fleet (verified against weeks of detector state history)."""
    cfg = _cfg(tmp_path)
    devices = {
        "devices": [
            _inverter("se", dtype="solaredge", connection_state="night_mode",
                      last_poll_error="Modbus Error: [Input/Output] No response")
        ]
    }
    anomalies = classify_anomalies(devices, {}, cfg, MIDNIGHT_UTC)
    assert any(a["type"] == MODBUS_HANG for a in anomalies)


def test_modbus_hang_wa_severity_is_error():
    # SE30K incident: a corroborated Modbus hang is a production outage and
    # must not page as the same soft "warn" as a generic stale poll.
    assert ad._alert_severity(MODBUS_HANG) == "error"
    assert ad._alert_severity(STALE_POLL) == "warn"


def test_supports_reboot_missing_device_is_false():
    assert ad._supports_reboot({"devices": []}, "ghost") is False
    assert ad._supports_reboot({}, "ghost") is False


def test_modbus_hang_not_for_non_modbus_types(tmp_path):
    cfg = _cfg(tmp_path)
    devices = {
        "devices": [
            _inverter("dtu", dtype="opendtu", last_poll_error="No response from slave")
        ]
    }
    anomalies = classify_anomalies(devices, {}, cfg, NOON_UTC)
    assert not any(a["type"] == MODBUS_HANG for a in anomalies)
    assert any(a["type"] == STALE_POLL for a in anomalies)


def test_stale_poll_detail_carries_staleness_age(tmp_path):
    # "connection_state=night_mode" alone is misleading (SE30K incident: a
    # 13h daylight hang read as a benign label) — the detail must carry how
    # long the device has been unreachable.
    cfg = _cfg(tmp_path)
    now_utc = datetime.fromtimestamp(1_050_000.0, tz=timezone.utc)
    devices = {
        "devices": [
            _inverter("se", dtype="solaredge", connection_state="night_mode",
                      last_successful_poll_at=1_000_000.0)
        ]
    }
    anomalies = classify_anomalies(devices, {}, cfg, now_utc)
    stale = [a for a in anomalies if a["type"] == STALE_POLL]
    assert stale
    assert "connection_state=night_mode" in stale[0]["detail"]
    assert "no successful poll for 50000s" in stale[0]["detail"]


# --------------------------------------------------------------------------
# Pseudo-row filtering
# --------------------------------------------------------------------------


def test_pseudo_rows_ignored(tmp_path):
    cfg = _cfg(tmp_path)
    devices = {
        "devices": [
            {"id": "venus", "type": "venus", "connection_state": "disconnected"},
            {"id": "virtual", "type": "virtual", "connection_state": "disconnected"},
            _inverter("real", connection_state="disconnected"),
        ]
    }
    anomalies = classify_anomalies(devices, {}, cfg, NOON_UTC)
    flagged_ids = {a["device_id"] for a in anomalies}
    assert flagged_ids == {"real"}


# --------------------------------------------------------------------------
# Cooldown dedupe + re-alert + recovery
# --------------------------------------------------------------------------


def test_cooldown_suppresses_repeat_within_window(tmp_path):
    cfg = _cfg(tmp_path)
    state = DetectorState()
    anomalies = [
        {"device_id": "d1", "type": STALE_POLL, "severity": "warning", "detail": "x", "location_id": ""}
    ]
    t0 = 1_000_000.0
    alerts1, _ = apply_cooldown(anomalies, state, cfg, t0)
    assert len(alerts1) == 1  # first time -> alert
    # Second pass shortly after -> within cooldown -> suppressed.
    alerts2, _ = apply_cooldown(anomalies, state, cfg, t0 + 60.0)
    assert alerts2 == []


def test_cooldown_realerts_after_window(tmp_path):
    cfg = _cfg(tmp_path)
    state = DetectorState()
    anomalies = [
        {"device_id": "d1", "type": STALE_POLL, "severity": "warning", "detail": "x", "location_id": ""}
    ]
    t0 = 1_000_000.0
    apply_cooldown(anomalies, state, cfg, t0)
    alerts, _ = apply_cooldown(anomalies, state, cfg, t0 + cfg.cooldown_s + 1.0)
    assert len(alerts) == 1


def test_recovery_transition_when_anomaly_clears(tmp_path):
    cfg = _cfg(tmp_path)
    state = DetectorState()
    anomalies = [
        {"device_id": "d1", "type": STALE_POLL, "severity": "warning", "detail": "x", "location_id": ""}
    ]
    t0 = 1_000_000.0
    apply_cooldown(anomalies, state, cfg, t0)
    # Next pass: device healthy -> no anomalies -> recovery notification.
    alerts, recoveries = apply_cooldown([], state, cfg, t0 + 100.0)
    assert alerts == []
    assert len(recoveries) == 1
    assert recoveries[0]["device_id"] == "d1"
    assert recoveries[0]["type"] == STALE_POLL
    assert state.entries[DetectorState.key("d1", STALE_POLL)].resolved is True


def test_resolved_to_active_realerts(tmp_path):
    cfg = _cfg(tmp_path)
    state = DetectorState()
    anomalies = [
        {"device_id": "d1", "type": STALE_POLL, "severity": "warning", "detail": "x", "location_id": ""}
    ]
    t0 = 1_000_000.0
    apply_cooldown(anomalies, state, cfg, t0)
    apply_cooldown([], state, cfg, t0 + 100.0)  # resolved
    # Re-appears -> immediate re-alert despite being within cooldown.
    alerts, _ = apply_cooldown(anomalies, state, cfg, t0 + 200.0)
    assert len(alerts) == 1


# --------------------------------------------------------------------------
# Auto-fix MODBUS_HANG
# --------------------------------------------------------------------------


def test_autofix_reboots_and_verifies_recovery(tmp_path):
    cfg = _cfg(tmp_path)
    state = DetectorState()
    reboot_calls: list[str] = []

    def post(base_url, device_id, timeout):
        reboot_calls.append(device_id)
        return True

    # After reboot, re-fetch shows the device recovered.
    recovered_devices = {
        "devices": [
            _inverter("sg", dtype="sungrow", connection_state="connected",
                      last_successful_poll_at=2_000_000.0, last_poll_error=None)
        ]
    }

    def fetch(url, timeout):
        return recovered_devices

    sleep_calls: list[float] = []
    pre_devices = {
        "devices": [
            _inverter("sg", dtype="sungrow", connection_state="connecting",
                      last_successful_poll_at=1_000_000.0,
                      last_poll_error="No response from slave")
        ]
    }
    anomaly = {"device_id": "sg", "type": MODBUS_HANG, "severity": "critical", "detail": "x", "location_id": ""}
    outcome = autofix_modbus_hang(
        anomaly, pre_devices, state, cfg,
        post=post, fetch=fetch, sleep=lambda s: sleep_calls.append(s),
        now=lambda: 1_500_000.0,
    )
    assert reboot_calls == ["sg"]
    assert outcome["rebooted"] is True
    assert outcome["recovered"] is True
    assert sleep_calls == [cfg.reboot_backoff_s]
    assert state.entries[DetectorState.key("sg", MODBUS_HANG)].autofix_count == 1


def test_autofix_respects_attempt_cap(tmp_path):
    cfg = _cfg(tmp_path)
    state = DetectorState()
    reboot_calls: list[str] = []

    def post(base_url, device_id, timeout):
        reboot_calls.append(device_id)
        return True

    def fetch(url, timeout):
        # Never recovers, so each attempt is a fresh reboot until capped.
        return {"devices": [_inverter("sg", dtype="sungrow", connection_state="connecting",
                                      last_poll_error="No response")]}

    anomaly = {"device_id": "sg", "type": MODBUS_HANG, "severity": "critical", "detail": "x", "location_id": ""}
    pre = {"devices": [_inverter("sg", dtype="sungrow", connection_state="connecting",
                                 last_poll_error="No response")]}
    t = 1_500_000.0
    # cap is 2 per window — third attempt within window must be capped.
    autofix_modbus_hang(anomaly, pre, state, cfg, post=post, fetch=fetch, sleep=lambda s: None, now=lambda: t)
    autofix_modbus_hang(anomaly, pre, state, cfg, post=post, fetch=fetch, sleep=lambda s: None, now=lambda: t + 10)
    capped = autofix_modbus_hang(anomaly, pre, state, cfg, post=post, fetch=fetch, sleep=lambda s: None, now=lambda: t + 20)
    assert len(reboot_calls) == 2  # only 2 reboots fired
    assert capped["capped"] is True
    assert capped["attempted"] is False


def test_autofix_window_resets_counter(tmp_path):
    cfg = _cfg(tmp_path)
    state = DetectorState()
    reboot_calls: list[str] = []

    def post(base_url, device_id, timeout):
        reboot_calls.append(device_id)
        return True

    def fetch(url, timeout):
        return {"devices": [_inverter("sg", dtype="sungrow", connection_state="connecting",
                                      last_poll_error="No response")]}

    anomaly = {"device_id": "sg", "type": MODBUS_HANG, "severity": "critical", "detail": "x", "location_id": ""}
    pre = {"devices": [_inverter("sg", dtype="sungrow", connection_state="connecting",
                                 last_poll_error="No response")]}
    t = 1_500_000.0
    autofix_modbus_hang(anomaly, pre, state, cfg, post=post, fetch=fetch, sleep=lambda s: None, now=lambda: t)
    autofix_modbus_hang(anomaly, pre, state, cfg, post=post, fetch=fetch, sleep=lambda s: None, now=lambda: t + 10)
    # Way past the window -> counter resets -> reboot fires again.
    later = t + cfg.autofix_window_s + 100
    out = autofix_modbus_hang(anomaly, pre, state, cfg, post=post, fetch=fetch, sleep=lambda s: None, now=lambda: later)
    assert out["attempted"] is True
    assert len(reboot_calls) == 3


def test_run_detection_no_reboot_for_unsupported_device(tmp_path):
    """A solaredge MODBUS_HANG must alert but NEVER POST a reboot (the
    solaredge plugin has no reboot(); supports_reboot=False in the payload)."""
    cfg = _cfg(tmp_path)
    state = DetectorState()
    reboot_calls: list[str] = []

    def post(base_url, device_id, timeout):
        reboot_calls.append(device_id)
        return True

    se = _inverter("se", dtype="solaredge", connection_state="night_mode",
                   last_poll_error="Modbus Error: [Input/Output] No response")
    se["supports_reboot"] = False
    devices = {"devices": [se]}
    locations = _locations({"id": "loc1", "lat": LAT, "lon": LON})
    result = run_detection(
        cfg, state,
        fetch=_make_fetch(devices, locations),
        post=post,
        sleep=lambda s: None,
        now=lambda: NOON_UTC.timestamp(),
    )
    assert any(a["type"] == MODBUS_HANG for a in result["anomalies"])
    assert any(a["type"] == MODBUS_HANG for a in result["alerts"])
    assert reboot_calls == []
    assert result["autofixes"] == []


def test_run_detection_reboot_still_fires_for_sungrow(tmp_path):
    """Regression guard: the supports_reboot gate must not break the existing
    sungrow auto-reboot (payloads with and without the explicit flag)."""
    for explicit_flag in (True, None):
        cfg = _cfg(tmp_path / f"flag-{explicit_flag}")
        state = DetectorState()
        reboot_calls: list[str] = []

        def post(base_url, device_id, timeout):
            reboot_calls.append(device_id)
            return True

        sg = _inverter("sg", dtype="sungrow", connection_state="connecting",
                       last_poll_error="No response from slave")
        if explicit_flag is not None:
            sg["supports_reboot"] = explicit_flag
        devices = {"devices": [sg]}
        locations = _locations({"id": "loc1", "lat": LAT, "lon": LON})
        result = run_detection(
            cfg, state,
            fetch=_make_fetch(devices, locations),
            post=post,
            sleep=lambda s: None,
            now=lambda: NOON_UTC.timestamp(),
        )
        assert reboot_calls == ["sg"], f"explicit_flag={explicit_flag}"
        assert len(result["autofixes"]) == 1


# --------------------------------------------------------------------------
# State file round-trip
# --------------------------------------------------------------------------


def test_state_roundtrip(tmp_path):
    path = tmp_path / "state.json"
    state = DetectorState()
    state.entries[DetectorState.key("d1", STALE_POLL)] = ad.StateEntry(
        first_seen=1.0, last_alerted=2.0, autofix_count=1
    )
    save_state(path, state)
    loaded = load_state(path)
    entry = loaded.entries[DetectorState.key("d1", STALE_POLL)]
    assert entry.first_seen == 1.0
    assert entry.last_alerted == 2.0
    assert entry.autofix_count == 1


def test_state_corrupt_starts_fresh(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not valid json")
    loaded = load_state(path)
    assert loaded.entries == {}


def test_state_missing_starts_fresh(tmp_path):
    loaded = load_state(tmp_path / "nope.json")
    assert loaded.entries == {}


# --------------------------------------------------------------------------
# run_detection orchestration + main()
# --------------------------------------------------------------------------


def test_run_detection_full_pass(tmp_path):
    cfg = _cfg(tmp_path)
    state = DetectorState()
    devices = {
        "devices": [
            _inverter("dead", power_w=0.0),
            _inverter("alive", power_w=500.0),
        ]
    }
    locations = _locations({"id": "loc1", "lat": LAT, "lon": LON})
    result = run_detection(
        cfg, state,
        fetch=_make_fetch(devices, locations),
        post=lambda *a: True,
        sleep=lambda s: None,
        now=lambda: NOON_UTC.timestamp(),
    )
    assert result["reachable"] is True
    assert any(a["type"] == DEAD_INVERTER for a in result["anomalies"])


def test_weather_or_site_is_informational_only(tmp_path):
    """WEATHER_OR_SITE is detected + counted in anomalies but never enters the
    alert/cooldown/recovery path; a DEAD_INVERTER in the same model still alerts.
    """
    cfg = _cfg(tmp_path)
    state = DetectorState()
    # All site PV at ~0W in daylight -> WEATHER_OR_SITE for every device.
    weather_devices = {
        "devices": [
            _inverter("a", dtype="sungrow", power_w=0.0, last_successful_poll_at=NOON_UTC.timestamp()),
            _inverter("b", dtype="solaredge", power_w=0.0, last_successful_poll_at=NOON_UTC.timestamp()),
        ]
    }
    locations = _locations({"id": "loc1", "lat": LAT, "lon": LON})
    result = run_detection(
        cfg, state,
        fetch=_make_fetch(weather_devices, locations),
        post=lambda *a: True,
        sleep=lambda s: None,
        now=lambda: NOON_UTC.timestamp(),
    )
    # Detected + counted.
    assert any(a["type"] == WEATHER_OR_SITE for a in result["anomalies"])
    # But no alerts, no recoveries, no autofixes triggered by it.
    assert result["alerts"] == []
    assert result["recoveries"] == []
    assert result["autofixes"] == []
    # And no cooldown state seeded for WEATHER entries -> can never recover.
    assert state.entries == {}

    # A second pass with WEATHER absent must NOT synthesize a recovery, since
    # WEATHER never entered cooldown state in the first place.
    healthy_devices = {"devices": [_inverter("a", dtype="sungrow", power_w=500.0)]}
    result2 = run_detection(
        cfg, state,
        fetch=_make_fetch(healthy_devices, locations),
        post=lambda *a: True,
        sleep=lambda s: None,
        now=lambda: NOON_UTC.timestamp() + 7200.0,
    )
    assert result2["recoveries"] == []
    assert all(a["type"] != WEATHER_OR_SITE for a in result2["recoveries"])

    # Contrast: a DEAD_INVERTER still alerts and seeds cooldown state.
    cfg2 = _cfg(tmp_path / "dead")
    state2 = DetectorState()
    dead_devices = {
        "devices": [
            _inverter("dead", dtype="sungrow", power_w=0.0),
            _inverter("alive", dtype="sungrow", power_w=500.0),
        ]
    }
    result3 = run_detection(
        cfg2, state2,
        fetch=_make_fetch(dead_devices, locations),
        post=lambda *a: True,
        sleep=lambda s: None,
        now=lambda: NOON_UTC.timestamp(),
    )
    assert any(a["type"] == DEAD_INVERTER for a in result3["alerts"])
    assert state2.entries != {}


def test_weather_or_site_never_reaches_dispatch_seams(tmp_path, monkeypatch):
    """End-to-end through main(): an all-site-zero (weather) pass with both
    flags ON sends NO push notification and NO webhook escalation."""
    monkeypatch.setenv("PV_ANOMALY_STATE_PATH", str(tmp_path / "state.json"))
    escalations: list[dict] = []
    push: list[str] = []
    monkeypatch.setattr(ad, "escalate", lambda a, c, **kw: escalations.append(a))
    monkeypatch.setattr(ad, "notify_push", lambda t, c, **kw: push.append(t))

    orig_from_env = DetectorConfig.from_env

    def enabled_from_env():
        cfg = orig_from_env()
        cfg.escalation_enabled = True
        cfg.push_enabled = True
        return cfg

    monkeypatch.setattr(DetectorConfig, "from_env", staticmethod(enabled_from_env))

    devices = {
        "devices": [
            _inverter("a", dtype="sungrow", power_w=0.0, last_successful_poll_at=NOON_UTC.timestamp()),
            _inverter("b", dtype="solaredge", power_w=0.0, last_successful_poll_at=NOON_UTC.timestamp()),
        ]
    }
    rc = ad.main(
        fetch=_make_fetch(devices, _locations({"id": "loc1", "lat": LAT, "lon": LON})),
        post=lambda *a: True,
        sleep=lambda s: None,
        now=lambda: NOON_UTC.timestamp(),
    )
    assert rc == 0
    assert escalations == []
    assert push == []


def _seeded_micro_production(device_id: str, cfg: DetectorConfig) -> dict[str, ProductionRecord]:
    """A production record that has already tripped the micro DEAD gate today
    (produced earlier, then read 0W enough times) so classify emits DEAD."""
    return {
        device_id: ProductionRecord(
            day=NOON_UTC.strftime("%Y-%m-%d"),
            seen_producing=True,
            zero_runs=cfg.micro_dead_min_zero_runs,
        )
    }


def _queue_fetch(devices_queue: list[dict[str, Any]], locations: dict[str, Any]):
    """fetch(url, timeout) that pops a fresh /api/devices payload per call.

    The first /api/devices call returns devices_queue[0] (initial sample); each
    subsequent /api/devices call returns the next queued payload, holding the
    last one once exhausted. /api/locations always returns ``locations``.
    Records the number of /api/devices calls on the returned closure's
    ``.devices_calls`` attribute.
    """
    idx = {"n": 0}

    def fetch(url: str, timeout: float):
        if url.endswith("/api/locations"):
            return locations
        if url.endswith("/api/devices"):
            i = min(idx["n"], len(devices_queue) - 1)
            idx["n"] += 1
            return devices_queue[i]
        return None

    fetch.idx = idx  # type: ignore[attr-defined]
    return fetch


def test_dead_resample_suppresses_transient_dip(tmp_path):
    """A micro reads 0W in the initial sample (classify -> DEAD) but a re-sample
    shows it producing -> the DEAD anomaly is dropped and zero_runs reset to 0."""
    cfg = _micro_cfg(tmp_path)
    state = DetectorState()
    state.production = _seeded_micro_production("micro", cfg)

    initial = {
        "devices": [
            _inverter("micro", dtype="opendtu", power_w=0.0),
            _inverter("sib", dtype="opendtu", power_w=500.0),
        ]
    }
    recovered = {
        "devices": [
            _inverter("micro", dtype="opendtu", power_w=263.0),
            _inverter("sib", dtype="opendtu", power_w=500.0),
        ]
    }
    locations = _locations({"id": "loc1", "lat": LAT, "lon": LON})
    sleeps: list[float] = []
    result = run_detection(
        cfg, state,
        fetch=_queue_fetch([initial, recovered], locations),
        post=lambda *a: True,
        sleep=lambda s: sleeps.append(s),
        now=lambda: NOON_UTC.timestamp(),
    )
    assert not any(a["type"] == DEAD_INVERTER for a in result["anomalies"])
    assert not any(a["type"] == DEAD_INVERTER for a in result["alerts"])
    assert state.production["micro"].zero_runs == 0
    # The injected sleep was used (gap before re-sample) and no real sleeping.
    assert sleeps and all(s == cfg.dead_confirm_gap_s for s in sleeps)


def test_dead_resample_keeps_genuinely_dead(tmp_path):
    """A device 0W in the initial sample AND every re-sample stays the DEAD
    anomaly and reaches alerts."""
    cfg = _micro_cfg(tmp_path)
    state = DetectorState()
    state.production = _seeded_micro_production("micro", cfg)

    zero = {
        "devices": [
            _inverter("micro", dtype="opendtu", power_w=0.0),
            _inverter("sib", dtype="opendtu", power_w=500.0),
        ]
    }
    locations = _locations({"id": "loc1", "lat": LAT, "lon": LON})
    result = run_detection(
        cfg, state,
        fetch=_queue_fetch([zero, zero, zero], locations),
        post=lambda *a: True,
        sleep=lambda s: None,
        now=lambda: NOON_UTC.timestamp(),
    )
    assert any(
        a["device_id"] == "micro" and a["type"] == DEAD_INVERTER
        for a in result["anomalies"]
    )
    assert any(
        a["device_id"] == "micro" and a["type"] == DEAD_INVERTER
        for a in result["alerts"]
    )


def test_dead_resample_absent_device_keeps_anomaly(tmp_path):
    """A re-sample where the device is absent (or fetch returns None) counts as
    'still zero' -> the DEAD anomaly is kept."""
    cfg = _micro_cfg(tmp_path)
    state = DetectorState()
    state.production = _seeded_micro_production("micro", cfg)

    initial = {
        "devices": [
            _inverter("micro", dtype="opendtu", power_w=0.0),
            _inverter("sib", dtype="opendtu", power_w=500.0),
        ]
    }
    # Re-sample payloads omit the micro entirely.
    absent = {"devices": [_inverter("sib", dtype="opendtu", power_w=500.0)]}
    locations = _locations({"id": "loc1", "lat": LAT, "lon": LON})
    result = run_detection(
        cfg, state,
        fetch=_queue_fetch([initial, absent, absent], locations),
        post=lambda *a: True,
        sleep=lambda s: None,
        now=lambda: NOON_UTC.timestamp(),
    )
    assert any(
        a["device_id"] == "micro" and a["type"] == DEAD_INVERTER
        for a in result["alerts"]
    )


def test_dead_resample_zero_resamples_is_legacy(tmp_path):
    """dead_confirm_resamples=0 disables confirmation: no re-sample fetches and
    the DEAD anomaly is kept (legacy single-sample path)."""
    cfg = _micro_cfg(tmp_path)
    cfg.dead_confirm_resamples = 0
    state = DetectorState()
    state.production = _seeded_micro_production("micro", cfg)

    initial = {
        "devices": [
            _inverter("micro", dtype="opendtu", power_w=0.0),
            _inverter("sib", dtype="opendtu", power_w=500.0),
        ]
    }
    locations = _locations({"id": "loc1", "lat": LAT, "lon": LON})
    fetch = _queue_fetch([initial], locations)
    result = run_detection(
        cfg, state,
        fetch=fetch,
        post=lambda *a: True,
        sleep=lambda s: None,
        now=lambda: NOON_UTC.timestamp(),
    )
    assert any(a["type"] == DEAD_INVERTER for a in result["alerts"])
    # Only the single initial /api/devices fetch happened — no re-samples.
    assert fetch.idx["n"] == 1


def test_dead_resample_does_not_touch_other_types(tmp_path):
    """STALE_POLL is never re-sampled or dropped by the confirmation pass."""
    cfg = _micro_cfg(tmp_path)
    state = DetectorState()
    initial = {"devices": [_inverter("d1", dtype="sungrow", connection_state="disconnected")]}
    locations = _locations({"id": "loc1", "lat": LAT, "lon": LON})
    fetch = _queue_fetch([initial], locations)
    result = run_detection(
        cfg, state,
        fetch=fetch,
        post=lambda *a: True,
        sleep=lambda s: None,
        now=lambda: NOON_UTC.timestamp(),
    )
    assert any(a["type"] == STALE_POLL for a in result["alerts"])
    # No DEAD anomaly present -> no re-sample fetch beyond the initial one.
    assert fetch.idx["n"] == 1


def test_run_detection_unreachable_api(tmp_path):
    cfg = _cfg(tmp_path)
    state = DetectorState()
    result = run_detection(
        cfg, state,
        fetch=lambda url, timeout: None,
        post=lambda *a: True,
        sleep=lambda s: None,
        now=lambda: NOON_UTC.timestamp(),
    )
    assert result["reachable"] is False
    assert result["anomalies"] == []


def test_main_returns_zero_on_clean_run(tmp_path, monkeypatch):
    monkeypatch.setenv("PV_ANOMALY_STATE_PATH", str(tmp_path / "state.json"))
    devices = {"devices": [_inverter("d1")]}
    locations = _locations({"id": "loc1", "lat": LAT, "lon": LON})
    rc = ad.main(
        fetch=_make_fetch(devices, locations),
        post=lambda *a: True,
        sleep=lambda s: None,
        now=lambda: NOON_UTC.timestamp(),
    )
    assert rc == 0
    # State file persisted.
    assert (tmp_path / "state.json").exists()


def test_main_returns_zero_on_unreachable_api(tmp_path, monkeypatch):
    monkeypatch.setenv("PV_ANOMALY_STATE_PATH", str(tmp_path / "state.json"))
    rc = ad.main(
        fetch=lambda url, timeout: None,
        post=lambda *a: True,
        sleep=lambda s: None,
        now=lambda: NOON_UTC.timestamp(),
    )
    assert rc == 0


def test_main_returns_one_on_crash(tmp_path, monkeypatch):
    monkeypatch.setenv("PV_ANOMALY_STATE_PATH", str(tmp_path / "state.json"))

    def boom(url, timeout):
        raise RuntimeError("unexpected")

    rc = ad.main(fetch=boom, post=lambda *a: True, sleep=lambda s: None, now=lambda: 1.0)
    assert rc == 1


def test_main_invokes_escalation_stubs_when_enabled(tmp_path, monkeypatch):
    """When flags are on, main() routes alerts through the webhook functions."""
    monkeypatch.setenv("PV_ANOMALY_STATE_PATH", str(tmp_path / "state.json"))
    escalations: list[dict] = []
    push: list[str] = []
    monkeypatch.setattr(ad, "escalate", lambda a, c, **kw: escalations.append(a))
    monkeypatch.setattr(ad, "notify_push", lambda t, c, **kw: push.append(t))

    # Force the flags on by patching from_env to return an enabled config.
    orig_from_env = DetectorConfig.from_env

    def enabled_from_env():
        cfg = orig_from_env()
        cfg.escalation_enabled = True
        cfg.push_enabled = True
        return cfg

    monkeypatch.setattr(DetectorConfig, "from_env", staticmethod(enabled_from_env))

    devices = {"devices": [_inverter("d1", connection_state="disconnected")]}
    rc = ad.main(
        fetch=_make_fetch(devices, _locations()),
        post=lambda *a: True,
        sleep=lambda s: None,
        now=lambda: NOON_UTC.timestamp(),
    )
    assert rc == 0
    assert len(escalations) >= 1
    assert len(push) >= 1


# --------------------------------------------------------------------------
# notify_push — push notification payload wiring
# --------------------------------------------------------------------------


def test_notify_push_builds_payload(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg.push_enabled = True  # defaults False now — enable to exercise POST path
    captured: list[tuple[str, dict, float]] = []
    monkeypatch.setattr(
        ad, "_post_json", lambda url, payload, timeout: captured.append((url, payload, timeout)) or {"status": "ok"}
    )
    ad.notify_push("hello", cfg, severity="error", dedup_key="d1:STALE_POLL")
    assert len(captured) == 1
    url, payload, _ = captured[0]
    assert url == f"{cfg.webhook_base_url}/api/push"
    assert payload["text"] == "hello"
    assert payload["source"] == "pv-inverter-master:anomaly"
    assert payload["severity"] == "error"
    assert payload["dedup_key"] == "d1:STALE_POLL"


def test_notify_push_truncates_to_300(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg.push_enabled = True  # defaults False now — enable to exercise POST path
    captured: list[dict] = []
    monkeypatch.setattr(
        ad, "_post_json", lambda url, payload, timeout: captured.append(payload) or {"status": "ok"}
    )
    ad.notify_push("x" * 500, cfg)
    assert len(captured[0]["text"]) == 300
    # dedup_key omitted when not provided.
    assert "dedup_key" not in captured[0]


def test_notify_push_noop_when_disabled(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg.push_enabled = False
    calls: list[Any] = []
    monkeypatch.setattr(ad, "_post_json", lambda *a, **k: calls.append(a))
    ad.notify_push("hi", cfg)
    assert calls == []


# --------------------------------------------------------------------------
# escalate — escalation webhook payload + pending retry
# --------------------------------------------------------------------------


def test_escalate_builds_webhook_payload(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg.escalation_enabled = True  # defaults False now — enable to exercise POST path
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        ad, "_post_json", lambda url, payload, timeout: captured.append((url, payload)) or {"action": "delivered", "receiver_id": "p1"}
    )
    anomaly = {"device_id": "sg", "type": MODBUS_HANG, "severity": "critical", "detail": "hang", "location_id": "loc1"}
    ad.escalate(anomaly, cfg, sleep=lambda s: None)
    assert len(captured) == 1
    url, payload = captured[0]
    assert url == f"{cfg.webhook_base_url}/api/escalate"
    assert payload["repo"] == "pv-inverter-master"
    assert set(payload) == {"repo", "context", "source", "dedup_key"}
    assert payload["source"] == "pv-inverter-master:anomaly"
    assert "MODBUS_HANG" in payload["context"]
    assert "sg" in payload["context"]
    assert len(payload["context"]) <= 4000
    # Coalescing key for the escalation receiver: <repo>:anomaly:<dev>:<cooldown_type>.
    assert payload["dedup_key"] == "pv-inverter-master:anomaly:sg:MODBUS_HANG"


def test_escalate_dedup_key_uses_cooldown_alias_and_type(tmp_path, monkeypatch):
    """dedup_key mirrors the cooldown identity: STALE_POLL stays STALE_POLL, and
    MULTI_CLIENT aliases to MODBUS_HANG so its escalations coalesce onto the ONE
    shared incident stream (same as push notification + apply_cooldown keying)."""
    cfg = _cfg(tmp_path)
    cfg.escalation_enabled = True
    captured: list[dict] = []
    monkeypatch.setattr(
        ad, "_post_json",
        lambda url, payload, timeout: captured.append(payload) or {"action": "delivered"},
    )
    ad.escalate(
        {"device_id": "aabbccdd0003", "type": STALE_POLL, "severity": "warning",
         "detail": "connection_state=night_mode", "location_id": ""},
        cfg, sleep=lambda s: None,
    )
    ad.escalate(
        {"device_id": "aabbccdd0004", "type": MULTI_CLIENT, "severity": "critical",
         "detail": "foreign client", "location_id": ""},
        cfg, sleep=lambda s: None,
    )
    assert captured[0]["dedup_key"] == "pv-inverter-master:anomaly:aabbccdd0003:STALE_POLL"
    assert captured[1]["dedup_key"] == "pv-inverter-master:anomaly:aabbccdd0004:MODBUS_HANG"


def test_escalate_pending_retries_once(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg.escalation_enabled = True  # defaults False now — enable to exercise POST path
    posts: list[dict] = []
    sleeps: list[float] = []

    def fake_post(url, payload, timeout):
        posts.append(payload)
        # First call: startup pending; second call: delivered.
        if len(posts) == 1:
            return {"action": "pending"}
        return {"action": "delivered", "receiver_id": "p1"}

    monkeypatch.setattr(ad, "_post_json", fake_post)
    anomaly = {"device_id": "sg", "type": STALE_POLL, "severity": "warning", "detail": "x", "location_id": ""}
    ad.escalate(anomaly, cfg, sleep=lambda s: sleeps.append(s))
    assert len(posts) == 2  # exactly one retry
    assert len(sleeps) == 1


def test_escalate_noop_when_disabled(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg.escalation_enabled = False
    calls: list[Any] = []
    monkeypatch.setattr(ad, "_post_json", lambda *a, **k: calls.append(a))
    ad.escalate({"device_id": "x", "type": STALE_POLL}, cfg, sleep=lambda s: None)
    assert calls == []


# --------------------------------------------------------------------------
# from_env kill-switches
# --------------------------------------------------------------------------


def test_config_defaults_are_safe_off():
    """Safe-by-default: a fresh DetectorConfig() has both flags OFF so dev/test/
    manual runs can never alert the configured webhook (incident 2026-05-29)."""
    cfg = DetectorConfig()
    assert cfg.push_enabled is False
    assert cfg.escalation_enabled is False


def test_from_env_flags_default_false(monkeypatch):
    monkeypatch.delenv("PV_ANOMALY_PUSH", raising=False)
    monkeypatch.delenv("PV_ANOMALY_ESCALATION", raising=False)
    cfg = DetectorConfig.from_env()
    assert cfg.push_enabled is False
    assert cfg.escalation_enabled is False


def test_from_env_enables_when_set(monkeypatch):
    """Production enables via Environment= (PV_ANOMALY_*=1) — truthy turns ON."""
    monkeypatch.setenv("PV_ANOMALY_PUSH", "1")
    monkeypatch.setenv("PV_ANOMALY_ESCALATION", "1")
    monkeypatch.setenv("PV_ANOMALY_WEBHOOK_BASE_URL", "http://127.0.0.1:9000")
    cfg = DetectorConfig.from_env()
    assert cfg.webhook_base_url == "http://127.0.0.1:9000"
    assert cfg.push_enabled is True
    assert cfg.escalation_enabled is True


def test_from_env_falsy_keeps_off(monkeypatch):
    monkeypatch.setenv("PV_ANOMALY_PUSH", "0")
    monkeypatch.setenv("PV_ANOMALY_ESCALATION", "false")
    cfg = DetectorConfig.from_env()
    assert cfg.push_enabled is False
    assert cfg.escalation_enabled is False


# --------------------------------------------------------------------------
# Re-sample confirmation tunables (dead_confirm_*)
# --------------------------------------------------------------------------


def test_dead_confirm_defaults():
    """A fresh DetectorConfig carries the production re-sample defaults."""
    cfg = DetectorConfig()
    assert cfg.dead_confirm_resamples == 2
    assert cfg.dead_confirm_gap_s == 3.0


def test_dead_confirm_from_env_overrides(monkeypatch):
    monkeypatch.setenv("PV_ANOMALY_DEAD_CONFIRM_RESAMPLES", "3")
    monkeypatch.setenv("PV_ANOMALY_DEAD_CONFIRM_GAP_S", "1.5")
    cfg = DetectorConfig.from_env()
    assert cfg.dead_confirm_resamples == 3
    assert cfg.dead_confirm_gap_s == 1.5


def test_dead_confirm_from_env_defaults_when_unset(monkeypatch):
    monkeypatch.delenv("PV_ANOMALY_DEAD_CONFIRM_RESAMPLES", raising=False)
    monkeypatch.delenv("PV_ANOMALY_DEAD_CONFIRM_GAP_S", raising=False)
    cfg = DetectorConfig.from_env()
    assert cfg.dead_confirm_resamples == 2
    assert cfg.dead_confirm_gap_s == 3.0


def test_dead_confirm_from_env_malformed_falls_back(monkeypatch):
    monkeypatch.setenv("PV_ANOMALY_DEAD_CONFIRM_RESAMPLES", "")
    monkeypatch.setenv("PV_ANOMALY_DEAD_CONFIRM_GAP_S", "not-a-float")
    cfg = DetectorConfig.from_env()
    assert cfg.dead_confirm_resamples == 2
    assert cfg.dead_confirm_gap_s == 3.0


# --------------------------------------------------------------------------
# main() dispatch — recoveries + successful autofixes
# --------------------------------------------------------------------------


def test_dispatch_routes_recovery_and_autofix(tmp_path, monkeypatch):
    """_dispatch_events sends a recovery (severity=recovered, original dedup_key)
    and a successful autofix (human-readable push text)."""
    cfg = _cfg(tmp_path)
    cfg.push_enabled = True  # defaults False now — enable so dispatch routes
    cfg.escalation_enabled = True
    push: list[dict] = []
    monkeypatch.setattr(
        ad, "notify_push",
        lambda text, c, *, severity="info", dedup_key=None: push.append(
            {"text": text, "severity": severity, "dedup_key": dedup_key}
        ),
    )
    monkeypatch.setattr(ad, "escalate", lambda a, c, **kw: None)

    result = {
        "reachable": True,
        "anomalies": [],
        "alerts": [],
        "recoveries": [
            {"device_id": "d1", "type": STALE_POLL, "severity": "info", "detail": "recovered", "location_id": ""}
        ],
        "autofixes": [
            {"device_id": "sg", "attempted": True, "rebooted": True, "recovered": True, "capped": False}
        ],
    }
    ad._dispatch_events(result, cfg, sleep=lambda s: None)

    recovery = next(m for m in push if m["severity"] == "recovered")
    assert recovery["dedup_key"] == "d1:STALE_POLL"  # same key as the original alert
    autofix = next(m for m in push if m["dedup_key"] == "sg:autofix")
    assert autofix["severity"] == "warn"
    assert "sg" in autofix["text"]
    assert "laeuft wieder" in autofix["text"]


def test_dispatch_alert_severity_and_dedup_key(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg.push_enabled = True  # defaults False now — enable so dispatch routes
    cfg.escalation_enabled = True
    push: list[dict] = []
    escalations: list[dict] = []
    monkeypatch.setattr(
        ad, "notify_push",
        lambda text, c, *, severity="info", dedup_key=None: push.append(
            {"text": text, "severity": severity, "dedup_key": dedup_key}
        ),
    )
    monkeypatch.setattr(ad, "escalate", lambda a, c, **kw: escalations.append(a))

    result = {
        "reachable": True,
        "anomalies": [],
        "alerts": [
            {"device_id": "dead1", "type": DEAD_INVERTER, "severity": "critical", "detail": "0W", "location_id": "loc1"},
            {"device_id": "d2", "type": STALE_POLL, "severity": "warning", "detail": "stale", "location_id": "loc1"},
        ],
        "recoveries": [],
        "autofixes": [],
    }
    ad._dispatch_events(result, cfg, sleep=lambda s: None)

    dead = next(m for m in push if m["dedup_key"] == "dead1:DEAD_INVERTER")
    assert dead["severity"] == "error"
    stale = next(m for m in push if m["dedup_key"] == "d2:STALE_POLL")
    assert stale["severity"] == "warn"
    assert len(escalations) == 2  # both unresolved alerts escalated


def test_dispatch_noop_autofix_not_reported_when_not_recovered(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg.push_enabled = True  # defaults False now — enable so the recovered-gate is what suppresses
    push: list[dict] = []
    monkeypatch.setattr(
        ad, "notify_push",
        lambda text, c, *, severity="info", dedup_key=None: push.append({"dedup_key": dedup_key}),
    )
    monkeypatch.setattr(ad, "escalate", lambda a, c, **kw: None)
    result = {
        "reachable": True, "anomalies": [], "alerts": [], "recoveries": [],
        "autofixes": [
            {"device_id": "sg", "attempted": True, "rebooted": True, "recovered": False, "capped": False}
        ],
    }
    ad._dispatch_events(result, cfg, sleep=lambda s: None)
    assert not any(m["dedup_key"] == "sg:autofix" for m in push)


# --------------------------------------------------------------------------
# Micro-inverter DEAD gate (was-producing-then-dropped) + exclude-list
# --------------------------------------------------------------------------


def _loc_map():
    return build_location_map(_locations({"id": "loc1", "lat": LAT, "lon": LON}))


# Exclude the default-excluded device so it doesn't interfere with these tests
# (and so a micro at 0W is actually evaluated unless we explicitly exclude it).
def _micro_cfg(tmp_path: Path) -> DetectorConfig:
    cfg = _cfg(tmp_path)
    cfg.exclude_ids = frozenset()  # evaluate everything unless a test opts in
    return cfg


def test_micro_zero_from_start_of_day_not_flagged(tmp_path):
    """A micro at 0W from the start of the day (never produced) is NOT flagged
    even with a producing sibling in daylight."""
    cfg = _micro_cfg(tmp_path)
    devices = {
        "devices": [
            _inverter("micro_dead", dtype="opendtu", power_w=0.0),
            _inverter("micro_alive", dtype="opendtu", power_w=500.0),
        ]
    }
    production: dict[str, ProductionRecord] = {}
    anomalies = classify_anomalies(devices, _loc_map(), cfg, NOON_UTC, production=production)
    flagged = {(a["device_id"], a["type"]) for a in anomalies}
    assert ("micro_dead", DEAD_INVERTER) not in flagged
    # seen_producing stayed False -> never produced today.
    assert production["micro_dead"].seen_producing is False
    assert production["micro_dead"].zero_runs == 1


def test_micro_was_producing_then_dropped_flags_after_n_runs(tmp_path):
    """A micro that produced earlier (seen_producing True) then reads 0W is NOT
    flagged before reaching N consecutive zero runs, then IS flagged once it
    reaches >= micro_dead_min_zero_runs."""
    cfg = _micro_cfg(tmp_path)
    assert cfg.micro_dead_min_zero_runs == 2

    production: dict[str, ProductionRecord] = {}

    # Run 1: the micro is producing, sibling producing -> no DEAD, marks
    # seen_producing.
    producing = {
        "devices": [
            _inverter("micro", dtype="opendtu", power_w=400.0),
            _inverter("sib", dtype="opendtu", power_w=500.0),
        ]
    }
    classify_anomalies(producing, _loc_map(), cfg, NOON_UTC, production=production)
    assert production["micro"].seen_producing is True
    assert production["micro"].zero_runs == 0

    # Run 2: micro drops to 0W, sibling still producing. zero_runs=1 (< 2) ->
    # NOT yet flagged.
    dropped = {
        "devices": [
            _inverter("micro", dtype="opendtu", power_w=0.0),
            _inverter("sib", dtype="opendtu", power_w=500.0),
        ]
    }
    anomalies = classify_anomalies(dropped, _loc_map(), cfg, NOON_UTC, production=production)
    assert production["micro"].zero_runs == 1
    assert not any(
        a["device_id"] == "micro" and a["type"] == DEAD_INVERTER for a in anomalies
    )

    # Run 3: still 0W, zero_runs reaches 2 (>= N) -> NOW flagged DEAD.
    anomalies = classify_anomalies(dropped, _loc_map(), cfg, NOON_UTC, production=production)
    assert production["micro"].zero_runs == 2
    assert any(
        a["device_id"] == "micro" and a["type"] == DEAD_INVERTER for a in anomalies
    )


def test_large_inverter_flagged_immediately(tmp_path):
    """A large inverter (sungrow) at 0W + producing sibling in daylight is
    flagged DEAD immediately on the first run (unchanged behavior)."""
    cfg = _micro_cfg(tmp_path)
    devices = {
        "devices": [
            _inverter("big_dead", dtype="sungrow", power_w=0.0),
            _inverter("big_alive", dtype="solaredge", power_w=500.0),
        ]
    }
    production: dict[str, ProductionRecord] = {}
    anomalies = classify_anomalies(devices, _loc_map(), cfg, NOON_UTC, production=production)
    assert any(
        a["device_id"] == "big_dead" and a["type"] == DEAD_INVERTER for a in anomalies
    )


def test_excluded_device_produces_no_anomaly(tmp_path):
    """The default-excluded device (aabbccdd0001) never produces ANY anomaly,
    even when it would otherwise be DEAD and STALE, and is not production-
    tracked."""
    cfg = _cfg(tmp_path)  # keeps the default exclude set
    assert "aabbccdd0001" in cfg.exclude_ids
    devices = {
        "devices": [
            # Excluded micro: 0W + disconnected -> would be DEAD + STALE.
            _inverter(
                "aabbccdd0001",
                dtype="opendtu",
                power_w=0.0,
                connection_state="disconnected",
            ),
            _inverter("micro_alive", dtype="opendtu", power_w=500.0),
        ]
    }
    production: dict[str, ProductionRecord] = {}
    anomalies = classify_anomalies(devices, _loc_map(), cfg, NOON_UTC, production=production)
    assert not any(a["device_id"] == "aabbccdd0001" for a in anomalies)
    # No production tracking for the excluded device.
    assert "aabbccdd0001" not in production


def test_exclude_ids_env_replaces_default(monkeypatch):
    """PV_ANOMALY_EXCLUDE_IDS (comma-separated) REPLACES the default exclude
    set when present."""
    monkeypatch.setenv("PV_ANOMALY_EXCLUDE_IDS", "aaa, bbb ,ccc")
    cfg = DetectorConfig.from_env()
    assert cfg.exclude_ids == frozenset({"aaa", "bbb", "ccc"})
    assert "aabbccdd0001" not in cfg.exclude_ids


def test_exclude_ids_default_when_env_unset(monkeypatch):
    monkeypatch.delenv("PV_ANOMALY_EXCLUDE_IDS", raising=False)
    cfg = DetectorConfig.from_env()
    # Both remote-site OpenDTU rig-mates on 203.0.113.186 are known-ok 0W micros.
    assert cfg.exclude_ids == frozenset({"aabbccdd0001", "aabbccdd0002"})


def test_production_history_resets_on_new_day(tmp_path):
    """A stored production record from a previous day is reset (seen_producing
    back to False, zero_runs to 0) on a run with a new day key."""
    cfg = _micro_cfg(tmp_path)
    # Yesterday's record: produced and then had some zero runs.
    yesterday = (NOON_UTC).strftime("%Y-%m-%d")
    production: dict[str, ProductionRecord] = {
        "micro": ProductionRecord(day="2020-01-01", seen_producing=True, zero_runs=5)
    }
    # A new day: the micro reads 0W from the start -> record reset first, then
    # this run counts as a single zero run, seen_producing stays False.
    devices = {
        "devices": [
            _inverter("micro", dtype="opendtu", power_w=0.0),
            _inverter("sib", dtype="opendtu", power_w=500.0),
        ]
    }
    anomalies = classify_anomalies(devices, _loc_map(), cfg, NOON_UTC, production=production)
    rec = production["micro"]
    assert rec.day == yesterday
    assert rec.seen_producing is False
    assert rec.zero_runs == 1
    # And because seen_producing was reset, the micro is NOT flagged DEAD.
    assert not any(
        a["device_id"] == "micro" and a["type"] == DEAD_INVERTER for a in anomalies
    )


def test_production_history_persists_through_run_detection(tmp_path):
    """run_detection threads state.production through and mutates it in place so
    it survives across hourly runs (save_state/load_state round-trip)."""
    cfg = _micro_cfg(tmp_path)
    state = DetectorState()
    devices = {
        "devices": [
            _inverter("micro", dtype="opendtu", power_w=400.0),
            _inverter("sib", dtype="opendtu", power_w=500.0),
        ]
    }
    locations = _locations({"id": "loc1", "lat": LAT, "lon": LON})
    run_detection(
        cfg, state,
        fetch=_make_fetch(devices, locations),
        post=lambda *a: True,
        sleep=lambda s: None,
        now=lambda: NOON_UTC.timestamp(),
    )
    assert state.production["micro"].seen_producing is True
    # Round-trip through disk.
    save_state(cfg.state_path, state)
    loaded = load_state(cfg.state_path)
    assert loaded.production["micro"].seen_producing is True
    assert loaded.production["micro"].day == NOON_UTC.strftime("%Y-%m-%d")


# --------------------------------------------------------------------------
# RADIO_GAP: source-stale OpenDTU micro -> soft RADIO_GAP, time-escalation to
# DEAD (masking-safe), fail-safe default, flap-guard, persistence round-trip.
# --------------------------------------------------------------------------


def _stale_devs(now_utc: datetime, power_sib: float = 600.0) -> dict[str, Any]:
    """A source-stale Garden-like micro + a producing sibling (daylight).

    Carries a FRESH last_successful_poll_at (= the run's clock): a radio-gap
    poll returns success=True, so the proxy stamps the poll fresh — the device
    is NOT STALE_POLL, only source_stale. (Realistic Garden row.)
    """
    ts = now_utc.timestamp()
    return {
        "devices": [
            _inverter(
                "garden", dtype="opendtu", power_w=0.0, source_stale=True,
                last_successful_poll_at=ts,
            ),
            _inverter("sib", dtype="opendtu", power_w=power_sib, last_successful_poll_at=ts),
        ]
    }


def test_source_stale_micro_is_radio_gap_not_dead(tmp_path):
    """A would-be-DEAD OpenDTU micro that is source_stale (radio gap) in
    daylight with a producing sibling is classified RADIO_GAP (warning), NOT
    DEAD_INVERTER — and bypasses the seen_producing gate (staleness is positive
    evidence of a radio gap, not a shaded-0W reading). This is today's Garden
    case: the alarm-fatigue fix."""
    cfg = _micro_cfg(tmp_path)
    radio_gaps: dict[str, RadioGapRecord] = {}
    anomalies = classify_anomalies(
        _stale_devs(NOON_UTC), _loc_map(), cfg, NOON_UTC, production={}, radio_gaps=radio_gaps
    )
    types = {(a["device_id"], a["type"]) for a in anomalies}
    assert ("garden", RADIO_GAP) in types
    assert ("garden", DEAD_INVERTER) not in types
    gap_anom = next(a for a in anomalies if a["device_id"] == "garden")
    assert gap_anom["severity"] == "warning"
    # A persistence clock was started.
    assert "garden" in radio_gaps


def test_reachable_zero_micro_without_source_stale_is_dead(tmp_path):
    """FAIL-SAFE REGRESSION GUARD: a micro at 0W that is NOT source_stale
    (genuinely reachable, producing 0) still flags DEAD_INVERTER once the
    existing seen_producing + zero_runs gate is met. The radio-gap path must
    never suppress a genuinely reachable dead inverter."""
    cfg = _micro_cfg(tmp_path)
    production: dict[str, ProductionRecord] = {}
    # Run 1: producing -> seen_producing True.
    classify_anomalies(
        {
            "devices": [
                _inverter("micro", dtype="opendtu", power_w=400.0),
                _inverter("sib", dtype="opendtu", power_w=500.0),
            ]
        },
        _loc_map(), cfg, NOON_UTC, production=production, radio_gaps={},
    )
    dropped = {
        "devices": [
            _inverter("micro", dtype="opendtu", power_w=0.0, source_stale=False),
            _inverter("sib", dtype="opendtu", power_w=500.0),
        ]
    }
    # Run 2: zero_runs=1 -> not yet.
    classify_anomalies(dropped, _loc_map(), cfg, NOON_UTC, production=production, radio_gaps={})
    # Run 3: zero_runs=2 -> DEAD critical.
    anomalies = classify_anomalies(
        dropped, _loc_map(), cfg, NOON_UTC, production=production, radio_gaps={}
    )
    dead = [a for a in anomalies if a["device_id"] == "micro" and a["type"] == DEAD_INVERTER]
    assert dead and dead[0]["severity"] == "critical"


def test_radio_gap_escalates_to_dead_after_threshold(tmp_path):
    """MASKING-SAFETY (the load-bearing test): a source_stale gap that does NOT
    recover keeps accumulating DAYLIGHT staleness and, once it exceeds
    radio_gap_escalate_s, is re-promoted to DEAD_INVERTER (critical). Proves the
    de-noising never permanently masks a real radio-silent death."""
    cfg = _micro_cfg(tmp_path)
    cfg.radio_gap_escalate_s = 5000.0  # below the per-run RADIO_GAP_MAX_DELTA cap
    radio_gaps: dict[str, RadioGapRecord] = {}
    t1 = NOON_UTC + timedelta(seconds=3600)
    t2 = NOON_UTC + timedelta(seconds=7200)
    # Run 1: clock starts at 0 -> RADIO_GAP.
    a0 = classify_anomalies(_stale_devs(NOON_UTC), _loc_map(), cfg, NOON_UTC, production={}, radio_gaps=radio_gaps)
    assert any(a["device_id"] == "garden" and a["type"] == RADIO_GAP for a in a0)
    # Run 2: +3600s daylight stale (<= 5000) -> still RADIO_GAP.
    a1 = classify_anomalies(
        _stale_devs(t1), _loc_map(), cfg, t1, production={}, radio_gaps=radio_gaps
    )
    assert any(a["device_id"] == "garden" and a["type"] == RADIO_GAP for a in a1)
    assert not any(a["device_id"] == "garden" and a["type"] == DEAD_INVERTER for a in a1)
    # Run 3: +7200s total (> 5000) -> escalates to DEAD critical, no RADIO_GAP.
    a2 = classify_anomalies(
        _stale_devs(t2), _loc_map(), cfg, t2, production={}, radio_gaps=radio_gaps
    )
    dead = [a for a in a2 if a["device_id"] == "garden" and a["type"] == DEAD_INVERTER]
    assert dead and dead[0]["severity"] == "critical"
    assert not any(a["device_id"] == "garden" and a["type"] == RADIO_GAP for a in a2)


def test_radio_gap_survives_night_then_pages_next_day(tmp_path):
    """A gap starting in daylight, persisting through the night (no accumulation
    and NO alert at night), still stale next day -> keeps its clock and escalates
    to DEAD. An overnight death is never silently reset."""
    cfg = _micro_cfg(tmp_path)
    cfg.radio_gap_escalate_s = 5000.0
    radio_gaps: dict[str, RadioGapRecord] = {}
    t_build = NOON_UTC + timedelta(seconds=4000)
    t_day2 = NOON_UTC + timedelta(days=1, seconds=2000)
    # Day 1: build the clock to 4000s of daylight staleness (two daylight runs).
    classify_anomalies(_stale_devs(NOON_UTC), _loc_map(), cfg, NOON_UTC, production={}, radio_gaps=radio_gaps)
    classify_anomalies(
        _stale_devs(t_build), _loc_map(), cfg, t_build, production={}, radio_gaps=radio_gaps
    )
    assert radio_gaps["garden"].daylight_stale_s == 4000.0
    # NIGHT (solar midnight): sun down -> NO anomaly, clock frozen (not reset).
    night = classify_anomalies(
        _stale_devs(MIDNIGHT_UTC), _loc_map(), cfg, MIDNIGHT_UTC, production={}, radio_gaps=radio_gaps
    )
    assert not any(a["device_id"] == "garden" for a in night)
    assert radio_gaps["garden"].daylight_stale_s == 4000.0  # frozen, not reset
    # NEXT DAY daylight: still stale -> clock resumes (capped delta) -> DEAD.
    day2 = classify_anomalies(
        _stale_devs(t_day2), _loc_map(), cfg, t_day2,
        production={}, radio_gaps=radio_gaps,
    )
    dead = [a for a in day2 if a["device_id"] == "garden" and a["type"] == DEAD_INVERTER]
    assert dead and dead[0]["severity"] == "critical"


def test_substantial_recovery_resets_radio_gap_clock(tmp_path):
    """A genuine production frame (power_w > ac_zero_w) clears the gap clock."""
    cfg = _micro_cfg(tmp_path)
    radio_gaps = {
        "garden": RadioGapRecord(
            gap_start_ts=NOON_UTC.timestamp() - 3600,
            daylight_stale_s=3000.0,
            last_eval_ts=NOON_UTC.timestamp() - 100,
        )
    }
    producing = {
        "devices": [
            _inverter("garden", dtype="opendtu", power_w=450.0),  # real production
            _inverter("sib", dtype="opendtu", power_w=600.0),
        ]
    }
    classify_anomalies(producing, _loc_map(), cfg, NOON_UTC, production={}, radio_gaps=radio_gaps)
    assert "garden" not in radio_gaps  # cleared on substantial recovery


def test_reachable_zero_flap_does_not_reset_radio_gap_clock(tmp_path):
    """A single reachable-but-0W flap (source_stale False, power_w 0) must NOT
    reset the persistence clock — otherwise a dead micro that flickers reachable
    could mask itself via the flap back door (review finding)."""
    cfg = _micro_cfg(tmp_path)
    radio_gaps = {
        "garden": RadioGapRecord(
            gap_start_ts=NOON_UTC.timestamp() - 3600,
            daylight_stale_s=3000.0,
            last_eval_ts=NOON_UTC.timestamp() - 100,
        )
    }
    flap = {
        "devices": [
            _inverter("garden", dtype="opendtu", power_w=0.0, source_stale=False),
            _inverter("sib", dtype="opendtu", power_w=600.0),
        ]
    }
    classify_anomalies(flap, _loc_map(), cfg, NOON_UTC, production={}, radio_gaps=radio_gaps)
    # power_w=0 is not a substantial recovery -> clock preserved, untouched.
    assert "garden" in radio_gaps
    assert radio_gaps["garden"].daylight_stale_s == 3000.0


def test_radio_gap_record_state_roundtrip(tmp_path):
    """RadioGapRecord survives save_state/load_state so the persistence clock is
    not lost across hourly runs (a death's clock must not reset on restart)."""
    path = tmp_path / "state.json"
    state = DetectorState()
    state.radio_gaps["garden"] = RadioGapRecord(
        gap_start_ts=111.0, daylight_stale_s=2222.0, last_eval_ts=333.0
    )
    save_state(path, state)
    loaded = load_state(path)
    assert "garden" in loaded.radio_gaps
    r = loaded.radio_gaps["garden"]
    assert r.gap_start_ts == 111.0
    assert r.daylight_stale_s == 2222.0
    assert r.last_eval_ts == 333.0


def test_run_detection_source_stale_micro_emits_no_alert(tmp_path):
    """END-TO-END (the alarm-fatigue fix): a source_stale micro (today's Garden
    case) produces NO alert — RADIO_GAP is informational-only, never a push
    notification. It is still tracked (anomalies + clock) so it can escalate later."""
    cfg = _micro_cfg(tmp_path)
    cfg.dead_confirm_resamples = 0  # no resample fetches needed
    devices = _stale_devs(NOON_UTC)
    locations = _locations({"id": "loc1", "lat": LAT, "lon": LON})
    state = DetectorState()
    result = run_detection(
        cfg,
        state,
        fetch=_make_fetch(devices, locations),
        post=lambda *a: True,
        sleep=lambda s: None,
        now=lambda: NOON_UTC.timestamp(),
    )
    assert result["alerts"] == []  # no critical alert/push for a transient radio gap
    assert any(a["type"] == RADIO_GAP for a in result["anomalies"])
    assert "garden" in state.radio_gaps


# ---------------------------------------------------------------------------
# v8.3.4: DEAD_INVERTER freshness gate (live misfire 2026-06-11 09:00)
# ---------------------------------------------------------------------------

class TestDeadInverterFreshnessGate:
    """A 0W reading without a fresh successful poll is NOT dead-evidence."""

    NOW = datetime(2026, 6, 11, 11, 0, tzinfo=timezone.utc)

    def _mk(self, **kw):
        base = {
            "id": "se1", "type": "solaredge", "location_id": "loc1",
            "power_w": 0, "connection_state": "quiesce",
            "last_successful_poll_at": 0, "poll_interval": 10,
            "last_poll_error": "Modbus Error: [Input/Output] No response received",
        }
        base.update(kw)
        return base

    def _sibling(self, fresh=True, power=500.0):
        ts = self.NOW.timestamp() - (10.0 if fresh else 7200.0)
        return {
            "id": "sib1", "type": "opendtu", "location_id": "loc1",
            "power_w": power, "connection_state": "connected",
            "last_successful_poll_at": ts,
            "poll_interval": 10, "last_poll_error": None,
        }

    def _classify(self, devices):
        from pv_inverter_master.monitoring.anomaly_detector import (
            classify_anomalies, DetectorConfig,
        )
        # midday UTC in June, location with real coords (sun clearly up)
        now = self.NOW
        return classify_anomalies(
            {"devices": devices},
            location_map={"loc1": (49.5, 11.3)},
            cfg=DetectorConfig(),
            now_utc=now,
            radio_gaps={},
        )

    def test_never_polled_device_not_dead(self):
        """Startup default 0W on a never-polled device must not page DEAD
        (it is already covered by STALE_POLL/MODBUS_HANG)."""
        dev = self._mk(last_successful_poll_at=0)
        anomalies = self._classify([dev, self._sibling()])
        types = {a["type"] for a in anomalies if a["device_id"] == "se1"}
        assert "DEAD_INVERTER" not in types
        # the wedge itself is still surfaced
        assert "MODBUS_HANG" in types or "STALE_POLL" in types

    def test_stale_device_not_dead(self):
        dev = self._mk(last_successful_poll_at=self.NOW.timestamp() - 7200)
        anomalies = self._classify([dev, self._sibling()])
        types = {a["type"] for a in anomalies if a["device_id"] == "se1"}
        assert "DEAD_INVERTER" not in types

    def test_fresh_zero_with_fresh_sibling_still_dead(self):
        """Regression guard: a genuinely fresh 0W reading with a fresh
        producing sibling must still page DEAD_INVERTER."""
        dev = self._mk(
            last_successful_poll_at=self.NOW.timestamp() - 10,
            connection_state="connected",
            last_poll_error=None,
        )
        anomalies = self._classify([dev, self._sibling()])
        types = {a["type"] for a in anomalies if a["device_id"] == "se1"}
        assert "DEAD_INVERTER" in types

    def test_frozen_sibling_power_does_not_anchor_dead(self):
        """A wedged sibling's frozen power_w must not count as 'producing'."""
        dev = self._mk(
            last_successful_poll_at=self.NOW.timestamp() - 10,
            connection_state="connected",
            last_poll_error=None,
        )
        stale_sib = self._sibling(fresh=False, power=5658.0)
        anomalies = self._classify([dev, stale_sib])
        types = {a["type"] for a in anomalies if a["device_id"] == "se1"}
        assert "DEAD_INVERTER" not in types


def test_modbus_hang_connection_refused_and_timeout(tmp_path):
    """A powered-off Modbus-TCP inverter raises plain socket errors — these
    must still reach MODBUS_HANG critical. Without this, a hard-dead WR is
    stuck at STALE_POLL warning forever now that DEAD requires fresh data."""
    cfg = _cfg(tmp_path)
    # "Not connected" is pymodbus's ConnectionException text when the TCP
    # connect itself fails (live SE30K wedge #2, 2026-06-11 evening: port
    # 1502 refusing -> every poll errored "Not connected" and the outage
    # silently downgraded from MODBUS_HANG to STALE_POLL).
    for err in ("[Errno 111] Connection refused", "connect timed out", "Not connected"):
        devices = {
            "devices": [
                _inverter(
                    "se", dtype="solaredge",
                    connection_state="reconnecting", last_poll_error=err,
                )
            ]
        }
        anomalies = classify_anomalies(devices, {}, cfg, NOON_UTC)
        assert any(a["type"] == MODBUS_HANG for a in anomalies), err


def test_dispatch_escalates_only_highest_severity_per_device(tmp_path, monkeypatch):
    """A wedged device raising MODBUS_HANG (error) + STALE_POLL (warn) in the
    same run escalates ONCE (the critical one); push notifications stay per-type.
    Prevents duplicate escalation requests for the same root cause."""
    cfg = _cfg(tmp_path)
    cfg.push_enabled = True
    cfg.escalation_enabled = True
    push: list[dict] = []
    escalations: list[dict] = []
    monkeypatch.setattr(
        ad, "notify_push",
        lambda text, c, *, severity="info", dedup_key=None: push.append(
            {"severity": severity, "dedup_key": dedup_key}
        ),
    )
    monkeypatch.setattr(ad, "escalate", lambda a, c, **kw: escalations.append(a))

    result = {
        "reachable": True,
        "anomalies": [],
        "alerts": [
            {"device_id": "d1", "type": STALE_POLL, "severity": "warning", "detail": "quiesce", "location_id": ""},
            {"device_id": "d1", "type": MODBUS_HANG, "severity": "critical", "detail": "hang", "location_id": ""},
            {"device_id": "d2", "type": STALE_POLL, "severity": "warning", "detail": "stale", "location_id": ""},
        ],
        "recoveries": [],
        "autofixes": [],
    }
    ad._dispatch_events(result, cfg, sleep=lambda s: None)

    # d1: exactly ONE escalation, the error-ranked MODBUS_HANG.
    d1 = [e for e in escalations if e["device_id"] == "d1"]
    assert len(d1) == 1 and d1[0]["type"] == MODBUS_HANG
    # d2 (only a warn alert) still escalates.
    assert [e["type"] for e in escalations if e["device_id"] == "d2"] == [STALE_POLL]
    # Push notifications unchanged: one push per alert type.
    assert sorted(m["dedup_key"] for m in push) == [
        "d1:MODBUS_HANG", "d1:STALE_POLL", "d2:STALE_POLL",
    ]


# ---------------------------------------------------------------------------
# Staleness threshold: effective cadence vs raw config
# ---------------------------------------------------------------------------


def _threshold(device: dict[str, Any]) -> float:
    """The staleness threshold the detector derives for a device row."""
    return max(5.0 * ad._poll_cadence_s(device), 300.0)


# The full live fleet as measured on .191, 2026-07-18: (raw, effective).
# Every single one must land on the 300s floor both before and after the
# switch to the effective cadence — this pins that the change is a
# latent-correctness fix and shifts NO alert classification today.
LIVE_FLEET_CADENCES = [
    (0.0, 1.0),    # solaredge SE30K, senec
    (0.0, 5.0),    # opendtu x5
    (0.0, None),   # shelly (never resolved)
    (10.0, 10.0),  # sungrow (explicit per-device override)
    (0.0, 20.0),   # hoymiles x2
]


@pytest.mark.parametrize("raw,effective", LIVE_FLEET_CADENCES)
def test_live_fleet_thresholds_unchanged_by_effective_cadence(raw, effective):
    """No device in the current fleet sees a different threshold."""
    device = {"poll_interval": raw, "effective_poll_interval": effective}
    legacy = max(5.0 * raw, 300.0)
    assert _threshold(device) == legacy == 300.0


def test_effective_cadence_wins_once_it_clears_the_floor():
    """The fix bites only above ~60s cadence — the case the raw value hides.

    raw 0.0 means "use the default"; a device actually running a 120s cadence
    would have been judged against 300s (2.5 polls) instead of 600s (5 polls).
    """
    device = {"poll_interval": 0.0, "effective_poll_interval": 120.0}
    assert max(5.0 * 0.0, 300.0) == 300.0  # what the old code computed
    assert _threshold(device) == 600.0


def test_falls_back_to_raw_when_effective_absent():
    """Reduced payloads (tests, pseudo rows) carry no effective_poll_interval."""
    assert _threshold({"poll_interval": 10.0}) == 300.0
    assert _threshold({"poll_interval": 120.0}) == 600.0


def test_zero_effective_does_not_shadow_a_real_raw_value():
    """effective 0.0/None must not override a configured raw cadence."""
    assert _threshold({"poll_interval": 120.0, "effective_poll_interval": 0.0}) == 600.0
    assert _threshold({"poll_interval": 120.0, "effective_poll_interval": None}) == 600.0


def test_data_is_fresh_uses_the_effective_cadence():
    """The freshness gate for power-based rules follows the same cadence."""
    now = NOON_UTC.timestamp()
    # 400s old: stale under the 300s floor, fresh under a 120s cadence (600s).
    stale_row = {
        "last_successful_poll_at": now - 400.0,
        "poll_interval": 0.0,
        "effective_poll_interval": 5.0,
    }
    fresh_row = {
        "last_successful_poll_at": now - 400.0,
        "poll_interval": 0.0,
        "effective_poll_interval": 120.0,
    }
    assert ad._data_is_fresh(stale_row, now) is False
    assert ad._data_is_fresh(fresh_row, now) is True
