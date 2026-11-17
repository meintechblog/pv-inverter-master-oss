"""Regression tests for review findings."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.test_gateway_isolation import _slave

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    from pv_inverter_master import proxy as proxy_mod

    monkeypatch.setattr(proxy_mod, "DISTRIBUTE_COALESCE_S", 0.01)
    monkeypatch.setattr(proxy_mod, "FLUSH_RETRY_BACKOFF_S", 0.01, raising=False)


def _dist(sent):
    async def distribute(pct, enabled):
        sent.append((pct, enabled))

    d = MagicMock()
    d.distribute = distribute
    return d


# ------------------------------------------------------ Re-enable distribution
async def test_reenable_with_same_value_distributes_again():
    sent = []
    slave, ctrl = _slave(_dist(sent))
    await slave.async_setValues(6, 40154, [50])
    await slave.wait_distributions()
    await slave.async_setValues(6, 40158, [0])        # disable -> 100 % released
    await slave.wait_distributions()
    assert sent[-1][1] is False
    assert slave._last_applied_pct == 100.0
    await slave.async_setValues(6, 40158, [1])        # enable, same 50 %
    await slave.wait_distributions()
    assert sent[-1] == (50.0, True)
    assert len(sent) == 3


# ---------------------------------------------------------- Failed flush retry
async def test_failed_flush_is_retried_with_newest_value_and_flagged():
    calls = []

    async def distribute(pct, enabled):
        calls.append(pct)
        if len(calls) == 1:
            raise RuntimeError("boom")

    d = MagicMock()
    d.distribute = distribute
    slave, ctrl = _slave(d)
    await slave.async_setValues(6, 40154, [40])
    for _ in range(100):
        if slave.flush_error:
            break
        await asyncio.sleep(0.005)
    assert slave.flush_error and slave._app_ctx.distribution_error
    await slave.wait_distributions()
    assert calls == [40.0, 40.0]                      # retried
    assert not slave.flush_error and not slave._app_ctx.distribution_error
    assert slave.flush_failures_total == 1
    assert slave.get_filter_stats()["flush_failures_total"] == 1


async def test_retry_bypasses_release_up_hysteresis():
    """Failure on a throttle-down: the old higher release must not stay."""
    calls = []

    async def distribute(pct, enabled):
        calls.append(pct)
        if len(calls) == 2:
            raise RuntimeError("boom")

    d = MagicMock()
    d.distribute = distribute
    slave, ctrl = _slave(d)
    await slave.async_setValues(6, 40154, [80])
    await slave.wait_distributions()
    await slave.async_setValues(6, 40154, [78])       # down: fails once
    await slave.wait_distributions()
    assert calls == [80.0, 78.0, 78.0]
    assert slave._last_applied_pct == 78.0


async def test_gives_up_after_limit_but_stays_flagged(monkeypatch):
    from pv_inverter_master import proxy as proxy_mod

    monkeypatch.setattr(proxy_mod, "FLUSH_MAX_RETRIES", 2, raising=False)
    d = MagicMock()
    d.distribute = AsyncMock(side_effect=RuntimeError("down"))
    slave, _ = _slave(d)
    await slave.async_setValues(6, 40154, [40])
    await slave.wait_distributions()
    assert d.distribute.await_count == 3              # 1 + 2 retries
    assert slave.flush_error and slave._app_ctx.distribution_error
    # a following Venus write starts over and clears the flag on success
    d.distribute = AsyncMock()
    await slave.async_setValues(6, 40154, [30])
    await slave.wait_distributions()
    assert not slave.flush_error


async def test_health_degrades_on_distribution_error():
    from pv_inverter_master.context import AppContext
    from pv_inverter_master.webapp.routes_core import _derive_health_payload

    ctx = AppContext()
    cache = MagicMock()
    cache.is_stale = False
    ctx.cache = cache
    cfg = MagicMock()
    cfg.venus.host = ""
    assert _derive_health_payload(ctx, 600.0, cfg)["modbus_server"] == "ok"
    ctx.distribution_error = True
    assert _derive_health_payload(ctx, 600.0, cfg)["modbus_server"] == "degraded"


# ----------------------------------------------------------- Flush queue limit
async def test_at_most_one_running_and_one_waiting_flush():
    release = asyncio.Event()
    sent = []

    async def distribute(pct, enabled):
        sent.append(pct)
        if len(sent) == 1:
            await release.wait()

    d = MagicMock()
    d.distribute = distribute
    slave, _ = _slave(d)
    await slave.async_setValues(6, 40154, [90])
    await asyncio.sleep(0.05)                          # first on the wire
    for v in (80, 70, 60, 50, 40, 30):
        await slave.async_setValues(6, 40154, [v])
        await asyncio.sleep(0.02)
    assert len(slave._flush_tasks) == 1                # no task pile-up
    release.set()
    await slave.wait_distributions()
    assert sent == [90.0, 30.0]                        # coalesced to newest


# ----------------------------------------------------------------- DNS failure
async def test_dns_failure_then_success_never_gives_two_gateways(monkeypatch):
    from pv_inverter_master import modbus_gateway as mg

    mg.reset_gateways()
    state = {"up": False}

    def fake_getaddrinfo(host, *a, **k):
        if state["up"] and host == "wr.test":
            return [(2, 1, 6, "", ("127.0.0.1", 0))]
        raise OSError("no dns")

    monkeypatch.setattr(mg.socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(mg.GatewayUnresolved):
        mg.get_gateway("wr.test", 1502)
    assert not mg._GATEWAYS                         # nothing keyed by the name
    state["up"] = True
    by_name = mg.get_gateway("wr.test", 1502)
    by_ip = mg.get_gateway("127.0.0.1", 1502)
    assert by_name is by_ip and len(mg._GATEWAYS) == 1
    mg.reset_gateways()


async def test_dns_outage_after_resolution_keeps_the_gateway(monkeypatch):
    from pv_inverter_master import modbus_gateway as mg

    mg.reset_gateways()
    monkeypatch.setattr(
        mg.socket, "getaddrinfo",
        lambda h, *a, **k: [(2, 1, 6, "", ("127.0.0.1", 0))],
    )
    first = mg.get_gateway("wr.test", 1502)
    monkeypatch.setattr(mg, "_RESOLVE_TTL_S", 0.0)    # cache expired ...

    def down(*a, **k):
        raise OSError("dns down")

    monkeypatch.setattr(mg.socket, "getaddrinfo", down)  # ... and DNS gone
    assert mg.get_gateway("wr.test", 1502) is first
    mg.reset_gateways()


# ----------------------------------------------- Shutdown and detached connect
async def test_shutdown_waits_for_detached_connect_block():
    from pv_inverter_master import device_registry as dr

    done = []

    class P:
        async def connect(self):
            await asyncio.sleep(0.1)
            done.append(1)

    task = asyncio.create_task(dr._connect_shielded(P()))
    await asyncio.sleep(0.01)
    task.cancel()                                   # poll task cancelled (stop)
    await asyncio.gather(task, return_exceptions=True)
    assert not done and dr._CONNECT_TASKS           # block still running detached
    assert await dr.wait_connect_tasks(2.0) is True
    assert done == [1]


async def test_shutdown_abandons_stuck_connect_block_after_timeout():
    from pv_inverter_master import device_registry as dr

    class P:
        async def connect(self):
            await asyncio.sleep(60)

    task = asyncio.create_task(dr._connect_shielded(P()))
    await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert await dr.wait_connect_tasks(0.05) is False
    await asyncio.sleep(0)
    assert not dr._CONNECT_TASKS


async def test_shutdown_gateways_waits_for_deferred_close():
    from pv_inverter_master import modbus_gateway as mg

    mg.reset_gateways()
    gw = mg.get_gateway("127.0.0.1", 1)
    client = MagicMock(connected=True)
    gw.adopt_client(client)
    release = asyncio.Event()

    async def holder():
        async with gw.transaction():
            await release.wait()

    task = asyncio.create_task(holder())
    await asyncio.sleep(0.01)
    await gw.close(drain_timeout=0.05)               # deferred close pending
    client.close.assert_not_called()
    asyncio.get_running_loop().call_later(0.1, release.set)
    await mg.shutdown_gateways(2.0)
    client.close.assert_called_once()                # closed BEFORE returning
    await task
    mg.reset_gateways()


async def test_shutdown_gateways_is_bounded():
    from pv_inverter_master import modbus_gateway as mg

    mg.reset_gateways()
    gw = mg.get_gateway("127.0.0.1", 1)
    gw.adopt_client(MagicMock(connected=True))
    release = asyncio.Event()

    async def holder():
        async with gw.transaction():
            await release.wait()

    task = asyncio.create_task(holder())
    await asyncio.sleep(0.01)
    await asyncio.wait_for(mg.shutdown_gateways(0.1), 2.0)
    release.set()
    await task
    await gw.wait_closed()
    mg.reset_gateways()


def test_main_shutdown_waits_before_loop_ends():
    import inspect
    from pv_inverter_master import __main__ as m

    src = inspect.getsource(m)
    i = src.index("await registry.stop_all()")
    j = src.index("shutdown_complete")
    block = src[i:j]
    assert "wait_connect_tasks(SHUTDOWN_WAIT_S)" in block
    assert "shutdown_gateways(SHUTDOWN_WAIT_S)" in block
