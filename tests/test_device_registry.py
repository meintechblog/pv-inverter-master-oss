"""Tests for DeviceRegistry per-device lifecycle management."""
from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Pre-mock dashboard module to avoid timeseries.py slots= incompatibility on Python < 3.10
if "pv_inverter_master.dashboard" not in sys.modules:
    _mock_dashboard = MagicMock()
    _mock_dashboard.DashboardCollector = MagicMock
    sys.modules["pv_inverter_master.dashboard"] = _mock_dashboard

from pv_inverter_master.config import Config, InverterEntry, GatewayConfig
from pv_inverter_master.connection import ConnectionManager, ConnectionState
from pv_inverter_master.context import AppContext, DeviceState
from pv_inverter_master.plugin import PollResult


def _make_config(*entries: InverterEntry) -> Config:
    """Create a Config with given inverter entries."""
    return Config(inverters=list(entries))


def _ok_result() -> PollResult:
    return PollResult(
        success=True,
        common_registers=[0] * 67,
        inverter_registers=[0] * 52,
    )


def _fail_result() -> PollResult:
    return PollResult(
        success=False,
        common_registers=[],
        inverter_registers=[],
        error="timeout",
    )


def _make_mock_plugin(poll_result=None):
    """Create a mock InverterPlugin."""
    plugin = AsyncMock()
    plugin.connect = AsyncMock()
    plugin.close = AsyncMock()
    plugin.poll = AsyncMock(return_value=poll_result or _ok_result())
    # get_model_120_registers is a sync method, must be MagicMock (not AsyncMock)
    plugin.get_model_120_registers = MagicMock(return_value=None)
    # get_static_common_overrides is also sync
    plugin.get_static_common_overrides = MagicMock(return_value={})
    # min_poll_interval is a sync float property — pin it so the registry's
    # clamp sees a real number, not a truthy Mock.
    plugin.min_poll_interval = 0.0
    return plugin


@pytest.fixture
def app_ctx():
    return AppContext()


@pytest.fixture
def entry_a():
    return InverterEntry(id="dev_a", host="198.51.100.10", port=502, enabled=True, type="solaredge")


@pytest.fixture
def entry_b():
    return InverterEntry(id="dev_b", host="198.51.100.11", port=502, enabled=True, type="solaredge")


@pytest.fixture
def entry_disabled():
    return InverterEntry(id="dev_off", host="198.51.100.12", port=502, enabled=False, type="solaredge")


def _patch_factories(mock_plugin):
    """Return context manager patching plugin_factory."""
    return patch("pv_inverter_master.device_registry.plugin_factory", return_value=mock_plugin)


@pytest.mark.asyncio
async def test_start_device(app_ctx, entry_a):
    """start_device creates plugin, DeviceState, and starts poll task."""
    from pv_inverter_master.device_registry import DeviceRegistry

    config = _make_config(entry_a)
    on_success = AsyncMock()
    registry = DeviceRegistry(app_ctx, config, on_poll_success=on_success)

    mock_plugin = _make_mock_plugin()
    with _patch_factories(mock_plugin):
        await registry.start_device("dev_a")

    assert "dev_a" in app_ctx.devices
    assert "dev_a" in registry.get_active_device_ids()
    assert registry.get_active_count() == 1

    # Let poll loop run once
    await asyncio.sleep(0.05)
    mock_plugin.poll.assert_called()

    await registry.stop_all()


@pytest.mark.asyncio
async def test_plugin_min_poll_interval_floors_fast_config(app_ctx, entry_a):
    """A plugin's min_poll_interval clamps a too-fast configured interval."""
    from pv_inverter_master.device_registry import DeviceRegistry

    config = _make_config(entry_a)
    config.proxy.poll_interval = 1.0
    registry = DeviceRegistry(app_ctx, config, on_poll_success=AsyncMock())

    mock_plugin = _make_mock_plugin()
    mock_plugin.min_poll_interval = 45.0  # rate-limited device
    with _patch_factories(mock_plugin):
        await registry.start_device("dev_a")

    # 1.0s configured → floored up to the plugin's 45.0s minimum.
    assert app_ctx.devices["dev_a"].conn_mgr._poll_interval == 45.0

    await registry.stop_all()


@pytest.mark.asyncio
async def test_plugin_min_poll_interval_does_not_slow_explicit_config(app_ctx, entry_a):
    """An explicit interval already slower than the floor is left untouched."""
    from pv_inverter_master.device_registry import DeviceRegistry

    entry_a.poll_interval = 60.0  # explicit per-device override
    config = _make_config(entry_a)
    registry = DeviceRegistry(app_ctx, config, on_poll_success=AsyncMock())

    mock_plugin = _make_mock_plugin()
    mock_plugin.min_poll_interval = 45.0
    with _patch_factories(mock_plugin):
        await registry.start_device("dev_a")

    assert app_ctx.devices["dev_a"].conn_mgr._poll_interval == 60.0

    await registry.stop_all()


