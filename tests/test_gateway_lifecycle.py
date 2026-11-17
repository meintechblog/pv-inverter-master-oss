"""Regression tests for gateway lifecycle, serialization and network guards.
Only mocks and 127.0.0.1 fakes; the host 192.0.2.10 (TEST-NET-1) is a KEY only."""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pv_inverter_master import modbus_gateway
from pv_inverter_master.modbus_gateway import (
    ModbusGateway,
    get_gateway,
    prune_idle_gateways,
)
from tests.test_gateway_isolation import LAN_IP, FakeClient, fake_client, resolver, server  # noqa: F401


# ------------------------------------------------------------------ Queued waiters
async def test_prune_keeps_gateway_with_queued_waiter():
    gw = get_gateway(LAN_IP)
    await gw._lock.acquire()
    waiter = asyncio.create_task(gw._acquire(5))
    await asyncio.sleep(0)            # waiter is queued
    gw._lock.release()                # lock not locked() any more, waiter not yet running
    assert not gw._lock.locked()
    assert prune_idle_gateways() == 0
    assert get_gateway(LAN_IP) is gw
    await waiter
    gw._lock.release()
    # a waiter that gave up (timeout) does not pin the gateway forever
    await gw._lock.acquire()
    with pytest.raises(modbus_gateway.GatewayWaitTimeout):
        await gw._acquire(0.01)
    gw._lock.release()
    assert prune_idle_gateways() == 1


async def test_prune_keeps_failure_counters():
    gw = get_gateway(LAN_IP)
    gw._probe_silent = 2
    assert prune_idle_gateways() == 0
    gw._probe_silent = 0
    gw._probe_breaker_trips = 1
    assert prune_idle_gateways() == 0
    gw._probe_breaker_trips = 0
    assert prune_idle_gateways() == 1


# ------------------------------------------------------------------------ Connection target
async def test_connection_target_is_the_keyed_address(fake_client, resolver):
    gw = get_gateway("wr.test")
    assert gw.host == LAN_IP
    async with gw.probe_transaction(502):
        pass
    assert fake_client.instances[0].args[0] == LAN_IP   # never the host name again
    resolver.clear()
    assert get_gateway("WR.test") is gw


# ------------------------------------------------------------------------ Shutdown
class SlowClient(FakeClient):
    events: list[str] = []

    async def read_holding_registers(self, address, count=1, **kw):
        SlowClient.events.append("read_start")
        await asyncio.sleep(0.15)
        SlowClient.events.append("read_done")
        return MagicMock(isError=lambda: False)

    def close(self):
        SlowClient.events.append("close")
        super().close()


async def test_cancelled_probe_does_not_close_mid_request(monkeypatch):
    SlowClient.instances, SlowClient.events = [], []
    monkeypatch.setattr(modbus_gateway, "AsyncModbusTcpClient", SlowClient)
    gw = get_gateway(LAN_IP)

    async def probe():
        async with gw.probe_transaction(502) as t:
            await t.read_holding(0, 2, 1)

    task = asyncio.create_task(probe())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # the FIN came only after the device had answered ...
    assert SlowClient.events == ["read_start", "read_done", "close"]
    # ... and the gateway rests before the next session
    assert not gw.busy


async def test_cancel_with_stuck_request_quiesces_host(monkeypatch):
    SlowClient.instances, SlowClient.events = [], []
    monkeypatch.setattr(modbus_gateway, "AsyncModbusTcpClient", SlowClient)
    monkeypatch.setattr(modbus_gateway, "CANCEL_DRAIN_S", 0.02)
    gw = get_gateway(LAN_IP)

    async def probe():
        async with gw.probe_transaction(502) as t:
            await t.read_holding(0, 2, 1)

    task = asyncio.create_task(probe())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert gw.quiesced  # request never ended: no further session for a while
    assert gw._probe_silent == 1


