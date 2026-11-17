"""Gateway invariants against a fake single-session server on 127.0.0.1.

Asserted everywhere: never more than 1 TCP connection per host, never more than
1 Modbus transaction in flight, minimum gap kept (SE30K wedge protection).
"""
from __future__ import annotations

import asyncio

import pytest

from pv_inverter_master import modbus_gateway
from pv_inverter_master.modbus_gateway import (
    SUPERSEDED,
    GatewayPortConflict,
    GatewayQuiesced,
    GatewayWaitTimeout,
    ModbusConnectError,
    ModbusGateway,
    ModbusNotConnected,
    find_gateway,
    get_gateway,
    is_host_claimed,
)
from pv_inverter_master.plugins.solaredge import SolarEdgePlugin
from tests.fake_modbus_server import FakeSingleSessionServer


@pytest.fixture
async def server():
    srv = await FakeSingleSessionServer().start()
    yield srv
    # Server shutdown waits for open sessions: drop every gateway session first.
    for gw in list(modbus_gateway._GATEWAYS.values()):
        await gw.close()
    await srv.stop()


def _gw(srv, min_gap_s=0.0) -> ModbusGateway:
    gw = ModbusGateway("127.0.0.1", srv.port, min_gap_s=min_gap_s)
    modbus_gateway._GATEWAYS[f"direct-{id(gw)}"] = gw  # so the fixture closes it
    return gw


def _assert_single_session(srv):
    assert srv.max_connections <= 1
    assert srv.max_in_flight <= 1


async def test_many_concurrent_callers_use_one_session_one_transaction(server):
    gw = _gw(server)
    async with gw.transaction() as t:
        await t.connect()

    async def reader():
        async with gw.transaction() as t:
            return await t.read_holding(40069, 52, 1)

    async def writer(i):
        return await gw.run_latest(
            f"w{i}", lambda t: t.write_registers(0xF322, [i, i], 1)
        )

    results = await asyncio.gather(
        *[reader() for _ in range(15)], *[writer(i) for i in range(15)]
    )
    assert all(r is not SUPERSEDED and not r.isError() for r in results)
    assert len(server.requests) == 30
    assert gw.max_in_flight == 1
    _assert_single_session(server)
    assert server.total_connections == 1


async def test_minimum_gap_between_transactions(server):
    gw = _gw(server, min_gap_s=0.1)
    async with gw.transaction() as t:
        await t.connect()
    await asyncio.gather(*[_one_read(gw) for _ in range(5)])
    _assert_single_session(server)
    # Every request arrived >= min gap after the previous answer (timer slack).
    assert server.gaps and min(server.gaps) >= 0.08


async def _one_read(gw):
    async with gw.transaction() as t:
        return await t.read_holding(40069, 2, 1)


async def test_run_latest_coalesces_waiting_writes(server):
    gw = _gw(server)
    async with gw.transaction() as t:
        await t.connect()
    executed: list[int] = []

    async def op_for(i):
        async def op(t):
            executed.append(i)
            return await t.write_registers(0xF322, [i, 0], 1)
        return await gw.run_latest("limit", op)

    async with gw.transaction():  # hold the lock so the writers queue up
        tasks = [asyncio.create_task(op_for(i)) for i in range(1, 6)]
        await asyncio.sleep(0.05)
    results = await asyncio.gather(*tasks)
    assert executed == [5]  # only the newest sollwert reached the wire
    assert results[:4] == [SUPERSEDED] * 4
    assert len([r for r in server.requests if r[0] == 16]) == 1


async def test_wait_timeout_does_not_hang(server):
    gw = _gw(server)
    async with gw.transaction():
        with pytest.raises(GatewayWaitTimeout):
            async with gw.transaction(wait_timeout=0.05):
                pass


async def test_quiesce_refuses_everything_including_probe(server):
    gw = _gw(server)
    async with gw.transaction() as t:
        await t.connect()
    before = len(server.requests)
    gw.enter_quiesce(30)
    with pytest.raises(GatewayQuiesced):
        async with gw.transaction():
            pass
    with pytest.raises(GatewayQuiesced):
        async with gw.probe_transaction(server.port):
            pass
    assert len(server.requests) == before
    gw.clear_quiesce()
    await _one_read(gw)


async def test_handle_is_dead_after_lock_release(server):
    gw = _gw(server)
    async with gw.transaction() as t:
        await t.connect()
    with pytest.raises(RuntimeError):
        await t.read_holding(40069, 2, 1)


async def test_read_without_session_raises(server):
    gw = _gw(server)
    async with gw.transaction() as t:
        with pytest.raises(ModbusNotConnected):
            await t.read_holding(40069, 2, 1)


