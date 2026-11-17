"""Proxy orchestration: Modbus TCP server + staleness-aware slave context.

Provides the Modbus TCP server infrastructure that Venus OS reads from.
The register cache is populated by AggregationLayer (Phase 22+), not by
a built-in poll loop. When cache goes stale (30s without successful poll),
returns Modbus exception 0x04 to Venus OS.

The old _poll_loop and run_proxy functions have been replaced by DeviceRegistry
(per-device poll loops) + AggregationLayer (SunSpec aggregation) + run_modbus_server
(server-only setup without polling).
"""
from __future__ import annotations

import asyncio
import logging
import time

from pymodbus.datastore import (
    ModbusSequentialDataBlock,
    ModbusDeviceContext,
    ModbusServerContext,
)
from pymodbus.datastore.context import ExcCodes
from pymodbus.server import ModbusTcpServer

import structlog

# Plan 45-05 RESTART-01: Maintenance-mode write strategy. "slavebusy"
# rejects Model 123 writes with Modbus exception 0x06 (DEVICE_BUSY),
# which is the standard retryable exception that Venus OS is expected
# to back off on. Flip to "silent_drop" if live testing shows Venus
# OS disconnects on the exception — see
# src/pv_inverter_master/updater/maintenance.py for the decision record.
#
# RESTART-06: pymodbus 3.12 ModbusProtocol passes ``reuse_address=True``
# to asyncio.loop.create_server() by default (verified via
# ``inspect.getsource(ModbusProtocol)`` in Plan 45-05 Task 3). No
# explicit SO_REUSEADDR patch needed on pymodbus 3.8+.
MAINTENANCE_STRATEGY = "slavebusy"

from pv_inverter_master.control import (
    ControlState,
    HYSTERESIS_PCT,
    MIN_HOLD_AFTER_DOWN_S,
    MODEL_123_START,
    NEAR_FULL_PCT,
    WMAXLIMPCT_OFFSET,
    WMAXLIM_ENA_OFFSET,
    validate_wmaxlimpct,
)
from pv_inverter_master.sunspec_models import (
    build_initial_registers,
    DATABLOCK_START,
    PROXY_UNIT_ID,
)
from pv_inverter_master.register_cache import RegisterCache

logger = logging.getLogger(__name__)
control_log = structlog.get_logger(component="control")

# Polling interval in seconds (locked decision from CONTEXT.md)
POLL_INTERVAL = 1.0

#: Venus OS writes the Model 123 block as several consecutive Modbus requests
#: (WMaxLimPct, WMaxLim_Ena, ...). Each one used to be distributed to the
#: inverters on its own, i.e. several wire write blocks per regulation tick.
#: Writes arriving within this window share ONE distribution of the final
#: ControlState; every writer still returns only after that distribution.
DISTRIBUTE_COALESCE_S = 0.25
# Failed background distribution: retries (with doubling backoff).
FLUSH_MAX_RETRIES = 3
FLUSH_RETRY_BACKOFF_S = 0.5

# Cache staleness timeout (locked decision from CONTEXT.md)
STALENESS_TIMEOUT = 30.0

# Datablock addresses for cache updates (DATABLOCK_START-relative)
# Common Model: 67 registers starting at datablock address 40003 (40002 + 1 offset)
COMMON_CACHE_ADDR = 40003
# Inverter Model: 52 registers starting at datablock address 40070 (40069 + 1 offset)
INVERTER_CACHE_ADDR = 40070