@pytest.mark.asyncio
async def test_start_multiple_devices(app_ctx, entry_a, entry_b):
    """start_all starts N devices, each with independent poll task."""
    from pv_inverter_master.device_registry import DeviceRegistry

    config = _make_config(entry_a, entry_b)
    on_success = AsyncMock()
    registry = DeviceRegistry(app_ctx, config, on_poll_success=on_success)

    mock_plugin = _make_mock_plugin()
    with _patch_factories(mock_plugin):
        await registry.start_all()

    assert registry.get_active_count() == 2
    assert set(registry.get_active_device_ids()) == {"dev_a", "dev_b"}

    await registry.stop_all()


@pytest.mark.asyncio
async def test_stop_device(app_ctx, entry_a):
    """stop_device cancels poll task, calls plugin.close(), removes DeviceState."""
    from pv_inverter_master.device_registry import DeviceRegistry

    config = _make_config(entry_a)
    on_success = AsyncMock()
    registry = DeviceRegistry(app_ctx, config, on_poll_success=on_success)

    mock_plugin = _make_mock_plugin()
    with _patch_factories(mock_plugin):
        await registry.start_device("dev_a")
        await asyncio.sleep(0.02)
        await registry.stop_device("dev_a")

    assert "dev_a" not in app_ctx.devices
    assert registry.get_active_count() == 0
    mock_plugin.close.assert_called_once()


@pytest.mark.asyncio
async def test_enable_disable(app_ctx, entry_a):
    """disable_device stops poll + cleans up; enable_device starts fresh poll."""
    from pv_inverter_master.device_registry import DeviceRegistry

    config = _make_config(entry_a)
    on_success = AsyncMock()
    registry = DeviceRegistry(app_ctx, config, on_poll_success=on_success)

    mock_plugin = _make_mock_plugin()
    with _patch_factories(mock_plugin):
        await registry.start_device("dev_a")
        assert registry.get_active_count() == 1

        await registry.disable_device("dev_a")
        assert registry.get_active_count() == 0
        assert "dev_a" not in app_ctx.devices

        # Re-enable
        await registry.enable_device("dev_a")
        assert registry.get_active_count() == 1
        assert "dev_a" in app_ctx.devices

    await registry.stop_all()


def _unresolved_plugin():
    """A plugin whose ``gateway`` property raises GatewayUnresolved (never-resolved host)."""
    from pv_inverter_master.modbus_gateway import GatewayUnresolved

    plugin = _make_mock_plugin()

    def _raise(self):
        raise GatewayUnresolved("nohost.invalid: host name cannot be resolved")

    type(plugin).gateway = property(_raise)
    plugin.release = MagicMock()
    return plugin


@pytest.mark.asyncio
async def test_disable_and_stop_work_for_never_resolved_host(app_ctx, entry_a):
    """N5: removing/disabling a device whose host never resolved must not raise."""
    from pv_inverter_master.device_registry import DeviceRegistry

    registry = DeviceRegistry(app_ctx, _make_config(entry_a), on_poll_success=AsyncMock())
    plugin = _unresolved_plugin()
    with _patch_factories(plugin):
        await registry.start_device("dev_a")
        await registry.disable_device("dev_a")
        assert registry.get_active_count() == 0
        plugin.close.assert_awaited()
    await registry.stop_all()


@pytest.mark.asyncio
async def test_disabled_device_skipped(app_ctx, entry_a, entry_disabled):
    """start_all skips entries where entry.enabled=False."""
    from pv_inverter_master.device_registry import DeviceRegistry

    config = _make_config(entry_a, entry_disabled)
    on_success = AsyncMock()
    registry = DeviceRegistry(app_ctx, config, on_poll_success=on_success)

    mock_plugin = _make_mock_plugin()
    with _patch_factories(mock_plugin):
        await registry.start_all()

    assert registry.get_active_count() == 1
    assert "dev_a" in registry.get_active_device_ids()
    assert "dev_off" not in registry.get_active_device_ids()

    await registry.stop_all()


@pytest.mark.asyncio
async def test_no_task_leak(app_ctx, entry_a):
    """After 5x start/stop cycles, asyncio task count stays stable."""
    from pv_inverter_master.device_registry import DeviceRegistry

    config = _make_config(entry_a)
    on_success = AsyncMock()
    registry = DeviceRegistry(app_ctx, config, on_poll_success=on_success)

    mock_plugin = _make_mock_plugin()
    with _patch_factories(mock_plugin):
        baseline = len(asyncio.all_tasks())

        for _ in range(5):
            await registry.start_device("dev_a")
            await asyncio.sleep(0.02)
            await registry.stop_device("dev_a")
            await asyncio.sleep(0.02)

        final = len(asyncio.all_tasks())
        assert final <= baseline + 1, f"Task leak: baseline={baseline}, final={final}"