async def test_failed_connect_is_throttled_not_hammered(monkeypatch):
    attempts = []

    class _Dead:
        connected = False

        def __init__(self, *a, **kw):
            attempts.append(1)

        async def connect(self):
            return False

        def close(self):
            pass

    monkeypatch.setattr("pv_inverter_master.modbus_gateway.AsyncModbusTcpClient", _Dead)
    gw = ModbusGateway("127.0.0.1", 1, min_gap_s=0)
    for _ in range(5):
        with pytest.raises(ModbusConnectError):
            async with gw.transaction() as t:
                await t.connect()
    assert len(attempts) == 1  # the other four were refused locally


async def test_probe_reuses_live_session_and_never_opens_a_second(server):
    gw = _gw(server)
    async with gw.transaction() as t:
        await t.connect()
    async with gw.probe_transaction(server.port) as t:
        resp = await t.read_holding(40000, 2, 1)
        assert not resp.isError()
    assert server.total_connections == 1
    assert gw.connected  # probe did not close the production session
    with pytest.raises(GatewayPortConflict):
        async with gw.probe_transaction(server.port + 1):
            pass


async def test_probe_without_session_opens_temp_and_closes_it(server):
    gw = _gw(server)
    async with gw.probe_transaction(server.port) as t:
        resp = await t.read_holding(40000, 2, 1)
        assert not resp.isError()
    await asyncio.sleep(0.05)
    assert not gw.connected
    assert server.connections_now == 0
    _assert_single_session(server)


async def test_connect_race_between_plugins_and_probe_one_session(server):
    """Two plugin objects for the SAME host (e.g. stale instance + new one),
    polls, writes and a UI test button, all at once: still one session."""
    a = SolarEdgePlugin("127.0.0.1", server.port, dynamic_power_control=False)
    b = SolarEdgePlugin("127.0.0.1", server.port, dynamic_power_control=False)
    assert a.gateway is b.gateway

    async def ui_test():
        async with a.gateway.probe_transaction(server.port) as t:
            return await t.read_holding(40000, 2, 1)

    server.store.update({40000: 0x5375, 40001: 0x6E53})
    await asyncio.gather(
        a.connect(), b.connect(), ui_test(),
        a.poll(), b.poll(), a.write_power_limit(True, 40.0),
        b.write_power_limit(True, 60.0), ui_test(),
    )
    _assert_single_session(server)
    assert server.total_connections == 1
    assert is_host_claimed("127.0.0.1")
    await a.close()
    assert not a.gateway.connected


async def test_legacy_commit_does_not_overlap_next_poll(server):
    """The old fire-and-forget se30k-commit could overlap the next poll."""
    plugin = SolarEdgePlugin("127.0.0.1", server.port, dynamic_power_control=False)
    await plugin.connect()
    server.requests.clear()
    await asyncio.gather(
        plugin.write_power_limit(True, 50.0), plugin.poll(),
        plugin.poll(), plugin.write_power_limit(True, 70.0), plugin.poll(),
    )
    _assert_single_session(server)
    # the commit write (61696) is part of the completed write call
    assert (16, 61696) in server.requests


async def test_writers_hammering_with_polls_keep_invariants(server):
    plugin = SolarEdgePlugin("127.0.0.1", server.port, dynamic_power_control=False)
    await plugin.connect()
    calls = []
    for i in range(10):
        calls.append(plugin.write_power_limit(True, 10.0 + i))
        calls.append(plugin.poll())
    results = await asyncio.gather(*calls)
    _assert_single_session(server)
    writes = [r for r in results if hasattr(r, "error") and not hasattr(r, "inverter_registers")]
    # every write either ran or was coalesced away; never a transport error
    assert all(w.success or w.error == "superseded" for w in writes)
    assert any(w.success for w in writes)


async def test_registry_one_gateway_per_host_case_insensitive(monkeypatch):
    monkeypatch.setattr(modbus_gateway.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("127.0.0.1", 0))])
    a = get_gateway("Inverter.Example", 502)
    assert get_gateway("inverter.example", 1502) is a
    assert a.port == 1502
    assert find_gateway("INVERTER.EXAMPLE") is a


async def test_poll_loop_quiesce_arms_the_gateway(server):
    """ConnectionManager QUIESCE must stop ALL traffic at the gateway, not just the poll loop."""
    from pv_inverter_master.connection import ConnectionManager
    from pv_inverter_master.device_registry import _close_for_quiesce

    plugin = SolarEdgePlugin("127.0.0.1", server.port, dynamic_power_control=False)
    await plugin.connect()
    mgr = ConnectionManager(wedge_quiesce=True)
    for _ in range(ConnectionManager.QUIESCE_AFTER_CONSECUTIVE_FAILURES):
        mgr.on_poll_failure()
    import structlog
    await _close_for_quiesce(plugin, mgr, structlog.get_logger())
    assert plugin.gateway.quiesced
    assert not plugin.gateway.connected
    result = await plugin.write_power_limit(True, 50.0)
    assert not result.success
    with pytest.raises(GatewayQuiesced):
        await plugin.connect()  # reconnect hammering is refused at the gateway
