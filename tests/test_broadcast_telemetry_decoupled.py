"""Regression: telemetry logging must not depend on a connected browser.

Bug (2026-05-30): the InfluxDB/MQTT enqueue lived behind the
``if not clients: return`` guard in ``broadcast_device_snapshot`` /
``broadcast_virtual_snapshot``. Closing the dashboard tab silently stopped
all time-series logging for every device — surfaced as every inverter's
power "freezing" at its last value and InfluxDB going dark whenever nobody
was watching the dashboard.
"""
from __future__ import annotations

import asyncio

import pytest
from aiohttp import web

from pv_inverter_master.config import Config
from pv_inverter_master.context import AppContext
from pv_inverter_master.webapp import (
    broadcast_device_snapshot,
    broadcast_virtual_snapshot,
)


def _make_app(with_clients: bool) -> tuple[web.Application, AppContext]:
    app_ctx = AppContext()
    app_ctx.influx_queue = asyncio.Queue(maxsize=100)
    app_ctx.mqtt_pub_queue = asyncio.Queue(maxsize=100)
    app = web.Application()
    app["app_ctx"] = app_ctx
    app["config"] = Config()
    # An empty set models "dashboard closed"; broadcasts must still log.
    app["ws_clients"] = set()
    return app, app_ctx


@pytest.mark.asyncio
async def test_device_snapshot_logs_without_ws_clients():
    app, app_ctx = _make_app(with_clients=False)
    snapshot = {"ts": 1.0, "inverter": {"ac_power_w": 206.0}}

    await broadcast_device_snapshot(app, "dev1", snapshot)

    assert app_ctx.influx_queue.qsize() == 1, "device telemetry must reach InfluxDB with no browser open"
    assert app_ctx.mqtt_pub_queue.qsize() == 1
    msg = app_ctx.influx_queue.get_nowait()
    assert msg["type"] == "device"
    assert msg["device_id"] == "dev1"


@pytest.mark.asyncio
async def test_virtual_snapshot_logs_without_ws_clients():
    app, app_ctx = _make_app(with_clients=False)

    await broadcast_virtual_snapshot(app)

    assert app_ctx.influx_queue.qsize() == 1, "aggregate telemetry must reach InfluxDB with no browser open"
    msg = app_ctx.influx_queue.get_nowait()
    assert msg["type"] == "virtual"
