"""Regression tests: no code path may reach an inverter host past the
per-host ModbusGateway. Only 127.0.0.1 fakes and mocks are used."""
from __future__ import annotations

import asyncio
import os
import sys
import time
from ipaddress import IPv4Network
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import structlog

from pv_inverter_master import instance_lock, modbus_gateway
from pv_inverter_master.modbus_gateway import (
    GatewayHostClaimed,
    GatewayProbeBreaker,
    GatewayQuiesced,
    ModbusGateway,
    get_gateway,
    is_host_claimed,
    probe_read,
)
from pv_inverter_master.plugins.solaredge import SolarEdgePlugin
from tests.fake_modbus_server import FakeSingleSessionServer

REPO = Path(__file__).resolve().parent.parent
LAN_IP = "192.0.2.10"  # TEST-NET-1: used as a KEY only, never connected to


@pytest.fixture
async def server():
    srv = await FakeSingleSessionServer().start()
    yield srv
    for gw in list(modbus_gateway._GATEWAYS.values()):
        await gw.close()
    await srv.stop()


@pytest.fixture
def resolver(monkeypatch):
    """Mock name resolution: every *.test name resolves to LAN_IP."""
    calls = []

    def fake_getaddrinfo(host, *a, **k):
        calls.append(host)
        if str(host).endswith(".test"):
            return [(2, 1, 6, "", (LAN_IP, 0))]
        raise OSError("unresolvable")

    monkeypatch.setattr(modbus_gateway.socket, "getaddrinfo", fake_getaddrinfo)
    return calls