# --------------------------------------------------- Close after drain timeout
async def test_close_after_timeout_never_closes_without_lock():
    gw = ModbusGateway("127.0.0.1", 1, min_gap_s=0.0)
    client = MagicMock(connected=True)
    gw.adopt_client(client)
    release = asyncio.Event()

    async def holder():
        async with gw.transaction():
            await release.wait()

    task = asyncio.create_task(holder())
    await asyncio.sleep(0.01)
    await gw.close(drain_timeout=0.05)          # returns, does NOT close yet
    client.close.assert_not_called()
    release.set()
    await task
    await gw.wait_closed()
    client.close.assert_called_once()
    assert not gw.connected


# --------------------------------------------- Cancelled poll task vs. connect
async def test_cancelled_poll_task_does_not_abort_a_running_connect():
    from pv_inverter_master.connection import ConnectionManager
    from pv_inverter_master.context import AppContext, DeviceState
    from pv_inverter_master.device_registry import _device_poll_loop

    done = asyncio.Event()

    async def connect():
        await asyncio.sleep(0.15)
        done.set()

    plugin = MagicMock()
    plugin.connect = connect
    plugin._client = None
    state = DeviceState(conn_mgr=ConnectionManager(poll_interval=1.0), poll_lock=asyncio.Lock())
    task = asyncio.create_task(
        _device_poll_loop("d", plugin, state, 1.0, AsyncMock(), AppContext()))
    await asyncio.sleep(0.03)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(done.wait(), 2)   # the connect block ran to its end


# -------------------------------------------------- Dynamic Power Control init
def _arm_dpc_registers(srv) -> None:
    """Fill the fake SE30K so the FULL Dynamic-Power-Control init succeeds."""
    from pv_inverter_master.plugins import solaredge as se

    srv.store.update({40000: 0x5375, 40001: 0x6E53})
    for i, r in enumerate(se._f32_to_regs(30000.0, True)):
        srv.store[se.DPC_MAX_ACTIVE_POWER_REG + i] = r
    srv.store[se.ADV_PWR_CONTROL_EN_REG], srv.store[se.ADV_PWR_CONTROL_EN_REG + 1] = 0, 1
    srv.store[se.REACTIVE_PWR_CONFIG_REG], srv.store[se.REACTIVE_PWR_CONFIG_REG + 1] = 0, 4
    srv.store[se.LEGACY_LIMIT_REG] = 100


async def test_full_dpc_init_against_filled_fake_server(server):
    from pv_inverter_master.plugins.solaredge import DPC_ENABLE_REG, SolarEdgePlugin

    _arm_dpc_registers(server)
    plugin = SolarEdgePlugin("127.0.0.1", server.port, dynamic_power_control=True)
    await plugin.connect()
    assert plugin._dynamic_ok is True
    assert server.store[DPC_ENABLE_REG] == 1
    assert server.max_in_flight <= 1 and server.max_connections == 1
    await plugin.close()


async def test_connect_race_with_dpc_enabled_one_session(server):
    """Same race as the DPC-off test, but with the complete DPC init and DPC writes."""
    from pv_inverter_master.plugins.solaredge import SolarEdgePlugin

    _arm_dpc_registers(server)
    a = SolarEdgePlugin("127.0.0.1", server.port, dynamic_power_control=True)
    b = SolarEdgePlugin("127.0.0.1", server.port, dynamic_power_control=True)

    async def ui_test():
        async with a.gateway.probe_transaction(server.port) as t:
            return await t.read_holding(40000, 2, 1)

    await asyncio.gather(
        a.connect(), b.connect(), ui_test(), a.poll(), b.poll(),
        a.write_power_limit(True, 40.0), b.write_power_limit(True, 60.0), ui_test(),
    )
    assert server.max_in_flight <= 1
    assert server.max_connections == 1
    assert server.total_connections == 1
    assert a._dynamic_ok or b._dynamic_ok
    await a.close()


