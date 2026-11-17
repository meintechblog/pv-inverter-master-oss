"""DeviceRegistry: per-device poll lifecycle management.

Manages N inverter devices with independent poll loops as asyncio tasks.
Supports runtime add/remove/enable/disable with clean lifecycle management.
Each device gets its own plugin, ConnectionManager, DashboardCollector,
and poll counter.
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import structlog

from pv_inverter_master.config import Config, InverterEntry, get_gateway_for_inverter
from pv_inverter_master.modbus_gateway import ModbusGateway, peek_gateway
from pv_inverter_master.connection import ConnectionManager
from pv_inverter_master.context import AppContext, DeviceState
from pv_inverter_master.dashboard import DashboardCollector
from pv_inverter_master.plugins import plugin_factory

logger = structlog.get_logger()

# Read-drain deadline for stop_device (260611-hnk): worst case in flight is
# two sequential Modbus reads, each up to (retries+1) attempts at the client
# timeout — with timeout=2s, retries=1 that is 2 reads × 2 attempts × 2s = 8s.
# 10s adds margin on top. On expiry we fall back to the hard cancel —
# shutdown must never hang (R33 harte Deadline). Module-level lookup so
# tests can monkeypatch it. stop_all drains all devices in PARALLEL, so
# wall time stays = max (≈10s) and fits inside TimeoutStopSec.
READ_DRAIN_TIMEOUT_S = 10.0
# Hard limit for the EXTENDED stop drain: while the host gateway still runs a
# multi-step block (connect + DPC init, verify) stop keeps waiting instead of
# cancelling it mid-transaction. Must stay below TimeoutStopSec (service unit).
STOP_HARD_DRAIN_S = 40.0


def _extract_ac_power(inverter_registers: list[int]) -> float | None:
    """Extract AC power (W) from Model 103 inverter registers.

    Model 103 layout: DID(0) + Len(1) + 50 data fields.
    AC Power at data offset 12 (register index 14), scale factor at data offset 13 (register index 15).
    Values are unsigned 16-bit. Scale factor is signed 16-bit (two's complement).
    """
    if len(inverter_registers) < 16:
        return None
    raw_power = inverter_registers[14]
    raw_sf = inverter_registers[15]
    if raw_power == 0xFFFF or raw_sf == 0x8000:
        return None  # Not implemented / invalid
    # Convert unsigned to signed for scale factor
    sf = raw_sf if raw_sf < 0x8000 else raw_sf - 0x10000
    return float(raw_power) * (10.0 ** sf)


@dataclass
class ManagedDevice:
    """A device managed by the registry with its poll task."""

    entry: InverterEntry
    plugin: object  # InverterPlugin
    device_state: DeviceState
    poll_task: asyncio.Task | None = None


class DeviceRegistry:
    """Manages N inverter devices with independent poll loops.

    Each device gets its own asyncio poll task, plugin instance,
    ConnectionManager for backoff, and DashboardCollector.
    """

    def __init__(
        self,
        app_ctx: AppContext,
        config: Config,
        on_poll_success: Callable[[str], Awaitable[None]],
    ) -> None:
        self._app_ctx = app_ctx
        self._config = config
        self._on_poll_success = on_poll_success
        self._managed: dict[str, ManagedDevice] = {}
        self._distributor: object | None = None

    @property
    def distributor(self) -> object | None:
        """Public accessor for the PowerLimitDistributor instance."""
        return self._distributor

    async def start_device(self, device_id: str) -> None:
        """Start polling for a single device by its id.

        Finds the InverterEntry in config, creates plugin via plugin_factory,
        sets up DeviceState with ConnectionManager and DashboardCollector,
        and starts an asyncio poll task.

        Skips if entry is not enabled or device_id not found.
        """
        entry = self._find_entry(device_id)
        if entry is None:
            logger.warning("device_not_found", device_id=device_id)
            return
        if not entry.enabled:
            logger.info("device_disabled_skipped", device_id=device_id)
            return

        # Poll interval precedence: explicit per-device override >
        # gateway config > global proxy default.
        gateway_config = get_gateway_for_inverter(self._config, entry)
        if entry.poll_interval and entry.poll_interval > 0:
            poll_interval = entry.poll_interval
        elif gateway_config:
            poll_interval = gateway_config.poll_interval
        else:
            poll_interval = self._config.proxy.poll_interval

        plugin = plugin_factory(entry, gateway_config, data_dir=self._data_dir())

        # Respect a plugin-declared minimum poll interval. Some devices
        # (Hoymiles HMS built-in DTUs) are overwhelmed by fast polling
        # and return degraded data — clamp the interval up regardless of
        # the configured/global value.
        plugin_floor = getattr(plugin, "min_poll_interval", 0.0) or 0.0
        if plugin_floor > poll_interval:
            logger.info(
                "poll_interval_floored",
                device_id=device_id,
                type=entry.type,
                requested=poll_interval,
                floored_to=plugin_floor,
            )
            poll_interval = plugin_floor

        # Create per-device state. Persistent Modbus-TCP inverters (SolarEdge,
        # Sungrow) opt into wedge quiesce: their SunSpec server can wedge (TCP
        # up, every request times out) and only releases after minutes of true
        # client silence — see DESIGN-v8.3.1-restart-wedge-hardening.md.
        conn_mgr = ConnectionManager(
            poll_interval=poll_interval,
            wedge_quiesce=entry.type in ("solaredge", "sungrow"),
        )
        collector = DashboardCollector()
        poll_counter = {"success": 0, "total": 0}

        device_state = DeviceState(
            collector=collector,
            poll_counter=poll_counter,
            conn_mgr=conn_mgr,
            plugin=plugin,
            # Constructed here (inside the running loop) instead of a dataclass
            # default_factory, avoiding loop-binding pitfalls at import or test
            # collection time. Guards the in-flight Modbus poll transaction so
            # stop_device can drain before cancel (read-drain-then-close).
            poll_lock=asyncio.Lock(),
            # Mirror the resolved interval so the aggregation freshness gate
            # can bound this device's staleness by its OWN cadence (a device
            # legitimately polled every 60 s must not be judged by a 5 s rule).
            poll_interval=poll_interval,
        )

        managed = ManagedDevice(
            entry=entry,
            plugin=plugin,
            device_state=device_state,
        )

        # Register in managed dict and app context
        self._managed[device_id] = managed
        self._app_ctx.devices[device_id] = device_state

        # Start poll task
        task = asyncio.create_task(
            _device_poll_loop(
                device_id=device_id,
                plugin=plugin,
                device_state=device_state,
                poll_interval=poll_interval,
                on_success=self._on_poll_success,
                app_ctx=self._app_ctx,
            ),
            name=f"poll-{device_id}",
        )
        managed.poll_task = task

        logger.info("device_started", device_id=device_id, host=entry.host, type=entry.type)

    async def stop_device(self, device_id: str) -> None:
        """Stop polling for a device: drain in-flight read, cancel task,
        close plugin, remove state.

        Read-drain-then-close (260611-hnk): cancelling the poll task mid-
        Modbus-read and sending FIN leaves the SE30K SunSpec server with a
        half-finished transaction — the v8.3.x wedge signature (see
        DESIGN-v8.3.1-restart-wedge-hardening.md). We acquire the per-device
        poll lock first so the in-flight transaction completes; on timeout we
        fall back to today's hard cancel so shutdown never hangs.
        """
        managed = self._managed.pop(device_id, None)
        if managed is None:
            return

        # Drain the in-flight poll before cancelling. While we hold the lock,
        # the loop is either in asyncio.sleep or blocked on lock.acquire() —
        # both cancel-safe, no Modbus transaction in flight. asyncio.Lock is
        # FIFO-fair, so the loop's next acquire cannot jump our queued one.
        #
        # The whole drain→cancel→close sequence is try/finally-guarded:
        # stop_device runs inside aiohttp request handlers, which are
        # CANCELLED on client disconnect. Without the finally, a cancel
        # landing in the drain await would orphan the still-running poll
        # loop (device already popped from _managed — invisible, holding
        # the TCP session; re-enable would open a SECOND connection on the
        # one-slot SE30K) and leak the acquired lock / skip plugin.close().
        # CancelledError still propagates after cleanup — re-raised by the
        # runtime for a cancel landing in the drain window, re-raised
        # explicitly for one landing in the `await poll_task` window
        # (cancelling() delta check).
        drained = False
        lock = managed.device_state.poll_lock if managed.device_state else None
        try:
            if (
                lock is not None
                and managed.poll_task is not None
                and not managed.poll_task.done()
            ):
                t0 = time.monotonic()
                try:
                    await asyncio.wait_for(
                        lock.acquire(), timeout=READ_DRAIN_TIMEOUT_S
                    )
                    drained = True
                    logger.info(
                        "read_drain_ok",
                        device_id=device_id,
                        wait_s=round(time.monotonic() - t0, 3),
                    )
                except asyncio.TimeoutError:
                    # A multi-step gateway block (connect + DPC init, verify)
                    # can outlive the normal drain. Cancelling it now would
                    # abort a transaction on the wire and the close below would
                    # FIN the session mid-request, so keep waiting while the
                    # host gateway is still busy - up to a hard limit.
                    gw = peek_gateway(managed.plugin)
                    if gw is not None and gw.busy:
                        logger.warning(
                            "read_drain_extended",
                            device_id=device_id,
                            hard_limit_s=STOP_HARD_DRAIN_S,
                            msg="gateway transaction block still running - waiting",
                        )
                        try:
                            await asyncio.wait_for(
                                lock.acquire(),
                                timeout=STOP_HARD_DRAIN_S - READ_DRAIN_TIMEOUT_S,
                            )
                            drained = True
                        except asyncio.TimeoutError:
                            pass
                    if not drained:
                        logger.warning(
                            "read_drain_timeout",
                            device_id=device_id,
                            timeout_s=READ_DRAIN_TIMEOUT_S,
                            msg="in-flight poll did not drain — falling back to hard cancel",
                        )
        finally:
            # Nested try/finally: the awaits
            # below (`await managed.poll_task`, `await managed.plugin.close()`)
            # are cancellation points — solaredge close() re-raises
            # CancelledError explicitly, and stop_device runs inside aiohttp
            # handlers that get cancelled on client disconnect. Without the
            # inner finally, a cancel landing in the close() window would skip
            # lock.release() + devices.pop — the drained poll_lock stays held
            # FOREVER, every queued writer (locked_write_power_limit) hangs,
            # and the distribute()-gather / Venus path / updater maintenance
            # drain are dead. The inner finally makes release + pop
            # UNCONDITIONAL. CancelledError propagation: a cancel already in
            # flight (landed in the drain await) resumes by itself after this
            # finally; a cancel landing in the `await managed.poll_task`
            # window below is detected and re-raised explicitly.
            try:
                # Cancel poll task — guaranteed even if the drain await above
                # was cancelled.
                if managed.poll_task is not None and not managed.poll_task.done():
                    managed.poll_task.cancel()
                    # An EXTERNAL cancel of
                    # stop_device itself lands exactly in the await below and
                    # is indistinguishable-by-type from the poll task's own
                    # CancelledError — a blanket `pass` would swallow it and
                    # stop_device would complete "normally" despite being
                    # cancelled (caller's `await stop_task` never raises).
                    # Snapshot cancelling() and re-raise only on a NEW cancel
                    # request delivered AT this await. A delta check (not a
                    # bare `if cancelling()`) so a cancel that already landed
                    # in the drain window — currently propagating through the
                    # outer finally, resumed by the runtime afterwards — does
                    # NOT trigger an early re-raise that would skip
                    # plugin.close().
                    self_task = asyncio.current_task()
                    pre_cancelling = (
                        self_task.cancelling() if self_task is not None else 0
                    )
                    try:
                        await managed.poll_task
                    except asyncio.CancelledError:
                        if (
                            self_task is not None
                            and self_task.cancelling() > pre_cancelling
                        ):
                            # Our own cancellation, delivered at this await —
                            # propagate. lock.release() + devices.pop stay
                            # guaranteed by the inner finally below.
                            raise
                        # The poll task's own cancellation — swallow. Any
                        # cancel of US that landed earlier (drain window) is
                        # re-raised by the runtime when the outer finally
                        # exits.

                # Close plugin BEFORE releasing the drained lock. Writers
                # (locked_write_power_limit) now queue on this lock;
                # releasing before close() would let a queued write acquire and
                # hit the client exactly as close() FINs it — reintroducing the
                # race the drain exists to prevent. Released after close(),
                # a queued writer acquires post-close, finds _client None, and
                # gets a clean "Not connected" WriteResult instead.
                try:
                    await managed.plugin.close()
                except Exception:
                    logger.debug(
                        "plugin_close_error", device_id=device_id, exc_info=True
                    )
                # Device is gone for good (not a quiesce close): drop the
                # host claim so the scanner may look at the address again.
                if peek_gateway(managed.plugin) is not None:
                    managed.plugin.release()
            finally:
                if drained:
                    lock.release()

                # Remove from app context
                self._app_ctx.devices.pop(device_id, None)

                logger.info("device_stopped", device_id=device_id)

    async def start_all(self) -> None:
        """Start poll loops for all enabled inverters in config."""
        for entry in self._config.inverters:
            if entry.enabled:
                await self.start_device(entry.id)

    async def stop_all(self) -> None:
        """Stop all managed devices in parallel.

        Each stop_device can spend up to READ_DRAIN_TIMEOUT_S draining its
        in-flight Modbus read. Sequential stops would make shutdown wall
        time the SUM of all drains (N × 10s blows past systemd's
        TimeoutStopSec → SIGKILL); gather makes it the MAX (≈ one drain).
        """
        device_ids = list(self._managed.keys())
        if not device_ids:
            return
        results = await asyncio.gather(
            *(self.stop_device(device_id) for device_id in device_ids),
            return_exceptions=True,
        )
        for device_id, result in zip(device_ids, results):
            if isinstance(result, BaseException):
                logger.warning(
                    "stop_device_failed",
                    device_id=device_id,
                    error=repr(result),
                )

    async def enable_device(self, device_id: str) -> None:
        """Start a device (caller must set entry.enabled=True first)."""
        await self.start_device(device_id)

    async def disable_device(self, device_id: str) -> None:
        """Stop and clean up a device."""
        await self.stop_device(device_id)

    def get_active_device_ids(self) -> list[str]:
        """Return list of currently managed device ids."""
        return list(self._managed.keys())

    def get_active_count(self) -> int:
        """Return number of currently managed devices."""
        return len(self._managed)

    def _find_entry(self, device_id: str) -> InverterEntry | None:
        """Find InverterEntry by id in config."""
        for entry in self._config.inverters:
            if entry.id == device_id:
                return entry
        return None

    def _data_dir(self) -> str | None:
        """Writable directory for plugins that persist runtime state."""
        config_path = getattr(self._app_ctx, "config_path", None)
        if not config_path:
            return None
        return os.path.dirname(os.path.abspath(config_path))


_CONNECT_TASKS: set[asyncio.Future] = set()


async def _connect_shielded(plugin) -> None:
    """Run ``plugin.connect()`` (connect + DPC init: a multi-transaction block)
    so that cancelling the poll task - stop after the drain limit, cancelled
    stop request - does NOT abort it halfway. The block
    finishes in the background; ``plugin.close()`` queues behind it on the
    gateway lock and closes the session only afterwards."""
    task = asyncio.ensure_future(plugin.connect())
    _CONNECT_TASKS.add(task)
    task.add_done_callback(_CONNECT_TASKS.discard)
    # An exception of a block nobody awaits any more must not go unobserved.
    task.add_done_callback(lambda t: t.cancelled() or t.exception())
    await asyncio.shield(task)


async def wait_connect_tasks(timeout: float) -> bool:
    """Shutdown: wait (bounded) for detached connect+DPC blocks to finish.

    Returns True if none is left. Leftovers after ``timeout`` are cancelled
    (the process is going away; nothing can be done for them any more).
    """
    tasks = [t for t in _CONNECT_TASKS if not t.done()]
    if not tasks:
        return True
    _done, pending = await asyncio.wait(tasks, timeout=timeout)
    for t in pending:
        t.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
        logger.warning("connect_tasks_abandoned_at_shutdown", count=len(pending))
    return not pending


async def _device_poll_loop(
    device_id: str,
    plugin: object,
    device_state: DeviceState,
    poll_interval: float,
    on_success: Callable[[str], Awaitable[None]],
    app_ctx: AppContext,
) -> None:
    """Per-device poll loop running as an asyncio task.

    Simplified version of proxy.py _poll_loop without night mode injection,
    common translation, or cache writes (those belong to AggregationLayer in Plan 02).

    Args:
        device_id: Unique identifier for the device.
        plugin: InverterPlugin instance.
        device_state: DeviceState with conn_mgr, poll_counter, etc.
        poll_interval: Base poll interval in seconds.
        on_success: Async callback invoked with device_id on successful poll.
        app_ctx: Application context for checking polling_paused.
    """
    log = logger.bind(device_id=device_id)
    conn_mgr = device_state.conn_mgr
    poll_counter = device_state.poll_counter
    # Local fallback keeps direct-call tests and legacy DeviceState
    # constructions (no poll_lock) working — the guard is then a no-op
    # private lock with zero contention.
    poll_lock = device_state.poll_lock or asyncio.Lock()

    # Initial connect (+ DPC init, a multi-transaction block). Under the poll
    # lock like every later reconnect: stop_device drains this lock, so a stop
    # during the initial connect waits for it instead of cancelling it (and
    # then FIN-ing the session) in the middle of a transaction.
    if hasattr(plugin, "connect"):
        async with poll_lock:
            try:
                await _connect_shielded(plugin)
                log.info("plugin_connected")
            except Exception as exc:
                log.warning("initial_connect_failed", error=str(exc))

    while True:
        # Skip polling when paused
        if app_ctx.polling_paused:
            await asyncio.sleep(poll_interval)
            continue

        try:
            # The lock covers the Modbus-traffic window: reconnect + poll.
            # WRITES self-lock per target device inside the distributor's
            # _send_limit (locked_write_power_limit) — including
            # the deferred-restore distribute() below, which therefore must
            # NOT be wrapped here (non-reentrant lock). Pure bookkeeping
            # stays outside — holding the lock through non-Modbus work would
            # inflate stop_device's drain time for no safety gain. `async
            # with` releases the lock on cancel (read-drain-then-close,
            # 260611-hnk).
            async with poll_lock:
                # Reconnect if needed. _client is None after a quiesce close
                # (or plugin.close()) — treat that as "needs reconnect" too; a
                # bare `not getattr(None, "connected", True)` would skip it
                # forever.
                if hasattr(plugin, "_client"):
                    client = plugin._client
                    if client is None or not getattr(client, "connected", True):
                        try:
                            await _connect_shielded(plugin)
                            log.info("plugin_reconnected")
                        except Exception:
                            log.debug("plugin_reconnect_failed", exc_info=True)

                result = await plugin.poll()
            poll_counter["total"] += 1
            # 260528-hhj: record every poll attempt timestamp before branching
            # on success/failure so the UI can show "last attempt" relative
            # time even when the device has never succeeded.
            device_state.last_poll_at = time.time()

            if result.success:
                poll_counter["success"] += 1
                # 260528-hhj: success clears any stale error and stamps the
                # last-success timestamp the UI shows in its status card.
                device_state.last_successful_poll_at = time.time()
                # Monotonic twin for age checks that must not be fooled by an
                # NTP step (aggregation freshness gate). Written here so the
                # two stamps can never drift apart.
                _prev_ok_mono = device_state.last_successful_poll_monotonic
                device_state.last_successful_poll_monotonic = time.monotonic()
                # Gap since the PREVIOUS success, for the Influx telemetry that
                # the staleness-floor calibration needs. The gap is the useful
                # quantity, not the age: at the moment of a successful poll the
                # age is ~0 by construction, so sampling it here would measure
                # nothing. The gap distribution bounds it — a device whose
                # successes are X apart is at most X old between them.
                #
                # It is intentionally NOT derived from poll_interval: the real
                # spacing is driven by connection backoff (5s -> 60s) and by
                # multi-read cycles, which is exactly the decoupling that put
                # the aggregation floor at 120s. Measured, not assumed.
                # None on the first success after start (no previous point).
                device_state.last_poll_gap_s = (
                    device_state.last_successful_poll_monotonic - _prev_ok_mono
                    if _prev_ok_mono is not None
                    else None
                )
                device_state.last_poll_error = None
                # Carry the plugin's source-stale flag (success but no fresh
                # data: OpenDTU radio gap). Default False for plugins that
                # never set it, so /api/devices consumers see "not stale".
                device_state.source_stale = bool(getattr(result, "stale", False))
                conn_mgr.on_poll_success()

                # Store raw poll data for register viewer
                device_state.last_poll_data = {
                    "common_registers": result.common_registers,
                    "inverter_registers": result.inverter_registers,
                }

                # Get nameplate registers from plugin for rated power
                nameplate_regs = None
                if hasattr(plugin, "get_model_120_registers"):
                    try:
                        nameplate_regs = plugin.get_model_120_registers()
                    except Exception:
                        log.debug("nameplate_register_error", exc_info=True)

                # Update DashboardCollector with decoded snapshot
                if device_state.collector is not None:
                    device_state.collector.collect_from_raw(
                        common_registers=result.common_registers,
                        inverter_registers=result.inverter_registers,
                        conn_mgr=conn_mgr,
                        poll_counter=poll_counter,
                        control_state=getattr(app_ctx, "control_state", None),
                        app_ctx=app_ctx,
                        nameplate_registers=nameplate_regs,
                    )

                distributor = getattr(app_ctx, 'distributor', None)
                if distributor is not None and hasattr(distributor, 'on_poll'):
                    ac_power_w = _extract_ac_power(result.inverter_registers)
                    if ac_power_w is not None:
                        distributor.on_poll(device_id, ac_power_w)

                # Persist plugin-resolved fields back to the InverterEntry
                # so they show up in the UI and survive restarts. Currently
                # only OpenDTU does this (auto-adopting the gateway's only
                # inverter when serial is empty). Best-effort — a save
                # failure must not break polling.
                try:
                    plugin_serial = getattr(plugin, "serial", None) or ""
                    plugin_fw = getattr(plugin, "firmware_version", None) or ""
                    config = getattr(app_ctx, "config", None)
                    if config is not None and (plugin_serial or plugin_fw):
                        for entry in config.inverters:
                            if entry.id != device_id:
                                continue
                            dirty = False
                            if plugin_serial and entry.serial != plugin_serial:
                                entry.serial = plugin_serial
                                dirty = True
                            if plugin_fw and entry.firmware_version != plugin_fw:
                                entry.firmware_version = plugin_fw
                                dirty = True
                            if dirty:
                                from pv_inverter_master.config import save_config
                                save_config(app_ctx.config_path, config)
                                log.info(
                                    "entry_persisted",
                                    serial=plugin_serial,
                                    firmware_version=plugin_fw,
                                )
                            break
                except Exception:
                    log.debug("entry_persist_failed", exc_info=True)

                # Deferred boot limit-restore (poll-first, DESIGN v8.3.1):
                # the restore write is held until the FIRST successful poll
                # of any device, then re-run with the CURRENT ControlState
                # value (never a boot-time snapshot — a fresher Venus write
                # clears the flag inside distribute()).
                distributor = getattr(app_ctx, "distributor", None)
                if distributor is not None and getattr(
                    distributor, "pending_restore_redistribute", False
                ):
                    distributor.pending_restore_redistribute = False
                    cs = getattr(app_ctx, "control_state", None)
                    if cs is not None:
                        try:
                            # NO outer poll_lock here: distribute()'s writes self-lock per
                            # TARGET device inside _send_limit via
                            # locked_write_power_limit. Our own device's
                            # lock is free at this point (released after the
                            # poll above), so _send_limit can take it; and
                            # distribute touching OTHER devices takes THEIR
                            # locks independently — no lock is held across
                            # another device's acquire, no ordering cycle.
                            # An outer async-with on the poll lock would
                            # double-acquire this device's own non-reentrant
                            # asyncio.Lock inside _send_limit → permanent
                            # poll-loop deadlock.
                            await distributor.distribute(
                                cs.wmaxlimpct_float, cs.is_enabled
                            )
                            log.info(
                                "deferred_limit_restore_applied",
                                pct=cs.wmaxlimpct_float,
                                enabled=cs.is_enabled,
                            )
                        except Exception:
                            log.warning("deferred_limit_restore_failed", exc_info=True)

                await on_success(device_id)
                log.debug("poll_success")
            else:
                # 260528-hhj: surface soft-fail error to the UI; truncate so a
                # multi-KB traceback can't bloat the WS push.
                device_state.last_poll_error = (
                    str(result.error)[:200] if result.error else "unknown error"
                )
                conn_mgr.on_poll_failure()
                log.warning("poll_failed", error=result.error, state=conn_mgr.state.value)
                await _close_for_quiesce(plugin, conn_mgr, log)

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            poll_counter["total"] += 1
            # 260528-hhj: an exception in plugin.poll() (network blow-up,
            # parser bug, ...) is also a poll attempt — record it the same
            # way as a soft-fail, but never touch last_successful_poll_at.
            device_state.last_poll_at = time.time()
            device_state.last_poll_error = str(exc)[:200]
            conn_mgr.on_poll_failure()
            log.error("poll_error", error=str(exc), exc_info=True)
            await _close_for_quiesce(plugin, conn_mgr, log)

        await asyncio.sleep(conn_mgr.sleep_duration)


async def _close_for_quiesce(plugin: object, conn_mgr: object, log) -> None:
    """Close the plugin connection when a failure newly entered QUIESCE.

    The wedge only releases when the device sees TRUE zero-client silence,
    so dropping our TCP session is the whole point of the quiesce window.
    """
    if not getattr(conn_mgr, "quiesce_entered", False):
        return
    try:
        # Gateway first: from this moment NO caller (web test button, scanner,
        # writers) may touch the host until the window ends.
        # The gateway window mirrors the ConnectionManager's window EXACTLY:
        # the remaining time of the manager's current window, not a fresh full
        # duration per signal. The manager re-signals after every failed
        # recovery probe but only re-arms its window after several probes; a
        # fresh gateway window per signal would refuse the probes it plans.
        gateway = peek_gateway(plugin)
        if gateway is not None:
            remaining = getattr(conn_mgr, "quiesce_remaining", None)
            if remaining is None:
                remaining = getattr(conn_mgr, "QUIESCE_DURATION", 480.0)
            if remaining > 0:
                gateway.enter_quiesce(remaining)
        if hasattr(plugin, "close"):
            await plugin.close()
        log.warning(
            "quiesce_entered",
            duration_s=getattr(conn_mgr, "QUIESCE_DURATION", None),
            msg="suspected Modbus wedge -- closing connection, suspending all traffic",
        )
    except Exception:
        log.debug("quiesce_close_failed", exc_info=True)