@pytest.mark.asyncio
async def test_per_device_state(app_ctx, entry_a, entry_b):
    """Each device gets its own ConnectionManager, poll_counter, DashboardCollector."""
    from pv_inverter_master.device_registry import DeviceRegistry

    config = _make_config(entry_a, entry_b)
    on_success = AsyncMock()
    registry = DeviceRegistry(app_ctx, config, on_poll_success=on_success)

    mock_plugin = _make_mock_plugin()
    with _patch_factories(mock_plugin):
        await registry.start_all()

    state_a = app_ctx.devices["dev_a"]
    state_b = app_ctx.devices["dev_b"]

    assert state_a is not state_b
    assert state_a.conn_mgr is not state_b.conn_mgr
    assert state_a.poll_counter is not state_b.poll_counter
    assert state_a.collector is not state_b.collector

    await registry.stop_all()


@pytest.mark.asyncio
async def test_on_poll_success_callback(app_ctx, entry_a):
    """Successful poll calls on_poll_success callback with device_id."""
    from pv_inverter_master.device_registry import DeviceRegistry

    config = _make_config(entry_a)
    on_success = AsyncMock()
    registry = DeviceRegistry(app_ctx, config, on_poll_success=on_success)

    mock_plugin = _make_mock_plugin()
    with _patch_factories(mock_plugin):
        await registry.start_device("dev_a")
        await asyncio.sleep(0.05)

    on_success.assert_called_with("dev_a")

    await registry.stop_all()


@pytest.mark.asyncio
async def test_poll_loop_handles_cancel(app_ctx, entry_a):
    """CancelledError propagates out of poll loop (not caught by except Exception)."""
    from pv_inverter_master.device_registry import _device_poll_loop

    on_success = AsyncMock()

    mock_plugin = _make_mock_plugin()
    # Make poll raise CancelledError
    mock_plugin.poll = AsyncMock(side_effect=asyncio.CancelledError)

    device_state = DeviceState(
        conn_mgr=ConnectionManager(poll_interval=0.01),
        poll_counter={"success": 0, "total": 0},
    )

    task = asyncio.create_task(
        _device_poll_loop(
            device_id="dev_a",
            plugin=mock_plugin,
            device_state=device_state,
            poll_interval=0.01,
            on_success=on_success,
            app_ctx=app_ctx,
        )
    )

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_poll_success_populates_health_fields(app_ctx, entry_a):
    """Successful poll sets last_poll_at + last_successful_poll_at + clears last_poll_error."""
    import time as _time
    from pv_inverter_master.device_registry import DeviceRegistry

    config = _make_config(entry_a)
    config.proxy.poll_interval = 0.01
    registry = DeviceRegistry(app_ctx, config, on_poll_success=AsyncMock())

    mock_plugin = _make_mock_plugin()  # default _ok_result
    before = _time.time()
    with _patch_factories(mock_plugin):
        await registry.start_device("dev_a")
        await asyncio.sleep(0.05)

    state = app_ctx.devices["dev_a"]
    assert state.last_poll_at is not None
    assert state.last_successful_poll_at is not None
    assert isinstance(state.last_poll_at, float)
    assert isinstance(state.last_successful_poll_at, float)
    # Both timestamps should be within the last few seconds.
    assert state.last_poll_at >= before
    assert state.last_successful_poll_at >= before
    assert state.last_poll_error is None

    await registry.stop_all()


@pytest.mark.asyncio
async def test_poll_failure_populates_last_poll_error(app_ctx, entry_a):
    """Soft-fail (PollResult.success=False) sets last_poll_at + last_poll_error; leaves last_successful_poll_at None."""
    import time as _time
    from pv_inverter_master.device_registry import DeviceRegistry

    config = _make_config(entry_a)
    config.proxy.poll_interval = 0.01
    registry = DeviceRegistry(app_ctx, config, on_poll_success=AsyncMock())

    mock_plugin = _make_mock_plugin(poll_result=_fail_result())
    before = _time.time()
    with _patch_factories(mock_plugin):
        await registry.start_device("dev_a")
        await asyncio.sleep(0.05)

    state = app_ctx.devices["dev_a"]
    assert state.last_poll_at is not None
    assert state.last_poll_at >= before
    assert state.last_successful_poll_at is None
    assert state.last_poll_error == "timeout"

    await registry.stop_all()


@pytest.mark.asyncio
async def test_poll_exception_populates_last_poll_error_truncated(app_ctx, entry_a):
    """Plugin raising Exception sets last_poll_at + truncated last_poll_error; leaves last_successful_poll_at."""
    import time as _time
    from pv_inverter_master.device_registry import DeviceRegistry

    config = _make_config(entry_a)
    config.proxy.poll_interval = 0.01
    registry = DeviceRegistry(app_ctx, config, on_poll_success=AsyncMock())

    mock_plugin = _make_mock_plugin()
    mock_plugin.poll = AsyncMock(side_effect=RuntimeError("boom" * 100))
    before = _time.time()
    with _patch_factories(mock_plugin):
        await registry.start_device("dev_a")
        await asyncio.sleep(0.05)

    state = app_ctx.devices["dev_a"]
    assert state.last_poll_at is not None
    assert state.last_poll_at >= before
    assert state.last_successful_poll_at is None
    assert state.last_poll_error is not None
    assert len(state.last_poll_error) <= 200
    assert "boom" in state.last_poll_error

    await registry.stop_all()