class FakeClient:
    """Stand-in for AsyncModbusTcpClient: records kwargs and concurrency."""

    instances: list["FakeClient"] = []
    active = 0
    max_active = 0

    def __init__(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs
        self.connected = False
        self.calls: list[tuple] = []
        FakeClient.instances.append(self)

    async def connect(self):
        self.connected = True

    def close(self):
        self.connected = False

    async def write_register(self, address, value, **kw):
        FakeClient.active += 1
        FakeClient.max_active = max(FakeClient.max_active, FakeClient.active)
        self.calls.append(("write_register", address, value, kw))
        await asyncio.sleep(0.02)
        FakeClient.active -= 1
        return MagicMock(isError=lambda: False)


@pytest.fixture
def fake_client(monkeypatch):
    FakeClient.instances, FakeClient.active, FakeClient.max_active = [], 0, 0
    monkeypatch.setattr(modbus_gateway, "AsyncModbusTcpClient", FakeClient)
    return FakeClient


class _Req:
    def __init__(self, body, venus_host):
        self._body = body
        self.app = {"config": MagicMock(venus=MagicMock(host=venus_host))}

    async def json(self):
        return self._body


# ------------------------------------------------------------ Venus write path
async def test_venus_write_goes_through_gateway_one_session(fake_client):
    from pv_inverter_master.webapp.routes_control import venus_write_handler

    resps = await asyncio.gather(*[
        venus_write_handler(_Req({"register": 2706, "value": v}, "127.0.0.1"))
        for v in (10, 20, 30)
    ])
    assert [r.status for r in resps] == [200, 200, 200]
    assert fake_client.max_active == 1  # parallel Venus requests queue on the host lock
    assert len(fake_client.instances) == 3  # sequential temp sessions, never 2 at once


async def test_venus_write_refuses_configured_inverter_host(fake_client):
    from pv_inverter_master.webapp.routes_control import venus_write_handler

    get_gateway("127.0.0.1").claim(object())  # a device plugin owns the host
    resp = await venus_write_handler(_Req({"register": 2706, "value": 5}, "127.0.0.1"))
    assert resp.status == 409
    assert fake_client.instances == []  # not even a client was built


async def test_venus_write_refuses_alias_of_inverter_host(fake_client, resolver):
    from pv_inverter_master.webapp.routes_control import venus_write_handler

    get_gateway(LAN_IP).claim(object())
    resp = await venus_write_handler(_Req({"register": 2706, "value": 5}, "wr.test"))
    assert resp.status == 409
    assert fake_client.instances == []


async def test_venus_write_uses_pymodbus_compat_kwarg(fake_client, monkeypatch):
    from pv_inverter_master.webapp.routes_control import venus_write_handler

    monkeypatch.setattr(modbus_gateway, "_UNIT_ARG", "slave")
    await venus_write_handler(_Req({"register": 2706, "value": 1}, "127.0.0.1"))
    kw = fake_client.instances[0].calls[0][3]
    assert kw == {"slave": 100}
    monkeypatch.setattr(modbus_gateway, "_UNIT_ARG", "device_id")
    await venus_write_handler(_Req({"register": 2706, "value": 1}, "127.0.0.1"))
    assert fake_client.instances[-1].calls[0][3] == {"device_id": 100}


def test_no_hardcoded_unit_argument_outside_gateway():
    for path in [*(REPO / "src").rglob("*.py"), REPO / "scripts" / "validate_se30k.py"]:
        if path.name == "modbus_gateway.py":
            continue
        code = [l for l in path.read_text().splitlines()
                if "``" not in l and not l.lstrip().startswith("#")]
        assert not [l for l in code if "slave=" in l], path
        assert not [l for l in code if "device_id=100" in l], path


# ----------------------------------------------------------- Listener sessions
async def _listener():
    conns = []

    async def handle(r, w):
        conns.append(1)
        w.close()

    srv = await asyncio.start_server(handle, "127.0.0.1", 0)
    return srv, srv.sockets[0].getsockname()[1], conns


async def test_scanner_probe_rechecks_claim_at_probe_time():
    from pv_inverter_master import scanner

    srv, port, conns = await _listener()
    try:
        assert await scanner._probe_modbus_port("127.0.0.1", port, 0.5) is True
        get_gateway("127.0.0.1").claim(object())  # device started after the list was built
        assert await scanner._probe_modbus_port("127.0.0.1", port, 0.5) is False
        await asyncio.sleep(0.1)  # let the listener's accept handler run
        assert len(conns) == 1  # the claimed host saw no second connection
    finally:
        srv.close()
        await srv.wait_closed()


async def test_scanner_probe_waits_for_gateway_lock():
    from pv_inverter_master import scanner

    srv, port, conns = await _listener()
    try:
        gw = get_gateway("127.0.0.1")
        async with gw.transaction():
            task = asyncio.create_task(scanner._probe_modbus_port("127.0.0.1", port, 0.5))
            await asyncio.sleep(0.15)
            assert not conns and not task.done()  # blocked behind the lock holder
        assert await asyncio.wait_for(task, 2) is True
        await asyncio.sleep(0.1)
        assert len(conns) == 1
    finally:
        srv.close()
        await srv.wait_closed()


async def test_scanner_probes_ports_of_a_host_one_after_the_other(monkeypatch):
    from pv_inverter_master import scanner

    active: dict[str, int] = {}
    peak: dict[str, int] = {}
    order: list[tuple[str, int]] = []

    async def fake_probe(ip, port, timeout):
        active[ip] = active.get(ip, 0) + 1
        peak[ip] = max(peak.get(ip, 0), active[ip])
        order.append((ip, port))
        await asyncio.sleep(0.01)
        active[ip] -= 1
        return False

    monkeypatch.setattr(scanner, "_probe_modbus_port", fake_probe)
    with patch("pv_inverter_master.scanner.detect_subnet", return_value=IPv4Network("127.0.0.0/30")), \
         patch("pv_inverter_master.scanner._scan_opendtu", return_value=[]):
        await scanner.scan_subnet(scanner.ScanConfig(ports=[502, 1502, 5020]))
    assert peak and max(peak.values()) == 1
    assert [p for ip, p in order if ip == "127.0.0.1"] == [502, 1502, 5020]


async def test_scan_with_real_tcp_probe_leaves_claimed_host_alone():
    """No mock of the TCP probe: the claimed host's listener must see nothing."""
    from pv_inverter_master import scanner

    srv, port, conns = await _listener()
    try:
        get_gateway("127.0.0.1").claim(object())
        with patch("pv_inverter_master.scanner.detect_subnet", return_value=IPv4Network("127.0.0.0/30")), \
             patch("pv_inverter_master.scanner._scan_opendtu", return_value=[]):
            await scanner.scan_subnet(scanner.ScanConfig(ports=[port]))
        assert conns == []
    finally:
        srv.close()
        await srv.wait_closed()


# ------------------------------------------------------------- Alias addresses
async def test_alias_shares_gateway_claim_and_quiesce(resolver):
    a = get_gateway(LAN_IP, 1502)
    assert get_gateway("WR.test") is a
    assert get_gateway(f" {LAN_IP} ") is a
    a.claim(object())
    assert is_host_claimed("wr.test")
    a.enter_quiesce(30)
    with pytest.raises(GatewayQuiesced):  # Sungrow probe via an alias hits the same window
        await probe_read("wr.test", 502, 1, 5000, 2, input_registers=True)
    assert len(modbus_gateway._GATEWAYS) == 1


async def test_unresolvable_name_creates_no_gateway(resolver):
    """No gateway under an unresolved name (could later become a
    second gateway for the same target as its IP)."""
    with pytest.raises(modbus_gateway.GatewayUnresolved):
        get_gateway("nope.invalid")
    assert not modbus_gateway._GATEWAYS
    assert modbus_gateway.find_gateway("nope.invalid") is None
    assert not is_host_claimed("nope.invalid")


async def test_resolution_is_cached(resolver):
    get_gateway("wr.test"); get_gateway("wr.test"); get_gateway("wr.test")
    assert resolver.count("wr.test") == 1


# ------------------------------------------------------ Coalescing per unit id
async def test_coalescing_is_per_unit_id(server):
    a = SolarEdgePlugin("127.0.0.1", server.port, unit_id=1, dynamic_power_control=False)
    b = SolarEdgePlugin("127.0.0.1", server.port, unit_id=2, dynamic_power_control=False)
    await a.connect()
    server.units.clear()
    gw = a.gateway
    async with gw.transaction():  # writers queue up behind this
        tasks = [
            asyncio.create_task(a.write_power_limit(True, 20.0)),   # superseded by next unit-1 write
            asyncio.create_task(b.write_power_limit(True, 60.0)),   # different unit: must survive
            asyncio.create_task(a.write_power_limit(True, 40.0)),
        ]
        await asyncio.sleep(0.05)
    results = await asyncio.gather(*tasks)
    assert [r.success for r in results] == [False, True, True]
    limit_writes = [(u, addr) for fc, addr, u in server.units if fc == 16 and addr == 61441]
    assert sorted(u for u, _ in limit_writes) == [1, 2]
    assert server.max_in_flight <= 1


# ------------------------------------------------------------------- Lock path
def test_lock_path_is_fixed_no_per_user_fallback(monkeypatch):
    monkeypatch.delenv(instance_lock.ENV_LOCK_PATH, raising=False)
    monkeypatch.setattr(os, "access", lambda *a, **k: False)  # dir "not writable"
    assert instance_lock.lock_path() == instance_lock.SERVICE_LOCK_PATH
    assert "tmp" not in instance_lock.lock_path()


def test_unusable_lock_path_aborts(tmp_path, monkeypatch):
    monkeypatch.setattr(instance_lock, "_held", None)
    with pytest.raises(instance_lock.InstanceLockUnavailable):
        instance_lock.acquire_instance_lock(str(tmp_path / "missing-dir" / "instance.lock"))
    assert instance_lock._held is None


def test_main_aborts_when_lock_unavailable(tmp_path, monkeypatch, capsys):
    from pv_inverter_master import __main__ as entry

    monkeypatch.setenv(instance_lock.ENV_LOCK_PATH, str(tmp_path / "no" / "instance.lock"))
    monkeypatch.setattr(sys, "argv", ["pv-inverter-master"])
    monkeypatch.setattr(entry, "load_config", MagicMock(side_effect=AssertionError("started anyway")))
    with pytest.raises(SystemExit) as exc:
        entry.main()
    assert exc.value.code == 1
    assert "cannot open instance lock" in capsys.readouterr().err


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
def test_other_user_can_still_take_the_lock_read_only(tmp_path, monkeypatch):
    """The service creates the file; an operator without write access must still
    be able to take (and be refused by) the SAME file - never another path."""
    path = tmp_path / "instance.lock"
    path.write_text("123\n")
    path.chmod(0o444)
    monkeypatch.setattr(instance_lock, "_held", None)
    instance_lock.acquire_instance_lock(str(path))
    assert instance_lock._held is not None
    assert path.read_text() == "123\n"  # read-only holder does not rewrite the pid


def test_validation_script_holds_the_lock_while_it_runs(tmp_path, monkeypatch):
    import subprocess
    lock = str(tmp_path / "instance.lock")
    code = (
        "import sys, time\n"
        "from pv_inverter_master.instance_lock import guard_from_argv\n"
        "sys.argv = ['validate_se30k.py']\n"
        "guard_from_argv('validate_se30k.py')\n"
        "print('guarded', flush=True)\n"
        "time.sleep(30)\n"
    )
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path), PV_INVERTER_MASTER_LOCK=lock)
    child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, env=env)
    try:
        assert child.stdout.readline().strip() == b"guarded"
        # while the script talks to the inverter, a service start finds the lock taken
        monkeypatch.setattr(instance_lock, "_held", None)
        with pytest.raises(instance_lock.InstanceAlreadyRunning):
            instance_lock.acquire_instance_lock(lock)
    finally:
        child.kill()
        child.wait()
    instance_lock.acquire_instance_lock(lock)  # freed when the script exits


