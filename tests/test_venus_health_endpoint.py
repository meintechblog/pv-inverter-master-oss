"""Tests for /api/venus/health (GON Venus Health Dashboard).

Covers the JSON shape contract documented in routes_venus.py and the
``_compute_missing`` helper that builds the German hint list for the
"Steuerung & Regelung" card.

Like ``test_health_endpoint.py`` these tests bypass aiohttp's
``TestServer`` and exercise the handler with ``make_mocked_request`` —
the hot path does no IO except a single ``state_file.load_state`` call
which is patched per-test.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from pv_inverter_master.context import AppContext
from pv_inverter_master.state_file import PersistedState
from pv_inverter_master.webapp.routes_venus import (
    _compute_missing,
    venus_health_handler,
)


def _venus_cfg(host: str = "192.0.2.123", portal_id: str = "aabbccdd0007"):
    return SimpleNamespace(
        venus=SimpleNamespace(host=host, port=1883, portal_id=portal_id, name=""),
        virtual_inverter=SimpleNamespace(name="Fronius Proxy"),
    )


async def _call(ctx: AppContext, config) -> dict:
    """Invoke the handler via a mocked aiohttp request and return parsed JSON."""
    app = web.Application()
    app["app_ctx"] = ctx
    app["config"] = config
    req = make_mocked_request("GET", "/api/venus/health", app=app)
    with patch(
        "pv_inverter_master.webapp.routes_venus.state_file.load_state",
        return_value=PersistedState(),
    ):
        resp = await venus_health_handler(req)
    assert resp.status == 200
    return json.loads(resp.body.decode())


# ---------------------------------------------------------------------------
# Shape contract
# ---------------------------------------------------------------------------


async def test_health_shape_when_mqtt_disconnected():
    """All sections present with sensible defaults when MQTT is offline."""
    ctx = AppContext()
    ctx.venus_mqtt_connected = False
    ctx.venus_settings = None
    ctx.venus_connected_since_ts = None

    data = await _call(ctx, _venus_cfg())

    assert set(data.keys()) == {"mqtt", "system", "coupling", "peers", "control", "ats"}

    # mqtt block
    assert data["mqtt"]["configured"] is True
    assert data["mqtt"]["connected"] is False
    assert data["mqtt"]["host"] == "192.0.2.123"
    assert data["mqtt"]["portal_id"] == "aabbccdd0007"
    assert data["mqtt"]["connected_since_ts"] is None

    # system block — all None when nothing was observed
    assert data["system"] == {"serial": None, "type": None, "firmware": None, "hostname": None}

    # coupling block — not registered, everything None
    assert data["coupling"]["registered"] is False
    assert data["coupling"]["device_instance"] is None
    assert data["coupling"]["product_name"] is None
    assert data["coupling"]["connection_string"] is None
    assert data["coupling"]["position"] is None
    assert data["coupling"]["position_label"] is None

    # peers block — all four entries with present=False
    for peer in ("grid_meter", "vebus", "solarcharger", "battery"):
        assert data["peers"][peer]["present"] is False
        assert data["peers"][peer]["product_name"] is None
    assert data["peers"]["grid_meter"]["ac_power_w"] is None

    # control block — hints include both grid_meter and multiplus
    missing = data["control"]["missing_for_active_regulation"]
    assert "Grid-Meter (Carlo Gavazzi / Shelly EM) verbinden" in missing
    assert "Multiplus per VE.Bus anschließen" in missing
    assert data["control"]["last_throttle_command_ts"] is None
    assert data["control"]["last_throttle_pct"] is None


async def test_health_with_full_peer_state():
    """A populated venus_settings dict yields registered=True + correct hints."""
    ctx = AppContext()
    ctx.venus_mqtt_connected = True
    ctx.venus_connected_since_ts = 1_700_000_000.0
    ctx.venus_settings = {
        "pv_limit_w": 17000,
        "system": {
            "serial": "aabbccdd0007",
            "type": "Cerbo GX",
            "firmware": "v3.50",
            "hostname": "venus-knausi",
        },
        "peers": {
            "pvinverter": {
                "20": {
                    "device_instance": 20,
                    "product_name": "SunSpec PV-Inverter",
                    "custom_name": "Fronius Proxy",
                    "connection_string": "192.0.2.191 - 126 (sunspec)",
                    "last_ts": 1_700_000_001.0,
                },
            },
            "vebus": {},
            "grid_meter": {
                "30": {
                    "device_instance": 30,
                    "product_name": "Carlo Gavazzi EM24",
                    "ac_power_w": -1234.5,
                    "last_ts": 1_700_000_002.0,
                },
            },
            "solarcharger": {},
            "battery": {},
        },
        "limiter": {
            "enabled": True,
            "supported": True,
            "custom_name": "Fronius Proxy",
            "position": 1,
            "inverter_index": 0,
        },
    }

    data = await _call(ctx, _venus_cfg())

    assert data["mqtt"]["connected"] is True
    assert data["mqtt"]["connected_since_ts"] == 1_700_000_000.0

    assert data["system"]["serial"] == "aabbccdd0007"
    assert data["system"]["firmware"] == "v3.50"

    assert data["coupling"]["registered"] is True
    assert data["coupling"]["device_instance"] == 20
    assert data["coupling"]["product_name"] == "SunSpec PV-Inverter"
    assert data["coupling"]["connection_string"] == "192.0.2.191 - 126 (sunspec)"
    assert data["coupling"]["position"] == 1
    assert data["coupling"]["position_label"] == "AC-Ausgang"
    assert data["coupling"]["limiter_supported"] is True
    assert data["coupling"]["limiter_enabled"] is True
    assert data["coupling"]["power_limit_w"] == 17000

    assert data["peers"]["grid_meter"]["present"] is True
    assert data["peers"]["grid_meter"]["product_name"] == "Carlo Gavazzi EM24"
    assert data["peers"]["grid_meter"]["ac_power_w"] == -1234.5
    assert data["peers"]["vebus"]["present"] is False

    missing = data["control"]["missing_for_active_regulation"]
    assert "Multiplus per VE.Bus anschließen" in missing
    # Without a Multiplus the limiter hints must NOT show — they depend on vebus.
    assert "Feed-in-Limit oder ESS-Policy aktivieren" not in missing
    assert "ESS-Assistent auf Multiplus laden" not in missing
    # Grid meter IS present → no grid hint
    assert all("Grid-Meter" not in m for m in missing)


# ---------------------------------------------------------------------------
# _compute_missing — exhaustive branch coverage
# ---------------------------------------------------------------------------


def test_compute_missing_empty_state_lists_grid_and_multiplus():
    missing = _compute_missing({}, {}, PersistedState())
    assert missing[0] == "Grid-Meter (Carlo Gavazzi / Shelly EM) verbinden"
    assert "Multiplus per VE.Bus anschließen" in missing


def test_compute_missing_only_vebus_present_asks_for_battery_and_ess():
    peers = {"vebus": {"0": {"product_name": "Multiplus-II"}}}
    limiter = {"enabled": False, "supported": False}
    missing = _compute_missing(peers, limiter, PersistedState())
    # Grid is missing → first item
    assert missing[0] == "Grid-Meter (Carlo Gavazzi / Shelly EM) verbinden"
    assert "Batterie/MPPT-Verbindung prüfen" in missing
    assert "ESS-Assistent auf Multiplus laden" in missing
    assert "Feed-in-Limit oder ESS-Policy aktivieren" in missing


def test_compute_missing_grid_only_no_battery_hint():
    """Without vebus, battery hint is NOT emitted (depends on vebus presence)."""
    peers = {"grid_meter": {"30": {}}}
    missing = _compute_missing(peers, {}, PersistedState())
    assert "Multiplus per VE.Bus anschließen" in missing
    assert "Batterie/MPPT-Verbindung prüfen" not in missing
    # Grid present → no grid hint
    assert all("Grid-Meter" not in m for m in missing)


def test_compute_missing_full_stack_returns_empty():
    """Grid + vebus + battery + limiter enabled+supported → no hints."""
    peers = {
        "grid_meter": {"30": {}},
        "vebus": {"0": {}},
        "battery": {"1": {}},
    }
    limiter = {"enabled": True, "supported": True}
    missing = _compute_missing(peers, limiter, PersistedState())
    assert missing == []


def test_compute_missing_limiter_unknown_does_not_emit_hint():
    """limiter.enabled=None means 'not observed yet' — must not trigger the hint."""
    peers = {"grid_meter": {"30": {}}, "vebus": {"0": {}}, "battery": {"1": {}}}
    limiter = {"enabled": None, "supported": None}
    missing = _compute_missing(peers, limiter, PersistedState())
    assert missing == []