@pytest.mark.asyncio
async def test_poll_success_after_failure_clears_error(app_ctx, entry_a):
    """Success after a prior failure clears last_poll_error and sets last_successful_poll_at."""
    from pv_inverter_master.device_registry import DeviceRegistry

    config = _make_config(entry_a)
    config.proxy.poll_interval = 0.01
    registry = DeviceRegistry(app_ctx, config, on_poll_success=AsyncMock())

    mock_plugin = _make_mock_plugin()
    # First call fails, subsequent calls succeed.
    import itertools
    sequence = itertools.chain(
        [_fail_result()],
        itertools.cycle([_ok_result()]),
    )
    mock_plugin.poll = AsyncMock(side_effect=lambda *a, **kw: next(sequence))
    with _patch_factories(mock_plugin):
        await registry.start_device("dev_a")
        await asyncio.sleep(0.2)

    state = app_ctx.devices["dev_a"]
    assert state.last_poll_error is None
    assert state.last_successful_poll_at is not None

    await registry.stop_all()


# --- Read-drain-then-close (260611-hnk) ------------------------------------
# stop_device must drain an in-flight Modbus read before cancelling the poll
# task and closing the TCP session — cancelling mid-read + FIN is the
# suspected SE30K wedge mechanism (DESIGN-v8.3.1-restart-wedge-hardening.md).


@pytest.mark.asyncio
async def test_stop_device_drains_inflight_poll_before_close(app_ctx, entry_a):
    """stop_device waits for the in-flight poll transaction; close comes
    strictly after poll_end — the poll is NOT cancelled mid-read."""
    from pv_inverter_master.device_registry import DeviceRegistry

    config = _make_config(entry_a)
    config.proxy.poll_interval = 0.01
    registry = DeviceRegistry(app_ctx, config, on_poll_success=AsyncMock())

    order: list[str] = []
    poll_started = asyncio.Event()
    release_poll = asyncio.Event()

    async def _slow_poll():
        order.append("poll_start")
        poll_started.set()
        await release_poll.wait()
        order.append("poll_end")
        return _ok_result()

    mock_plugin = _make_mock_plugin()
    mock_plugin.poll = _slow_poll
    mock_plugin.close = AsyncMock(side_effect=lambda: order.append("close"))

    with _patch_factories(mock_plugin):
        await registry.start_device("dev_a")
        await poll_started.wait()

        # Stop concurrently while the poll is in flight; one 0-tick lets the
        # stop task queue its lock.acquire() behind the held poll lock.
        stop_task = asyncio.create_task(registry.stop_device("dev_a"))
        await asyncio.sleep(0)
        release_poll.set()
        await asyncio.wait_for(stop_task, timeout=2.0)

    assert order == ["poll_start", "poll_end", "close"]
    assert "dev_a" not in app_ctx.devices


@pytest.mark.asyncio
async def test_stop_device_closes_promptly_while_sleeping(app_ctx, entry_a):
    """With no poll in flight (loop sleeping) the lock is free — stop_device
    completes well under READ_DRAIN_TIMEOUT_S."""
    from pv_inverter_master.device_registry import DeviceRegistry

    config = _make_config(entry_a)
    config.proxy.poll_interval = 0.01
    registry = DeviceRegistry(app_ctx, config, on_poll_success=AsyncMock())

    mock_plugin = _make_mock_plugin()  # fast default _ok_result poll
    with _patch_factories(mock_plugin):
        await registry.start_device("dev_a")

        # Wait until the first poll succeeded (loop is past the locked window).
        deadline = asyncio.get_event_loop().time() + 1.0
        while app_ctx.devices["dev_a"].poll_counter["success"] == 0:
            assert asyncio.get_event_loop().time() < deadline, "no successful poll"
            await asyncio.sleep(0.005)

        await asyncio.wait_for(registry.stop_device("dev_a"), timeout=1.0)

    mock_plugin.close.assert_called_once()
    assert "dev_a" not in app_ctx.devices


@pytest.mark.asyncio
async def test_stop_device_drain_timeout_falls_back_to_cancel(
    app_ctx, entry_a, monkeypatch
):
    """A wedged/never-returning poll cannot hang shutdown: after the drain
    deadline stop_device falls back to the hard cancel (R33)."""
    from pv_inverter_master.device_registry import DeviceRegistry

    monkeypatch.setattr(
        "pv_inverter_master.device_registry.READ_DRAIN_TIMEOUT_S", 0.1
    )

    config = _make_config(entry_a)
    config.proxy.poll_interval = 0.01
    registry = DeviceRegistry(app_ctx, config, on_poll_success=AsyncMock())

    poll_started = asyncio.Event()
    never = asyncio.Event()  # never set — poll wedged forever, lock held

    async def _wedged_poll():
        poll_started.set()
        await never.wait()
        return _ok_result()

    mock_plugin = _make_mock_plugin()
    mock_plugin.poll = _wedged_poll

    with _patch_factories(mock_plugin):
        await registry.start_device("dev_a")
        await poll_started.wait()

        # Must complete despite the held lock — proves the fallback path.
        await asyncio.wait_for(registry.stop_device("dev_a"), timeout=2.0)

    mock_plugin.close.assert_called_once()
    assert "dev_a" not in app_ctx.devices
    assert registry.get_active_count() == 0