def test_script_refuses_when_lock_cannot_be_taken(tmp_path, monkeypatch):
    monkeypatch.setenv(instance_lock.ENV_LOCK_PATH, str(tmp_path / "no" / "instance.lock"))
    monkeypatch.setattr(sys, "argv", ["s.py"])
    with pytest.raises(SystemExit) as exc:
        instance_lock.guard_from_argv("s.py")
    assert exc.value.code == 2


def test_script_uses_guard_before_any_connection():
    text = (REPO / "scripts" / "validate_se30k.py").read_text()
    assert text.index("guard_from_argv(") < text.index("ModbusTcpClient(SE_HOST")


# ------------------------------------------------------------- Initial connect
async def test_initial_connect_runs_under_poll_lock():
    from pv_inverter_master.device_registry import _device_poll_loop
    from pv_inverter_master.context import AppContext, DeviceState
    from pv_inverter_master.connection import ConnectionManager

    lock = asyncio.Lock()
    seen = []
    plugin = MagicMock()
    plugin.connect = AsyncMock(side_effect=lambda: seen.append(lock.locked()))
    plugin._client = None
    plugin.poll = AsyncMock(side_effect=asyncio.CancelledError)
    state = DeviceState(conn_mgr=ConnectionManager(poll_interval=1.0), poll_lock=lock)
    with pytest.raises(asyncio.CancelledError):
        await _device_poll_loop("d", plugin, state, 1.0, AsyncMock(), AppContext())
    assert seen and seen[0] is True