async def test_stop_during_full_dpc_init_finishes_the_block(server, monkeypatch):
    from pv_inverter_master import device_registry as dr
    from pv_inverter_master.config import Config, InverterEntry
    from pv_inverter_master.context import AppContext
    from pv_inverter_master.plugins.solaredge import DPC_ENABLE_REG, SolarEdgePlugin

    _arm_dpc_registers(server)
    server.response_delay_s = 0.03
    monkeypatch.setattr(dr, "READ_DRAIN_TIMEOUT_S", 0.05)
    monkeypatch.setattr(dr, "STOP_HARD_DRAIN_S", 0.1)   # force the hard-cancel path
    entry = InverterEntry(id="d", host="127.0.0.1", port=server.port, enabled=True, type="solaredge")
    registry = dr.DeviceRegistry(AppContext(), Config(inverters=[entry]), on_poll_success=AsyncMock())
    plugin = SolarEdgePlugin("127.0.0.1", server.port, dynamic_power_control=True)
    with patch("pv_inverter_master.device_registry.plugin_factory", return_value=plugin):
        await registry.start_device("d")
        deadline = time.monotonic() + 3
        while not server.requests and time.monotonic() < deadline:
            await asyncio.sleep(0.005)
        assert server.requests
        await asyncio.wait_for(registry.stop_device("d"), 30)
    assert server.aborted_in_flight == 0
    assert server.max_in_flight <= 1
    assert server.store[DPC_ENABLE_REG] == 1        # the DPC block ran to its end
    assert not plugin.gateway.connected


# ------------------------------------------------------------------- Probe locking
def _listener():
    import socket
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    srv.setblocking(False)
    return srv


def _queued_connections(srv) -> int:
    n = 0
    while True:
        try:
            c, _ = srv.accept()
        except (BlockingIOError, OSError):
            return n
        c.close()
        n += 1


def test_anomaly_probe_does_not_connect_while_proxy_holds_the_lock(tmp_path, monkeypatch):
    import fcntl
    import os
    from pv_inverter_master.monitoring import anomaly_detector as ad

    lock = tmp_path / "instance.lock"
    monkeypatch.setenv("PV_INVERTER_MASTER_LOCK", str(lock))
    holder = os.open(lock, os.O_RDWR | os.O_CREAT, 0o666)   # "the proxy service"
    fcntl.flock(holder, fcntl.LOCK_EX)
    srv = _listener()
    try:
        accepted, own_ip = ad._tcp_accept_probe("127.0.0.1", srv.getsockname()[1], 1.0)
        assert _queued_connections(srv) == 0           # no second session next to the proxy
        assert accepted is True and own_ip == "127.0.0.1"  # evidence continues via conntrack
    finally:
        srv.close()
        os.close(holder)


def test_anomaly_probe_connects_when_proxy_is_down_and_releases_the_lock(tmp_path, monkeypatch):
    from pv_inverter_master import instance_lock
    from pv_inverter_master.monitoring import anomaly_detector as ad

    lock = tmp_path / "instance.lock"
    monkeypatch.setenv("PV_INVERTER_MASTER_LOCK", str(lock))
    srv = _listener()
    try:
        accepted, own_ip = ad._tcp_accept_probe("127.0.0.1", srv.getsockname()[1], 1.0)
        assert accepted is True and own_ip == "127.0.0.1"
        assert _queued_connections(srv) == 1
    finally:
        srv.close()
    assert instance_lock.running_instance_pid(str(lock)) is None   # lock released again


def test_anomaly_probe_unusable_lock_path_means_no_connect(tmp_path, monkeypatch):
    from pv_inverter_master.monitoring import anomaly_detector as ad

    monkeypatch.setenv("PV_INVERTER_MASTER_LOCK", str(tmp_path / "missing-dir" / "x.lock"))
    srv = _listener()
    try:
        ad._tcp_accept_probe("127.0.0.1", srv.getsockname()[1], 1.0)
        assert _queued_connections(srv) == 0
    finally:
        srv.close()