@pytest.mark.asyncio
async def test_stop_device_cancelled_mid_drain_still_cleans_up(
    app_ctx, entry_a, monkeypatch
):
    """stop_device runs in aiohttp handlers and can be CANCELLED while
    awaiting the drain (client disconnect). The finally guard must still
    cancel the poll task and close the plugin — otherwise the popped-but-
    alive poll loop holds the TCP session invisibly and a re-enable opens a
    SECOND connection on the one-slot SE30K."""
    from pv_inverter_master.device_registry import DeviceRegistry

    # Long drain window so the cancel deterministically lands mid-drain.
    monkeypatch.setattr(
        "pv_inverter_master.device_registry.READ_DRAIN_TIMEOUT_S", 30.0
    )

    config = _make_config(entry_a)
    config.proxy.poll_interval = 0.01
    registry = DeviceRegistry(app_ctx, config, on_poll_success=AsyncMock())

    poll_started = asyncio.Event()
    never = asyncio.Event()  # poll wedged forever — lock stays held

    async def _wedged_poll():
        poll_started.set()
        await never.wait()
        return _ok_result()

    mock_plugin = _make_mock_plugin()
    mock_plugin.poll = _wedged_poll

    with _patch_factories(mock_plugin):
        await registry.start_device("dev_a")
        await poll_started.wait()
        poll_task = registry._managed["dev_a"].poll_task

        stop_task = asyncio.create_task(registry.stop_device("dev_a"))
        await asyncio.sleep(0.05)  # stop_task now blocked in the drain await
        stop_task.cancel()
        # CancelledError must propagate (re-raised, not swallowed) ...
        with pytest.raises(asyncio.CancelledError):
            await stop_task

    # ... but cleanup must have run regardless: poll task cancelled,
    # plugin closed, device state removed.
    assert poll_task.done()
    assert poll_task.cancelled()
    mock_plugin.close.assert_called_once()
    assert "dev_a" not in app_ctx.devices
    assert registry.get_active_count() == 0


@pytest.mark.asyncio
async def test_stop_all_drains_in_parallel(app_ctx, monkeypatch):
    """stop_all must drain devices CONCURRENTLY: with 3 wedged devices each
    eating the full drain timeout, wall time must be ~max (1 timeout), not
    ~sum (3 timeouts) — sequential stops would blow systemd's
    TimeoutStopSec and end in SIGKILL."""
    import time as _time
    from pv_inverter_master.device_registry import DeviceRegistry

    monkeypatch.setattr(
        "pv_inverter_master.device_registry.READ_DRAIN_TIMEOUT_S", 0.2
    )

    # 5 devices: sequential drain would be ~5 × 0.2s = 1.0s vs parallel
    # ~0.2s — keeps the 0.5s assertion far from both edges (CI flake margin).
    n_devices = 5
    entries = [
        InverterEntry(
            id=f"dev_{i}",
            host=f"198.51.100.{10 + i}",
            port=502,
            enabled=True,
            type="solaredge",
        )
        for i in range(n_devices)
    ]
    config = _make_config(*entries)
    config.proxy.poll_interval = 0.01
    registry = DeviceRegistry(app_ctx, config, on_poll_success=AsyncMock())

    started = [asyncio.Event() for _ in range(n_devices)]
    never = asyncio.Event()  # all polls wedged — every drain takes ~0.2s
    plugins = []

    def _make_wedged_plugin(i):
        plugin = _make_mock_plugin()

        async def _wedged_poll():
            started[i].set()
            await never.wait()
            return _ok_result()

        plugin.poll = _wedged_poll
        plugins.append(plugin)
        return plugin

    with patch(
        "pv_inverter_master.device_registry.plugin_factory",
        side_effect=[_make_wedged_plugin(i) for i in range(n_devices)],
    ):
        await registry.start_all()
        for ev in started:
            await asyncio.wait_for(ev.wait(), timeout=1.0)

        t0 = _time.monotonic()
        await asyncio.wait_for(registry.stop_all(), timeout=2.0)
        elapsed = _time.monotonic() - t0

    # Parallel: ~1 × 0.2s drain timeout. Sequential would be ~5 × 0.2s = 1.0s.
    assert elapsed < 0.5, (
        f"stop_all took {elapsed:.2f}s — drains ran sequentially (sum), "
        f"expected parallel (max ≈ 0.2s)"
    )
    assert registry.get_active_count() == 0
    for plugin in plugins:
        plugin.close.assert_called_once()


# --- Write-path lock -------------------------------------------
# All write_power_limit call sites route through locked_write_power_limit,
# which holds the device's poll lock — so stop_device's drain covers in-flight
# WRITES too, and the deferred-restore distribute() must NOT double-acquire
# the calling device's own non-reentrant lock.