async def test_stop_waits_for_running_initial_connect(server, monkeypatch):
    """stop_device during connect + DPC init must not cancel it mid-transaction."""
    from pv_inverter_master import device_registry as dr
    from pv_inverter_master.config import Config, InverterEntry
    from pv_inverter_master.context import AppContext

    server.response_delay_s = 0.05  # DPC init = many transactions -> long block
    monkeypatch.setattr(dr, "READ_DRAIN_TIMEOUT_S", 0.1)
    entry = InverterEntry(id="d", host="127.0.0.1", port=server.port, enabled=True, type="solaredge")
    ctx = AppContext()
    registry = dr.DeviceRegistry(ctx, Config(inverters=[entry]), on_poll_success=AsyncMock())
    plugin = SolarEdgePlugin("127.0.0.1", server.port)
    with patch("pv_inverter_master.device_registry.plugin_factory", return_value=plugin):
        await registry.start_device("d")
        deadline = time.monotonic() + 3
        while not server.requests and time.monotonic() < deadline:
            await asyncio.sleep(0.005)
        assert server.requests, "initial connect never reached the wire"
        await asyncio.wait_for(registry.stop_device("d"), 30)
    assert server.aborted_in_flight == 0
    assert server.max_in_flight <= 1
    assert not plugin.gateway.connected