class StalenessAwareSlaveContext(ModbusDeviceContext):
    """ModbusDeviceContext that returns Modbus exception 0x04 when cache is stale.

    Per locked decision: after 30s without a successful poll, start returning
    Modbus errors to Venus OS instead of serving stale data.

    Overrides getValues() to check RegisterCache.is_stale before reading.
    When stale, raises ModbusIOException with exception_code=0x04
    (SLAVE_DEVICE_FAILURE), which pymodbus translates into a proper
    Modbus exception response to the client.

    Overrides async_setValues() to intercept writes to Model 123 registers
    for power control forwarding to the inverter.
    """

    def __init__(
        self,
        cache: RegisterCache,
        plugin: object | None = None,
        control_state: ControlState | None = None,
        app_ctx: object | None = None,
        distributor: object | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._cache = cache
        self._plugin = plugin
        self._control = control_state
        self._app_ctx = app_ctx
        self._distributor = distributor
        self.last_successful_read: float = 0.0
        self.read_count: int = 0
        # Asymmetric write-hysteresis state. ``_last_applied_pct`` is the
        # Venus OS WMaxLimPct value most recently propagated to
        # PowerLimitDistributor.distribute(); ``_last_applied_ts`` is the
        # monotonic timestamp of that call. Both stay None/0 until the
        # first Venus OS write so the very first command always passes
        # through regardless of size or direction.
        self._last_applied_pct: float | None = None
        self._last_applied_ts: float = 0.0
        # Filter stats — counters incremented on every Model 123 write so
        # the webapp can show a "X writes since boot, Y filtered" card
        # without scrolling the override_log ring buffer. ``filtered_other``
        # is a catch-all bucket: future filter reasons (e.g. slew-limit)
        # land here unless an explicit counter is added so we never lose
        # an event to a typo'd bucket name.
        self._writes_total: int = 0
        self._writes_applied: int = 0
        self._pending_flush: asyncio.Future | None = None
        self._flush_tasks: set[asyncio.Future] = set()
        self._flush_dirty: bool = False
        self.flush_error: bool = False
        self.flush_failures_total: int = 0
        self._dist_lock = asyncio.Lock()
        self.distributions_total: int = 0
        self._writes_filtered_below_hysteresis: int = 0
        self._writes_filtered_within_hold: int = 0
        self._writes_filtered_other: int = 0
        self._stats_since_ts: float = time.time()
        # Plan 45-05 RESTART-02: in-flight counter + drain event used by
        # updater.maintenance.drain_inflight_modbus. Every async_setValues
        # invocation increments on entry and decrements on exit; the event
        # is set whenever the counter hits zero so the drain helper can
        # wake up as soon as the last write completes.
        self._inflight_count: int = 0
        self._inflight_drained: asyncio.Event = asyncio.Event()
        self._inflight_drained.set()  # initially drained

    def getValues(self, fc_as_hex, address, count=1):
        """Override to intercept reads when cache is stale.

        When cache.is_stale is True, raise an exception that pymodbus
        will convert to Modbus exception code 0x04 (SLAVE_DEVICE_FAILURE).
        This tells Venus OS the device is unavailable rather than
        silently serving outdated data.
        """
        if self._cache.is_stale:
            raise Exception("Cache stale: no successful poll within timeout")
        self.last_successful_read = time.monotonic()
        self.read_count += 1
        return super().getValues(fc_as_hex, address, count)

    async def async_setValues(self, fc_as_hex, address, values):
        """Intercept writes to Model 123 registers for power control.

        pymodbus passes the protocol address directly (e.g. 40154 for
        register 40154). The +1 adjustment happens inside setValues(),
        not in the address passed to async_setValues.

        For Model 123 writes, validates and forwards to the inverter plugin.
        Other writes fall through to normal datablock storage.

        Plan 45-05 RESTART-01/02: during ``app_ctx.maintenance_mode``,
        Model 123 writes are either rejected with ``ExcCodes.DEVICE_BUSY``
        (slavebusy strategy) or silently dropped (silent_drop strategy).
        Reads are unaffected — Venus OS keeps displaying cached values.
        The in-flight counter is incremented on entry and decremented on
        exit so ``drain_inflight_modbus`` can wait for quiescence.
        """
        self._inflight_count += 1
        self._inflight_drained.clear()
        try:
            # Address from pymodbus is the SunSpec address directly
            abs_addr = address

            # Flag Venus OS detection on any Model 123 write (one-shot)
            if (
                self._app_ctx is not None
                and self._control is not None
                and self._control.is_model_123_address(abs_addr, len(values))
                and not self._app_ctx.venus_os_detected
            ):
                self._app_ctx.venus_os_detected = True
                self._app_ctx.venus_os_detected_ts = time.time()
                # Promote tracked client IP as Venus OS IP
                candidate_ip = self._app_ctx._last_modbus_client_ip
                if candidate_ip:
                    self._app_ctx.venus_os_client_ip = candidate_ip
                    logger.info("Venus OS detected: first Modbus write to Model 123 from %s", candidate_ip)
                else:
                    logger.info("Venus OS detected: first Modbus write to Model 123")

            # Plan 45-05 RESTART-01: maintenance-mode gate. Only gate
            # Model 123 writes — other writes (if any) go straight through
            # the datablock with no side effect on the inverter.
            if (
                self._app_ctx is not None
                and getattr(self._app_ctx, "maintenance_mode", False)
                and self._control is not None
                and self._control.is_model_123_address(abs_addr, len(values))
            ):
                if MAINTENANCE_STRATEGY == "slavebusy":
                    control_log.info(
                        "maintenance_mode_write_rejected",
                        addr=abs_addr, strategy="slavebusy",
                    )
                    return ExcCodes.DEVICE_BUSY
                # silent_drop
                control_log.info(
                    "maintenance_mode_write_dropped",
                    addr=abs_addr, strategy="silent_drop",
                )
                return None

            if (
                self._control is not None
                and self._control.is_model_123_address(abs_addr, len(values))
            ):
                # Always update local ControlState first (readback for Venus OS)
                self._handle_local_control_write(abs_addr, values)
                self._writes_total += 1

                # Distribute to all devices via PowerLimitDistributor
                if self._distributor is not None:
                    self._schedule_flush()
                return

            # Default: store in datablock via normal setValues
            self.setValues(fc_as_hex, address, values)
        finally:
            self._inflight_count -= 1
            if self._inflight_count <= 0:
                self._inflight_count = 0
                self._inflight_drained.set()

    def _schedule_flush(self) -> None:
        """Request a distribution of the CURRENT control state.

        At most ONE flush runner exists. A request while it runs only sets
        the dirty flag: the runner then makes exactly one more pass with the
        newest value (one running + one waiting, never a queue of tasks). The
        Venus write returns immediately; sequential writes of a tick collapse
        into one distribution. The runner counts as in-flight for the
        maintenance drain until it has finished, however the writers end.
        """
        self._flush_dirty = True
        task = self._pending_flush
        if task is not None and not task.done():
            return
        self._inflight_enter()
        task = asyncio.ensure_future(self._flush_runner())
        task.add_done_callback(lambda _t: self._inflight_exit())
        self._pending_flush = task
        self._flush_tasks.add(task)
        task.add_done_callback(self._flush_tasks.discard)

    async def wait_distributions(self) -> None:
        """Wait until every scheduled distribution has finished (tests, shutdown)."""
        while self._flush_tasks:
            await asyncio.gather(*list(self._flush_tasks), return_exceptions=True)

    def _inflight_enter(self) -> None:
        self._inflight_count += 1
        self._inflight_drained.clear()

    def _inflight_exit(self) -> None:
        self._inflight_count -= 1
        if self._inflight_count <= 0:
            self._inflight_count = 0
            self._inflight_drained.set()

    async def _flush_runner(self) -> None:
        """Run distributions while requests are pending; retry failures.

        A failed distribution is NOT dropped (the Venus write was already
        acknowledged): the runner retries with backoff (``FLUSH_MAX_RETRIES``),
        always with the NEWEST control state. After a failure the booked state
        is unknown, so ``_last_applied_pct`` is reset and the retry bypasses
        the release-up hysteresis - a higher old release is never kept just
        because the new value looks like noise. The failure is visible in
        ``flush_error`` / ``flush_failures_total`` (stats, /api/health).
        """
        failures = 0
        first = True
        while self._flush_dirty:
            if first and DISTRIBUTE_COALESCE_S > 0:
                await asyncio.sleep(DISTRIBUTE_COALESCE_S)
            first = False
            # From here a new write only marks the runner dirty again.
            self._flush_dirty = False
            try:
                await self._flush_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("venus_distribute_failed")
                failures += 1
                self.flush_failures_total += 1
                self._set_flush_error(True)
                self._last_applied_pct = None  # unknown state: force next pass
                if failures > FLUSH_MAX_RETRIES:
                    logger.error(
                        "venus_distribute_gave_up after %d attempts; next "
                        "Venus write retries", failures,
                    )
                    if not self._flush_dirty:
                        return
                    # A newer value arrived during the last attempt: it is a
                    # fresh request, so start over with it instead of leaving
                    # a higher release standing with no runner (bounded: only
                    # new writes re-arm the flag).
                    failures = 0
                    await asyncio.sleep(FLUSH_RETRY_BACKOFF_S)
                    continue
                self._flush_dirty = True
                await asyncio.sleep(FLUSH_RETRY_BACKOFF_S * (2 ** (failures - 1)))
            else:
                failures = 0
                self._set_flush_error(False)

    def _set_flush_error(self, value: bool) -> None:
        self.flush_error = value
        if self._app_ctx is not None:
            try:
                self._app_ctx.distribution_error = value
            except Exception:  # pragma: no cover - foreign ctx objects
                pass

    async def _flush_once(self) -> None:
        # Serialise: a second pass must not distribute while the first one is
        # still on the wire.
        async with self._dist_lock:
            if self._distributor is None or not self._should_apply_to_distributor():
                return
            # Book exactly what is transferred. A write arriving during
            # distribute() is NOT part of it and must be seen by the next
            # pass as a real difference.
            pct = self._control.wmaxlimpct_float
            enabled = self._control.is_enabled
            self.distributions_total += 1
            await self._distributor.distribute(pct, enabled)
            # With WMaxLim_Ena=0 the distributor releases 100 %: book THAT,
            # not the stored percentage, or re-enabling with the same value
            # would be filtered as "no change" and never throttle again.
            self._last_applied_pct = pct if enabled else 100.0
            self._last_applied_ts = time.monotonic()
            self._writes_applied += 1

    def _should_apply_to_distributor(self) -> bool:
        """Asymmetric hysteresis on the Venus OS write path.

        Throttle-DOWN (new < last applied) always passes through — drops
        in budget must reach the SE30K instantly. RELEASE-UP is gated:

          1. First write since startup: always pass through (last=None).
          2. ``new_pct >= near_full_pct``: always pass through (disable /
             100 % escape hatch must never be filtered).
          3. ``|Δ| < hysteresis_pct``: filter out (noise).
          4. ``now - last_applied_ts < min_hold_after_down_s``: filter out
             (let the SE30K finish settling — measured response ≈15 s).

        Filter decisions are recorded in ``app_ctx.override_log`` so the
        webapp dashboard surfaces them and we can verify dampening in
        the wild. With ``hysteresis_pct == 0`` the filter is fully
        bypassed (pre-patch behaviour).
        """
        cfg = self._tuning_params()
        hyst_pct = cfg["hysteresis_pct"]
        new_pct = self._control.wmaxlimpct_float

        # Disabled by config — always pass through.
        if hyst_pct <= 0:
            return True

        # Disable-path (ena=0) is handled by distribute(enable=False)
        # which force-sends 100 % to every device. Never filter that.
        if not self._control.is_enabled:
            return True

        # First write since boot.
        if self._last_applied_pct is None:
            return True

        delta = new_pct - self._last_applied_pct

        # Throttle-DOWN: always pass.
        if delta < 0:
            return True

        # NEAR-FULL release: always pass.
        if new_pct >= cfg["near_full_pct"]:
            return True

        # RELEASE-UP filter.
        if delta < hyst_pct:
            self._record_filter_decision(new_pct, delta, "below_hysteresis")
            return False

        age = time.monotonic() - self._last_applied_ts
        if age < cfg["min_hold_after_down_s"]:
            self._record_filter_decision(new_pct, delta, "within_hold_window")
            return False

        return True

    def _tuning_params(self) -> dict:
        """Resolve hysteresis parameters from config, falling back to defaults."""
        cfg = getattr(getattr(self._app_ctx, "config", None), "control", None)
        if cfg is None:
            return {
                "hysteresis_pct": HYSTERESIS_PCT,
                "min_hold_after_down_s": MIN_HOLD_AFTER_DOWN_S,
                "near_full_pct": NEAR_FULL_PCT,
            }
        return {
            "hysteresis_pct": cfg.hysteresis_pct,
            "min_hold_after_down_s": cfg.min_hold_after_down_s,
            "near_full_pct": cfg.near_full_pct,
        }

    def _record_filter_decision(self, new_pct: float, delta: float, reason: str) -> None:
        """Log a filtered Venus OS write to the structured log + override_log."""
        if reason == "below_hysteresis":
            self._writes_filtered_below_hysteresis += 1
        elif reason == "within_hold_window":
            self._writes_filtered_within_hold += 1
        else:
            self._writes_filtered_other += 1
        control_log.info(
            "venus_write_filtered",
            new_pct=round(new_pct, 2),
            last_pct=round(self._last_applied_pct or 0.0, 2),
            delta=round(delta, 2),
            reason=reason,
        )
        if self._app_ctx is not None and self._app_ctx.override_log is not None:
            self._app_ctx.override_log.append(
                "venus_os", "filtered", new_pct, detail=reason,
            )

    def get_filter_stats(self) -> dict:
        """Return current write/filter counter snapshot for the dashboard API."""
        return {
            "writes_total": self._writes_total,
            "writes_applied": self._writes_applied,
            "filtered_below_hysteresis": self._writes_filtered_below_hysteresis,
            "filtered_within_hold": self._writes_filtered_within_hold,
            "filtered_other": self._writes_filtered_other,
            "since_ts": self._stats_since_ts,
            "flush_error": self.flush_error,
            "flush_failures_total": self.flush_failures_total,
        }

    def _handle_local_control_write(self, abs_addr: int, values: list[int]) -> None:
        """Accept Model 123 write locally without forwarding to a plugin.

        Updates ControlState and readback registers. Used when no single
        plugin is available for power limit forwarding (multi-device mode,
        Phase 23 will add PowerLimitDistributor).
        """
        offset = abs_addr - MODEL_123_START

        if offset == WMAXLIMPCT_OFFSET and len(values) >= 1:
            error = validate_wmaxlimpct(values[0])
            if error:
                control_log.info(
                    "power_limit_write",
                    wmaxlimpct=values[0], result="rejected", reason=error,
                )
                raise Exception(f"ILLEGAL_VALUE: {error}")

            floor = max(self._control.clamp_min_pct, 1)
            ceiling = self._control.clamp_max_pct
            clamped = max(floor, min(ceiling, values[0]))
            self._control.update_wmaxlimpct(clamped)
            self._control.update_wmaxlim_ena(1)
            self._control.set_from_venus_os()
            self._control.save_last_limit()
            if self._app_ctx and self._app_ctx.override_log is not None:
                self._app_ctx.override_log.append(
                    "venus_os", "set", self._control.wmaxlimpct_float,
                )
            self._update_model_123_readback()
            return

        if offset == WMAXLIM_ENA_OFFSET and len(values) >= 1:
            ena_value = values[0]
            if ena_value not in (0, 1):
                raise Exception(
                    f"ILLEGAL_VALUE: WMaxLim_Ena must be 0 or 1, got {ena_value}"
                )
            self._control.update_wmaxlim_ena(ena_value)
            self._control.set_from_venus_os()
            if self._app_ctx and self._app_ctx.override_log is not None:
                ena_action = "enable" if self._control.is_enabled else "disable"
                self._app_ctx.override_log.append(
                    "venus_os", ena_action, self._control.wmaxlimpct_float,
                )
            self._update_model_123_readback()
            return

        # Other Model 123 registers: store locally
        self.store["h"].setValues(abs_addr + 1, values)

    def _update_model_123_readback(self) -> None:
        """Write current ControlState as Model 123 readback to the datablock."""
        readback = self._control.get_model_123_readback()
        # Model 123 DID is at 40149, datablock address = 40149 + 1 offset = 40150
        self.store["h"].setValues(40150, readback)


async def _start_server(server: ModbusTcpServer) -> None:
    """Start the Modbus TCP server with fallback for API differences.

    Tries server.serve_forever() first (pymodbus 3.x standard).
    Falls back to StartAsyncTcpServer if serve_forever() is not available.
    """
    if hasattr(server, "serve_forever"):
        await server.serve_forever()
    else:
        from pymodbus.server import StartAsyncTcpServer
        logger.info("serve_forever() not found, using StartAsyncTcpServer fallback")
        await StartAsyncTcpServer(
            context=server.context,
            address=server.address,
        )


async def run_modbus_server(
    host: str = "0.0.0.0",
    port: int = 502,
    app_ctx: object | None = None,
) -> tuple:
    """Set up the Modbus TCP server infrastructure (no polling).

    Creates datablock, RegisterCache, StalenessAwareSlaveContext, and
    ModbusTcpServer. Does NOT start poll loops -- that is handled by
    DeviceRegistry. Does NOT connect any plugin -- plugins are per-device.

    Returns:
        Tuple of (cache, control_state, server, server_task, slave_ctx) so
        the caller can manage the server lifecycle, pass cache to
        AggregationLayer, and inject a PowerLimitDistributor post-hoc.
    """
    # Build initial register datablock with static SunSpec values
    initial_values = build_initial_registers()
    datablock = ModbusSequentialDataBlock(DATABLOCK_START, initial_values)

    # Create register cache with staleness tracking
    cache = RegisterCache(datablock, staleness_timeout=STALENESS_TIMEOUT)

    # Create control state for Model 123 write path
    control_state = ControlState()

    # Create staleness-aware Modbus server context with unit ID 126
    # plugin=None -- power limit forwarding deferred to Phase 23
    slave_ctx = StalenessAwareSlaveContext(
        cache=cache, plugin=None, control_state=control_state,
        app_ctx=app_ctx, hr=datablock,
    )
    server_ctx = ModbusServerContext(
        devices={PROXY_UNIT_ID: slave_ctx},
        single=False,
    )

    # Create the Modbus TCP server
    server = ModbusTcpServer(
        context=server_ctx,
        address=(host, port),
    )

    # Patch server to track most recent Modbus TCP client IP
    _orig_callback_new_connection = server.callback_new_connection

    def _patched_callback_new_connection():
        handler = _orig_callback_new_connection()
        _orig_connection_made = handler.connection_made

        def _capture_ip_connection_made(transport):
            if app_ctx is not None:
                try:
                    peername = transport.get_extra_info("peername")
                    if peername:
                        # Store as candidate -- only promoted to venus_os_client_ip
                        # when Model 123 write is detected in async_setValues
                        app_ctx._last_modbus_client_ip = peername[0]
                except Exception:
                    logger.debug("Failed to capture Modbus client IP", exc_info=True)
            return _orig_connection_made(transport)

        handler.connection_made = _capture_ip_connection_made
        return handler

    server.callback_new_connection = _patched_callback_new_connection
    logger.info(
        "Starting Modbus TCP server on %s:%d (unit ID %d)",
        host, port, PROXY_UNIT_ID,
    )

    # Populate app context
    if app_ctx is not None:
        app_ctx.cache = cache
        app_ctx.control_state = control_state
        # Plan 45-05 RESTART-02: expose the slave context so
        # updater.maintenance.drain_inflight_modbus can inspect its
        # in-flight counter during graceful shutdown.
        app_ctx._slave_ctx = slave_ctx

    # Start server as background task
    server_task = asyncio.create_task(_start_server(server), name="modbus-server")

    return cache, control_state, server, server_task, slave_ctx