@pytest.mark.asyncio
async def test_stop_device_drains_inflight_write_before_close(app_ctx, entry_a):
    """stop_device waits for an in-flight locked WRITE; plugin.close() comes
    strictly after write_end — a webapp/distributor write racing the drain
    can never be FIN'd mid-write."""
    from pv_inverter_master.device_registry import DeviceRegistry
    from pv_inverter_master.plugin import WriteResult, locked_write_power_limit

    config = _make_config(entry_a)
    config.proxy.poll_interval = 0.01
    registry = DeviceRegistry(app_ctx, config, on_poll_success=AsyncMock())

    # Pause polling so the poll loop never competes for the lock.
    app_ctx.polling_paused = True

    order: list[str] = []
    write_started = asyncio.Event()
    release_write = asyncio.Event()

    async def _slow_write(enable, limit_pct, *, force=False):
        order.append("write_start")
        write_started.set()
        await release_write.wait()
        order.append("write_end")
        return WriteResult(success=True)

    mock_plugin = _make_mock_plugin()
    mock_plugin.write_power_limit = _slow_write
    mock_plugin.close = AsyncMock(side_effect=lambda: order.append("close"))

    with _patch_factories(mock_plugin):
        await registry.start_device("dev_a")
        poll_lock = app_ctx.devices["dev_a"].poll_lock

        write_task = asyncio.create_task(
            locked_write_power_limit(
                mock_plugin, True, 50.0, poll_lock=poll_lock,
            )
        )
        await asyncio.wait_for(write_started.wait(), timeout=2.0)

        # Stop concurrently while the write holds the lock; one 0-tick lets
        # the stop task queue its lock.acquire() behind the held write lock.
        stop_task = asyncio.create_task(registry.stop_device("dev_a"))
        await asyncio.sleep(0)
        release_write.set()
        await asyncio.wait_for(write_task, timeout=5.0)
        await asyncio.wait_for(stop_task, timeout=5.0)

    assert order == ["write_start", "write_end", "close"]
    assert "dev_a" not in app_ctx.devices


@pytest.mark.asyncio
async def test_deferred_restore_distribute_does_not_deadlock(app_ctx, entry_a):
    """The deferred-restore distribute() runs WITHOUT the outer poll-lock
    block (outer lock removed): a distribute whose write acquires the
    SAME device's lock inside (via locked_write_power_limit) must complete —
    the old outer `async with poll_lock` would double-acquire the
    non-reentrant asyncio.Lock and deadlock the poll loop forever."""
    from pv_inverter_master.device_registry import DeviceRegistry
    from pv_inverter_master.plugin import WriteResult, locked_write_power_limit

    config = _make_config(entry_a)
    config.proxy.poll_interval = 0.01
    registry = DeviceRegistry(app_ctx, config, on_poll_success=AsyncMock())

    mock_plugin = _make_mock_plugin()  # fast default _ok_result poll
    write_ran: list[bool] = []

    async def _write(enable, limit_pct, *, force=False):
        write_ran.append(True)
        return WriteResult(success=True)

    mock_plugin.write_power_limit = _write

    restored = asyncio.Event()

    class _RestoreDistributor:
        """Minimal distributor stub: distribute() takes the SAME device's
        poll lock inside — the exact double-acquire the old F3a outer
        block would hang on."""

        pending_restore_redistribute = True

        async def distribute(self, pct, enabled):
            await locked_write_power_limit(
                mock_plugin, True, pct,
                poll_lock=app_ctx.devices["dev_a"].poll_lock,
            )
            restored.set()

    app_ctx.distributor = _RestoreDistributor()
    app_ctx.control_state = MagicMock(wmaxlimpct_float=42.0, is_enabled=True)

    # Patch the collector class: when another test file imported the REAL
    # dashboard module first, the registry would build a real
    # DashboardCollector whose collect_from_raw raises on the MagicMock
    # control_state — the poll loop swallows that per-iteration and the
    # deferred restore would never be reached (import-order flake).
    with _patch_factories(mock_plugin), patch(
        "pv_inverter_master.device_registry.DashboardCollector", MagicMock
    ):
        await registry.start_device("dev_a")
        # Pre-fix this deadlocks forever (poll loop holds its own lock
        # around distribute, the inner write waits on the same lock).
        await asyncio.wait_for(restored.wait(), timeout=3.0)

    assert write_ran, "deferred-restore write never executed"
    assert app_ctx.distributor.pending_restore_redistribute is False

    await registry.stop_all()