# ----------------------------------------------- Close during multi-step block
async def test_close_waits_for_a_long_multi_step_block(server):
    gw = ModbusGateway("127.0.0.1", server.port, min_gap_s=0.0)
    async with gw.transaction() as t:
        await t.connect()
    events = []

    async def holder():
        async with gw.transaction() as t:
            for _ in range(4):  # multi-step block, longer than the (short) drain below
                await t.read_holding(40000, 2, 1)
                await asyncio.sleep(0.1)
        events.append("holder_done")

    task = asyncio.create_task(holder())
    await asyncio.sleep(0.02)
    # old behaviour: drain_timeout 0.2 < block (~0.5 s) -> closed mid-block
    await gw.close(drain_timeout=5.0)
    events.append("closed")
    await task
    assert events == ["holder_done", "closed"]
    assert server.aborted_in_flight == 0


async def test_close_hard_limit_logs_error_and_still_returns(server):
    from structlog.testing import capture_logs
    gw = ModbusGateway("127.0.0.1", server.port, min_gap_s=0.0)
    async with gw.transaction() as t:
        await t.connect()
    release = asyncio.Event()

    async def stuck():
        async with gw.transaction():
            await release.wait()

    task = asyncio.create_task(stuck())
    await asyncio.sleep(0.02)
    with capture_logs() as logs:
        await asyncio.wait_for(gw.close(drain_timeout=0.1), 2)  # never hangs forever
    # Never closed without the lock - the session lives until the holder is done
    assert gw.connected
    assert any(e["event"] == "gateway_close_drain_timeout" and e["log_level"] == "error"
               for e in logs)
    release.set()
    await task
    await gw.wait_closed()
    assert not gw.connected
    assert modbus_gateway.CLOSE_DRAIN_TIMEOUT_S >= 30  # multi-step blocks fit


# -------------------------------------------------------------- Gateway window
async def test_gateway_window_follows_connection_manager(server):
    from pv_inverter_master.connection import ConnectionManager
    from pv_inverter_master.device_registry import _close_for_quiesce

    plugin = SolarEdgePlugin("127.0.0.1", server.port, dynamic_power_control=False)
    await plugin.connect()
    gw = plugin.gateway
    mgr = ConnectionManager(wedge_quiesce=True)
    log = structlog.get_logger()
    for _ in range(ConnectionManager.QUIESCE_AFTER_CONSECUTIVE_FAILURES):
        mgr.on_poll_failure()
    await _close_for_quiesce(plugin, mgr, log)
    assert gw.quiesced
    assert 470 < gw._quiesce_until - time.monotonic() <= 480

    # Window expires (both sides); recovery probes follow.
    mgr._quiesce_until = time.monotonic() - 1
    gw.clear_quiesce()
    for probe in range(1, ConnectionManager.QUIESCE_PROBE_ATTEMPTS + 1):
        await plugin.connect()  # the probe is NOT refused locally
        mgr.on_poll_failure()
        await _close_for_quiesce(plugin, mgr, log)
        if probe < ConnectionManager.QUIESCE_PROBE_ATTEMPTS:
            assert not gw.quiesced, f"probe {probe} must not re-arm the gateway window"
    # after the last planned probe the manager re-arms: gateway follows
    assert gw.quiesced
    assert 470 < gw._quiesce_until - time.monotonic() <= 480
    # ...and the full cycle ends together with the manager's window
    mgr._quiesce_until = time.monotonic() - 1
    gw.clear_quiesce()
    await plugin.connect()
    assert gw.connected


