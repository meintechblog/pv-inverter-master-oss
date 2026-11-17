"""Unit tests for /api/status honesty (2026-07-16 incident).

The old handler reported "solaredge": "connected" as soon as ANY device's
connection manager was in state connected — a dead SE30K among ten healthy
devices was invisible, and even a bare TCP connect without a single
successful poll counted as connected. The fixed contract:

    - healthy device := conn state "connected" AND >=1 successful poll
    - all healthy            -> "connected"
    - some healthy           -> "degraded"
    - none healthy           -> raw state of the first device (e.g. "quiesce")
    - no devices             -> "no_devices"
    - additive fields: devices_ok (int), devices_unhealthy (ids)
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from pv_inverter_master.context import AppContext, DeviceState
from pv_inverter_master.webapp.routes_core import status_handler


def _make_device(*, state: str, polled: bool) -> DeviceState:
    ds = DeviceState()
    ds.conn_mgr = SimpleNamespace(state=SimpleNamespace(value=state))
    ds.last_successful_poll_at = time.time() if polled else None
    return ds


def _make_app(devices: dict) -> web.Application:
    ctx = AppContext()
    ctx.devices = devices
    ctx.venus_mqtt_connected = True
    app = web.Application()
    app["app_ctx"] = ctx
    return app


async def _status(devices: dict) -> dict:
    req = make_mocked_request("GET", "/api/status", app=_make_app(devices))
    resp = await status_handler(req)
    assert resp.status == 200
    return json.loads(resp.body.decode())


async def test_all_healthy_reports_connected():
    data = await _status({
        "a": _make_device(state="connected", polled=True),
        "b": _make_device(state="connected", polled=True),
    })
    assert data["solaredge"] == "connected"
    assert data["devices_ok"] == 2
    assert data["devices_unhealthy"] == []


async def test_dead_device_among_healthy_reports_degraded():
    """The 2026-07-16 incident shape: SE30K in quiesce, siblings fine."""
    data = await _status({
        "se30k": _make_device(state="quiesce", polled=False),
        "hoymiles": _make_device(state="connected", polled=True),
    })
    assert data["solaredge"] == "degraded"
    assert data["devices_ok"] == 1
    assert data["devices_unhealthy"] == ["se30k"]


async def test_connected_state_without_poll_is_not_healthy():
    """Bare TCP connect (no successful poll yet) must not count as healthy."""
    data = await _status({
        "se30k": _make_device(state="connected", polled=False),
    })
    assert data["solaredge"] != "connected"
    assert data["devices_ok"] == 0
    assert data["devices_unhealthy"] == ["se30k"]


async def test_none_healthy_reports_first_raw_state():
    data = await _status({
        "se30k": _make_device(state="quiesce", polled=False),
        "sungrow": _make_device(state="reconnecting", polled=False),
    })
    assert data["solaredge"] == "quiesce"
    assert data["devices_ok"] == 0
    assert set(data["devices_unhealthy"]) == {"se30k", "sungrow"}


async def test_no_devices_reports_no_devices():
    data = await _status({})
    assert data["solaredge"] == "no_devices"
    assert data["devices_ok"] == 0
    assert data["devices_unhealthy"] == []


async def test_device_without_conn_mgr_is_skipped():
    """Devices not yet started (conn_mgr None) don't crash or count."""
    ds = DeviceState()
    ds.conn_mgr = None
    data = await _status({"pending": ds})
    assert data["solaredge"] == "no_devices"
    assert data["devices_unhealthy"] == []