@pytest.mark.asyncio
async def test_queued_writer_gets_result_after_stop_device(app_ctx, entry_a):
    """A writer queued behind stop_device's drain gets a clean WriteResult
    within bounded time (regression check):
    if stop_device leaked the drained lock — e.g. a cancel in the close()
    window skipping lock.release() — the queued writer would hang on
    lock.acquire() and this test would time out."""
    from pv_inverter_master.device_registry import DeviceRegistry
    from pv_inverter_master.plugin import WriteResult, locked_write_power_limit

    config = _make_config(entry_a)
    config.proxy.poll_interval = 0.01
    registry = DeviceRegistry(app_ctx, config, on_poll_success=AsyncMock())
    app_ctx.polling_paused = True

    closed = False
    write_started = asyncio.Event()
    release_write = asyncio.Event()

    async def _write(enable, limit_pct, *, force=False):
        if closed:
            return WriteResult(success=False, error="Not connected")
        write_started.set()
        await release_write.wait()
        return WriteResult(success=True)

    async def _close():
        nonlocal closed
        closed = True

    mock_plugin = _make_mock_plugin()
    mock_plugin.write_power_limit = _write
    mock_plugin.close = _close

    with _patch_factories(mock_plugin):
        await registry.start_device("dev_a")
        poll_lock = app_ctx.devices["dev_a"].poll_lock

        # First writer holds the lock mid-write.
        first_write = asyncio.create_task(
            locked_write_power_limit(mock_plugin, True, 50.0, poll_lock=poll_lock)
        )
        await asyncio.wait_for(write_started.wait(), timeout=2.0)

        # stop_device queues its drain acquire behind the held lock …
        stop_task = asyncio.create_task(registry.stop_device("dev_a"))
        await asyncio.sleep(0)
        # … and a SECOND writer queues behind stop_device (FIFO lock).
        second_write = asyncio.create_task(
            locked_write_power_limit(mock_plugin, True, 30.0, poll_lock=poll_lock)
        )
        await asyncio.sleep(0)

        release_write.set()
        await asyncio.wait_for(first_write, timeout=5.0)
        await asyncio.wait_for(stop_task, timeout=5.0)
        # Leaked lock ⇒ this hangs (timeout < WRITE_LOCK_TIMEOUT_S, so the
        # defense-in-depth fallback cannot mask the leak here).
        result = await asyncio.wait_for(second_write, timeout=5.0)

    assert result.success is False
    assert result.error == "Not connected"
    assert not poll_lock.locked(), "poll lock leaked past the queued writer"
    assert "dev_a" not in app_ctx.devices


@pytest.mark.asyncio
async def test_stop_device_cancel_mid_close_releases_lock(app_ctx, entry_a):
    """CancelledError raised inside plugin.close() (cancellation point —
    solaredge re-raises it explicitly; stop_device runs in aiohttp handlers
    cancelled on client disconnect) must NOT leak the drained lock or leave
    the device in app_ctx.devices. The nested finally makes cleanup
    unconditional; CancelledError still propagates (never swallowed)."""
    from pv_inverter_master.device_registry import DeviceRegistry

    config = _make_config(entry_a)
    config.proxy.poll_interval = 0.01
    registry = DeviceRegistry(app_ctx, config, on_poll_success=AsyncMock())
    app_ctx.polling_paused = True

    mock_plugin = _make_mock_plugin()
    mock_plugin.close = AsyncMock(side_effect=asyncio.CancelledError)

    with _patch_factories(mock_plugin):
        await registry.start_device("dev_a")
        poll_lock = app_ctx.devices["dev_a"].poll_lock

        # CancelledError propagates out of stop_device (not swallowed) …
        with pytest.raises(asyncio.CancelledError):
            await registry.stop_device("dev_a")

    # … but cleanup ran unconditionally: lock free, device removed.
    assert not poll_lock.locked(), "poll lock leaked on cancel-mid-close"
    assert "dev_a" not in app_ctx.devices


@pytest.mark.asyncio
async def test_stop_device_external_cancel_during_poll_task_await_propagates(
    app_ctx, entry_a, monkeypatch
):
    """An EXTERNAL cancel of stop_device landing exactly in the
    `await managed.poll_task` window must propagate to the caller:
    pre-fix the blanket `except CancelledError: pass` swallowed it because it
    is indistinguishable-by-type from the poll task's own CancelledError —
    stop_device then completed "normally" despite being cancelled. Cleanup
    (devices.pop, lock handling) stays guaranteed by the inner finally."""
    from pv_inverter_master.device_registry import DeviceRegistry

    # Short drain window: the wedged poll holds the lock, the drain times
    # out fast and stop_device proceeds to the hard cancel + await.
    monkeypatch.setattr(
        "pv_inverter_master.device_registry.READ_DRAIN_TIMEOUT_S", 0.05
    )

    config = _make_config(entry_a)
    config.proxy.poll_interval = 0.01
    registry = DeviceRegistry(app_ctx, config, on_poll_success=AsyncMock())

    poll_started = asyncio.Event()
    cancel_absorbed = asyncio.Event()
    release_poll = asyncio.Event()
    never = asyncio.Event()

    async def _wedged_poll():
        poll_started.set()
        try:
            await never.wait()
        except asyncio.CancelledError:
            # Absorb the hard cancel and stay wedged — keeps stop_device
            # suspended in `await managed.poll_task`, so the test's external
            # cancel deterministically lands in exactly that window.
            cancel_absorbed.set()
            await release_poll.wait()
            raise
        return _ok_result()

    mock_plugin = _make_mock_plugin()
    mock_plugin.poll = _wedged_poll

    with _patch_factories(mock_plugin):
        await registry.start_device("dev_a")
        poll_lock = app_ctx.devices["dev_a"].poll_lock
        poll_task = registry._managed["dev_a"].poll_task
        await asyncio.wait_for(poll_started.wait(), timeout=2.0)

        stop_task = asyncio.create_task(registry.stop_device("dev_a"))
        # Drain times out (lock held by the wedged poll), hard cancel fires,
        # the poll absorbs it — stop_task is now parked in `await poll_task`.
        await asyncio.wait_for(cancel_absorbed.wait(), timeout=2.0)
        await asyncio.sleep(0.05)
        assert not stop_task.done()

        stop_task.cancel()
        # Pre-fix this hangs in the swallow path: stop_task completes
        # normally and pytest.raises fails. Post-fix the cancelling() delta
        # check re-raises our cancellation.
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(stop_task, timeout=2.0)

        # Cleanup ran via the inner finally: device removed.
        assert "dev_a" not in app_ctx.devices
        assert registry.get_active_count() == 0

        # Once the wedged poll finally honors its cancellation, the poll
        # loop's `async with` releases the lock — nothing leaked, lock free.
        release_poll.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(poll_task, timeout=2.0)
        assert poll_task.cancelled()
        assert not poll_lock.locked(), "poll lock leaked after external cancel"