# --------------------------------------------------------------- Probe breaker
async def test_mute_server_trips_probe_breaker(server):
    server.mute = True
    gw = get_gateway("127.0.0.1")
    for _ in range(modbus_gateway.PROBE_BREAKER_AFTER):
        with pytest.raises(Exception) as exc:
            await probe_read("127.0.0.1", server.port, 1, 40000, 2, timeout=0.15)
        assert not isinstance(exc.value, GatewayProbeBreaker)
    sessions = server.total_connections
    with pytest.raises(GatewayProbeBreaker):
        await probe_read("127.0.0.1", server.port, 1, 40000, 2, timeout=0.15)
    assert server.total_connections == sessions  # breaker open: no new session
    gw._probe_breaker_until = 0.0  # window over
    server.mute = False
    await probe_read("127.0.0.1", server.port, 1, 40000, 2)
    assert gw._probe_silent == 0 and gw._probe_breaker_trips == 0


async def test_breaker_also_stops_scanner_tcp_probe(server):
    gw = get_gateway("127.0.0.1")
    gw._probe_breaker_until = time.monotonic() + 60
    with pytest.raises(GatewayProbeBreaker):
        await gw.tcp_probe(server.port)


# ------------------------------------------------------------------- Poll rate
@pytest.mark.parametrize("typ", ["solaredge", "sungrow"])
async def test_single_session_plugins_poll_at_most_once_a_second(typ):
    from pv_inverter_master.config import Config, InverterEntry
    from pv_inverter_master.context import AppContext
    from pv_inverter_master.device_registry import DeviceRegistry
    from pv_inverter_master.plugins.sungrow import SungrowPlugin

    entry = InverterEntry(id="d", host=LAN_IP, port=502, enabled=True, type=typ, poll_interval=0.05)
    ctx = AppContext()
    reg = DeviceRegistry(ctx, Config(inverters=[entry]), on_poll_success=AsyncMock())
    plugin = SolarEdgePlugin(LAN_IP, 502) if typ == "solaredge" else SungrowPlugin(LAN_IP, 502)
    plugin.connect = AsyncMock()
    plugin.poll = AsyncMock(return_value=MagicMock(success=False, error="x"))
    plugin.close = AsyncMock()
    with patch("pv_inverter_master.device_registry.plugin_factory", return_value=plugin):
        await reg.start_device("d")
        assert ctx.devices["d"].conn_mgr._poll_interval == 1.0
        await reg.stop_all()


# ---------------------------------------------------------- Slave distribution
def _slave(distributor):
    from pymodbus.datastore import ModbusSequentialDataBlock
    from pv_inverter_master.context import AppContext
    from pv_inverter_master.control import ControlState
    from pv_inverter_master.proxy import StalenessAwareSlaveContext
    from pv_inverter_master.register_cache import RegisterCache

    datablock = ModbusSequentialDataBlock(1, [0] * 2000)
    cache = RegisterCache(datablock, staleness_timeout=30.0)
    cache.update(40003, [0] * 10)
    ctrl = ControlState()
    slave = StalenessAwareSlaveContext(
        cache=cache, plugin=None, control_state=ctrl,
        app_ctx=AppContext(), distributor=distributor, hr=datablock,
    )
    return slave, ctrl


async def test_model123_writes_of_one_tick_distribute_once(monkeypatch):
    from pv_inverter_master import proxy as proxy_mod

    monkeypatch.setattr(proxy_mod, "DISTRIBUTE_COALESCE_S", 0.1)
    dist = MagicMock()
    dist.distribute = AsyncMock()
    slave, ctrl = _slave(dist)
    # Venus OS: WMaxLimPct, then the enable flag, then a re-write - back to back.
    await asyncio.gather(
        slave.async_setValues(6, 40154, [30]),
        slave.async_setValues(6, 40158, [1]),
        slave.async_setValues(6, 40154, [20]),
    )
    await slave.wait_distributions()
    assert dist.distribute.await_count == 1
    pct, enabled = dist.distribute.await_args.args
    assert pct == ctrl.wmaxlimpct_float == 20.0  # the FINAL value of the tick
    assert enabled is True


