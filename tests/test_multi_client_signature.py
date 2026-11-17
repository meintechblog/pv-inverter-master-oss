"""MULTI_CLIENT evidence pass (incident 2026-07-16).

A session-limited inverter (SolarEdge) blocked by a FOREIGN Modbus client
looks exactly like a firmware wedge from the poller's side (TCP up, Modbus
silent). The enrichment pass retypes a corroborated MODBUS_HANG to
MULTI_CLIENT when (a) a fresh TCP connect is accepted AND (b) the fleet
conntrack measurement shows established sessions from a foreign source.
"""
from __future__ import annotations

from typing import Any

import pv_inverter_master.monitoring.anomaly_detector as ad
from pv_inverter_master.monitoring.anomaly_detector import (
    ALERTABLE_TYPES,
    MODBUS_HANG,
    MULTI_CLIENT,
    DetectorConfig,
    _enrich_multi_client,
)


def _hang_anomaly(device_id: str = "se") -> dict[str, Any]:
    return {
        "device_id": device_id,
        "type": MODBUS_HANG,
        "severity": "critical",
        "detail": "solaredge Modbus hang: No response",
        "location_id": "loc1",
    }


def _devices(device_id: str = "se") -> dict[str, Any]:
    return {
        "devices": [
            {"id": device_id, "type": "solaredge", "host": "192.0.2.18", "port": 1502}
        ]
    }


def _cfg() -> DetectorConfig:
    return DetectorConfig(http_timeout_s=0.1)


def test_foreign_client_retypes_to_multi_client():
    """TCP accepted + foreign conntrack source => MULTI_CLIENT naming the src."""
    out = _enrich_multi_client(
        [_hang_anomaly()],
        _devices(),
        _cfg(),
        tcp_probe=lambda h, p, t: (True, "192.0.2.191"),
        conntrack=lambda h, own, t: ["198.51.100.102"],
    )
    assert out[0]["type"] == MULTI_CLIENT
    assert out[0]["severity"] == "critical"
    assert "198.51.100.102" in out[0]["detail"]
    assert "kill the foreign client" in out[0]["detail"]


def test_no_conntrack_coverage_keeps_hang_with_runbook_hint():
    """Without coverage stay MODBUS_HANG but demand a client check
    BEFORE any reset — the responder must not power-cycle past a killable
    client."""
    out = _enrich_multi_client(
        [_hang_anomaly()],
        _devices(),
        _cfg(),
        tcp_probe=lambda h, p, t: (True, "192.0.2.191"),
        conntrack=lambda h, own, t: None,
    )
    assert out[0]["type"] == MODBUS_HANG
    assert "BEFORE any reset" in out[0]["detail"]


def test_conntrack_empty_is_not_wedge_proof():
    """An empty query result is indistinguishable from
    missing watcher coverage (a field case had 27 connections invisible to it) — it must
    NEVER read as 'no foreign client'. Same runbook hint as the None case."""
    out = _enrich_multi_client(
        [_hang_anomaly()],
        _devices(),
        _cfg(),
        tcp_probe=lambda h, p, t: (True, "192.0.2.191"),
        conntrack=lambda h, own, t: [],
    )
    assert out[0]["type"] == MODBUS_HANG
    assert "BEFORE any reset" in out[0]["detail"]
    assert "NO foreign client" not in out[0]["detail"]


def test_hostile_host_skipped_no_probe():
    """A host that could break out of the Flux string
    literal is rejected before ANY probe runs."""
    calls: list[str] = []
    devices = {
        "devices": [
            {"id": "se", "type": "solaredge", "host": 'x" or true "', "port": 1502}
        ]
    }
    out = _enrich_multi_client(
        [_hang_anomaly()],
        devices,
        _cfg(),
        tcp_probe=lambda h, p, t: calls.append(h) or (True, None),
        conntrack=lambda h, own, t: ["198.51.100.102"],
    )
    assert out[0]["type"] == MODBUS_HANG
    assert calls == []


def test_tcp_refused_leaves_anomaly_untouched():
    calls: list[str] = []
    out = _enrich_multi_client(
        [_hang_anomaly()],
        _devices(),
        _cfg(),
        tcp_probe=lambda h, p, t: (False, None),
        conntrack=lambda h, own, t: calls.append(h) or ["198.51.100.102"],
    )
    assert out[0]["type"] == MODBUS_HANG
    assert "BEFORE any reset" not in out[0]["detail"]
    assert calls == []  # conntrack not queried when the port is dead


def test_own_ip_is_excluded_from_foreign_check():
    """The probe's own source IP is handed to the conntrack seam for
    exclusion — our own poller sessions never count as foreign."""
    seen_own: list[set] = []

    def conntrack(host, own_ips, timeout):
        seen_own.append(own_ips)
        return []

    _enrich_multi_client(
        [_hang_anomaly()],
        _devices(),
        _cfg(),
        tcp_probe=lambda h, p, t: (True, "203.0.113.5"),
        conntrack=conntrack,
    )
    assert seen_own == [{"203.0.113.5"}]