@pytest.mark.asyncio
async def test_locked_write_times_out_on_leaked_lock(monkeypatch):
    """Defense-in-depth: locked_write_power_limit's lock acquire is bounded —
    against a never-freed (leaked) lock it returns a clean failed
    WriteResult(error='write_lock_timeout') instead of hanging forever."""
    import pv_inverter_master.plugin as plugin_mod
    from pv_inverter_master.plugin import WriteResult, locked_write_power_limit

    monkeypatch.setattr(plugin_mod, "WRITE_LOCK_TIMEOUT_S", 0.05)

    mock_plugin = MagicMock()
    mock_plugin.write_power_limit = AsyncMock(
        return_value=WriteResult(success=True)
    )

    leaked_lock = asyncio.Lock()
    await leaked_lock.acquire()  # held forever — simulated leak

    result = await asyncio.wait_for(
        locked_write_power_limit(
            mock_plugin, True, 50.0, poll_lock=leaked_lock,
        ),
        timeout=2.0,
    )

    assert result.success is False
    assert result.error == "write_lock_timeout"
    mock_plugin.write_power_limit.assert_not_called()
    assert leaked_lock.locked()  # still held by the leaker, not stolen


@pytest.mark.asyncio
async def test_backoff_on_failure(app_ctx, entry_a):
    """After poll failure, sleep_duration follows ConnectionManager backoff."""
    from pv_inverter_master.device_registry import DeviceRegistry

    config = _make_config(entry_a)
    on_success = AsyncMock()
    registry = DeviceRegistry(app_ctx, config, on_poll_success=on_success)

    # Override proxy poll_interval so the poll loop runs fast
    config.proxy.poll_interval = 0.01

    mock_plugin = _make_mock_plugin(poll_result=_fail_result())
    with _patch_factories(mock_plugin):
        await registry.start_device("dev_a")
        # Let enough poll failures happen (need 3+ for RECONNECTING with 3-strike rule)
        await asyncio.sleep(0.3)

    state = app_ctx.devices.get("dev_a")
    if state and state.conn_mgr:
        cm = state.conn_mgr
        # After 3+ failures, state should be RECONNECTING and backoff > initial
        assert cm.state != ConnectionState.CONNECTED
        assert cm.sleep_duration >= ConnectionManager.INITIAL_BACKOFF

    await registry.stop_all()


@pytest.mark.asyncio
async def test_poll_gap_none_on_first_success_then_measures_spacing(app_ctx, entry_a):
    """last_poll_gap_s stays None until a device has succeeded twice.

    The gap is what the staleness-floor calibration needs: at the moment of a
    success the AGE is ~0 by construction, so only the spacing between
    successes carries information.
    """
    from pv_inverter_master.device_registry import DeviceRegistry

    config = _make_config(entry_a)
    config.proxy.poll_interval = 0.01
    registry = DeviceRegistry(app_ctx, config, on_poll_success=AsyncMock())

    mock_plugin = _make_mock_plugin()
    with _patch_factories(mock_plugin):
        await registry.start_device("dev_a")
        # One poll in: a first success has no predecessor to measure against.
        await asyncio.sleep(0.005)
        ds = app_ctx.devices["dev_a"]
        assert ds.last_poll_gap_s is None
        # Let it poll repeatedly, then the gap must be a real positive spacing.
        await asyncio.sleep(0.1)
        gap = ds.last_poll_gap_s

    await registry.stop_all()

    assert gap is not None
    assert gap > 0.0
    # Sanity bound: the loop sleeps ~0.01s, so a gap of seconds would mean the
    # value is measuring something other than consecutive successes.
    assert gap < 1.0