async def test_sequential_writes_of_one_tick_distribute_once(monkeypatch):
    """SEQUENTIAL writes (each awaited) share the tick buffer."""
    from pv_inverter_master import proxy as proxy_mod

    monkeypatch.setattr(proxy_mod, "DISTRIBUTE_COALESCE_S", 0.2)
    dist = MagicMock()
    dist.distribute = AsyncMock()
    slave, ctrl = _slave(dist)
    await slave.async_setValues(6, 40154, [30])
    await slave.async_setValues(6, 40158, [1])
    await slave.async_setValues(6, 40154, [20])
    await slave.wait_distributions()
    assert dist.distribute.await_count == 1
    assert dist.distribute.await_args.args == (20.0, True)


async def test_write_after_the_window_is_a_new_distribution(monkeypatch):
    from pv_inverter_master import proxy as proxy_mod

    monkeypatch.setattr(proxy_mod, "DISTRIBUTE_COALESCE_S", 0.02)
    dist = MagicMock()
    dist.distribute = AsyncMock()
    slave, _ = _slave(dist)
    await slave.async_setValues(6, 40154, [30])
    await slave.wait_distributions()
    await slave.async_setValues(6, 40154, [10])
    await slave.wait_distributions()
    assert dist.distribute.await_count == 2  # nothing is ever lost


async def test_cancelled_writer_does_not_abort_the_shared_distribution(monkeypatch):
    from pv_inverter_master import proxy as proxy_mod

    monkeypatch.setattr(proxy_mod, "DISTRIBUTE_COALESCE_S", 0.1)
    dist = MagicMock()
    dist.distribute = AsyncMock()
    slave, _ = _slave(dist)
    first = asyncio.create_task(slave.async_setValues(6, 40154, [30]))
    second = asyncio.create_task(slave.async_setValues(6, 40154, [20]))
    await asyncio.sleep(0.02)
    first.cancel()
    await asyncio.gather(second, return_exceptions=True)
    await slave.wait_distributions()
    assert dist.distribute.await_count == 1


# ------------------------------------------------------------- Queued writes
async def test_write_during_distribute_is_not_booked_as_applied(monkeypatch):
    """A 20 % write arriving while distribute(80) runs must still be sent."""
    from pv_inverter_master import proxy as proxy_mod

    monkeypatch.setattr(proxy_mod, "DISTRIBUTE_COALESCE_S", 0.01)
    release = asyncio.Event()
    sent = []

    async def distribute(pct, enabled):
        sent.append(pct)
        if len(sent) == 1:
            await release.wait()

    dist = MagicMock()
    dist.distribute = distribute
    slave, ctrl = _slave(dist)
    await slave.async_setValues(6, 40154, [80])
    await asyncio.sleep(0.05)             # first distribute(80) is on the wire
    await slave.async_setValues(6, 40154, [20])
    await asyncio.sleep(0.05)             # second flush waits behind the first
    release.set()                          # first ends BEFORE the second flush ran
    await slave.wait_distributions()
    assert sent == [80.0, 20.0]
    assert slave._last_applied_pct == 20.0


# ------------------------------------------------------------- In-flight writes
async def test_background_distribution_counts_as_inflight(monkeypatch):
    """Maintenance drain must wait for a distribution whose writers are gone."""
    from pv_inverter_master import proxy as proxy_mod

    monkeypatch.setattr(proxy_mod, "DISTRIBUTE_COALESCE_S", 0.05)
    release = asyncio.Event()

    async def distribute(pct, enabled):
        await release.wait()

    dist = MagicMock()
    dist.distribute = distribute
    slave, _ = _slave(dist)
    writer = asyncio.create_task(slave.async_setValues(6, 40154, [30]))
    await asyncio.sleep(0.01)
    writer.cancel()
    await asyncio.gather(writer, return_exceptions=True)
    await asyncio.sleep(0.1)               # flush is inside distribute()
    assert not slave._inflight_drained.is_set()
    assert slave._inflight_count == 1
    release.set()
    await slave.wait_distributions()
    assert slave._inflight_drained.is_set() and slave._inflight_count == 0