def test_non_hang_anomalies_untouched_and_no_probe():
    calls: list[str] = []
    anomaly = {"device_id": "se", "type": "STALE_POLL", "severity": "warning", "detail": "x"}
    out = _enrich_multi_client(
        [anomaly],
        _devices(),
        _cfg(),
        tcp_probe=lambda h, p, t: calls.append(h) or (True, None),
        conntrack=lambda h, own, t: ["198.51.100.102"],
    )
    assert out[0]["type"] == "STALE_POLL"
    assert calls == []


def test_multi_client_is_alertable_error():
    assert MULTI_CLIENT in ALERTABLE_TYPES
    assert ad._alert_severity(MULTI_CLIENT) == "error"


def test_cooldown_retype_shares_incident_stream_no_false_recovery():
    """Deploy-transition shape: an ACTIVE MODBUS_HANG
    state entry + a retyped MULTI_CLIENT anomaly must NOT produce a
    'recovered' — same aliased key, one incident stream. The diagnosis
    upgrade re-alerts once despite the cooldown (actionable src_ip)."""
    state = ad.DetectorState()
    state.entries["se:MODBUS_HANG"] = ad.StateEntry(
        first_seen=1000.0, last_alerted=1000.0, resolved=False, last_type=MODBUS_HANG
    )
    anomaly = _hang_anomaly()
    anomaly["type"] = MULTI_CLIENT
    alerts, recoveries = ad.apply_cooldown([anomaly], state, DetectorConfig(), 2000.0)
    assert recoveries == []
    assert alerts == [anomaly]  # upgrade re-alert, cooldown notwithstanding
    assert state.entries["se:MODBUS_HANG"].last_type == MULTI_CLIENT
    assert "se:MULTI_CLIENT" not in state.entries


def test_cooldown_flap_downgrade_stays_suppressed():
    """Conntrack coverage drops (MULTI_CLIENT -> plain MODBUS_HANG next run):
    information LOSS — no new alert, no recovery, no key churn."""
    state = ad.DetectorState()
    cfg = DetectorConfig()
    upgraded = _hang_anomaly()
    upgraded["type"] = MULTI_CLIENT
    alerts, recoveries = ad.apply_cooldown([upgraded], state, cfg, 1000.0)
    assert len(alerts) == 1 and recoveries == []
    plain = _hang_anomaly()
    alerts, recoveries = ad.apply_cooldown([plain], state, cfg, 2000.0)
    assert alerts == [] and recoveries == []
    # Flap back up: coverage returns with the SAME diagnosis already alerted
    # in run 1 (last_type stays MULTI_CLIENT through the suppressed dip) —
    # nothing new for the operator, the flap is completely silent. This is
    # the anti-storm property the alias exists for.
    upgraded2 = _hang_anomaly()
    upgraded2["type"] = MULTI_CLIENT
    alerts, recoveries = ad.apply_cooldown([upgraded2], state, cfg, 3000.0)
    assert alerts == [] and recoveries == []
    assert list(state.entries.keys()) == ["se:MODBUS_HANG"]


def test_cooldown_real_recovery_resolves_family_once():
    """Device genuinely recovers after a MULTI_CLIENT run: exactly one
    recovery for the shared family key."""
    state = ad.DetectorState()
    cfg = DetectorConfig()
    upgraded = _hang_anomaly()
    upgraded["type"] = MULTI_CLIENT
    ad.apply_cooldown([upgraded], state, cfg, 1000.0)
    alerts, recoveries = ad.apply_cooldown([], state, cfg, 2000.0)
    assert alerts == []
    assert len(recoveries) == 1
    assert recoveries[0]["device_id"] == "se"


def test_state_entry_last_type_roundtrip_and_legacy_tolerance():
    entry = ad.StateEntry(first_seen=1.0, last_type=MULTI_CLIENT)
    assert ad.StateEntry.from_dict(entry.to_dict()).last_type == MULTI_CLIENT
    legacy = ad.StateEntry.from_dict({"first_seen": 1.0, "resolved": False})
    assert legacy.last_type == ""


def test_run_detection_without_probes_skips_enrichment(tmp_path, monkeypatch):
    """Opt-in wiring: callers that don't inject probes (all pre-existing
    tests) never reach the real socket/Influx code, even with a hang in the
    fixture data."""
    def boom(*a, **kw):  # noqa: ANN002, ANN003
        raise AssertionError("real probe must not run")

    monkeypatch.setattr(ad, "_tcp_accept_probe", boom)
    monkeypatch.setattr(ad, "_conntrack_foreign_sources", boom)
    cfg = DetectorConfig(state_path=tmp_path / "state.json", http_timeout_s=0.1)
    state = ad.load_state(cfg.state_path)
    devices = {
        "devices": [
            {
                "id": "se",
                "type": "solaredge",
                "host": "192.0.2.18",
                "port": 1502,
                "enabled": True,
                "connection_state": "reconnecting",
                "power_w": 0,
                "location_id": "loc1",
                "poll_interval": 30.0,
                "last_successful_poll_at": None,
                "last_poll_error": "No response from slave",
                "source_stale": False,
            }
        ]
    }
    result = ad.run_detection(
        cfg,
        state,
        fetch=lambda url, t: devices if "devices" in url else {"locations": []},
        post=lambda url, payload, t: True,
        sleep=lambda s: None,
        now=lambda: 1_766_000_000.0,
    )
    types = {a["type"] for a in result["anomalies"]}
    assert MODBUS_HANG in types
    assert MULTI_CLIENT not in types
