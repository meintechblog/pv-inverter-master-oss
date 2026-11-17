"""Web-UI Modbus buttons go through the host gateway (no second session)."""
from __future__ import annotations

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from pv_inverter_master import modbus_gateway
from pv_inverter_master.modbus_gateway import get_gateway
from pv_inverter_master.plugins.solaredge import SolarEdgePlugin
from pv_inverter_master.webapp.routes_config import config_test_handler
from pv_inverter_master.webapp.routes_discovery import sungrow_probe_handler
from tests.fake_modbus_server import FakeSingleSessionServer


@pytest.fixture
async def server():
    srv = await FakeSingleSessionServer().start()
    srv.store.update({40000: 0x5375, 40001: 0x6E53, 5000: 0x0123})
    yield srv
    for gw in list(modbus_gateway._GATEWAYS.values()):
        await gw.close()
    await srv.stop()


@pytest.fixture
async def client():
    app = web.Application()
    app.router.add_post("/api/config/test", config_test_handler)
    app.router.add_post("/api/sungrow/probe", sungrow_probe_handler)
    async with TestClient(TestServer(app)) as c:
        yield c


async def test_config_test_button_reuses_live_session(server, client):
    plugin = SolarEdgePlugin("127.0.0.1", server.port, dynamic_power_control=False)
    await plugin.connect()
    assert server.total_connections == 1
    # button hammered while the plugin polls
    bodies = [{"host": "127.0.0.1", "port": server.port, "unit_id": 1}] * 6
    resps = await asyncio.gather(
        *[client.post("/api/config/test", json=b) for b in bodies],
        *[plugin.poll() for _ in range(6)],
    )
    for r in resps[:6]:
        assert (await r.json())["success"] is True
    assert server.total_connections == 1
    assert server.max_connections == 1 and server.max_in_flight == 1
    assert plugin.gateway.connected  # test button did not close the production session


async def test_config_test_button_on_unused_host_opens_one_temp_session(server, client):
    r = await client.post(
        "/api/config/test", json={"host": "127.0.0.1", "port": server.port, "unit_id": 1}
    )
    assert (await r.json())["success"] is True
    await asyncio.sleep(0.05)
    assert server.total_connections == 1 and server.connections_now == 0


async def test_config_test_button_refused_during_quiesce(server, client):
    plugin = SolarEdgePlugin("127.0.0.1", server.port, dynamic_power_control=False)
    await plugin.connect()
    plugin.gateway.enter_quiesce(60)
    before = len(server.requests)
    r = await client.post(
        "/api/config/test", json={"host": "127.0.0.1", "port": server.port, "unit_id": 1}
    )
    body = await r.json()
    assert body["success"] is False and "quiesce" in body["error"]
    assert len(server.requests) == before


async def test_sungrow_probe_uses_device_id_and_gateway(server, client):
    r = await client.post(
        "/api/sungrow/probe", json={"host": "127.0.0.1", "port": server.port, "unit_id": 1}
    )
    body = await r.json()
    assert body["success"] is True
    assert body["model"] == "Sungrow (code 0x0123)"
    assert (4, 5000) in server.requests  # FC04, input registers
    assert get_gateway("127.0.0.1").max_in_flight == 1
