"""Single-session Modbus gateway: ONE TCP connection, ONE transaction at a time per host.

Why this exists
---------------
The SolarEdge SE30K SunSpec server tolerates exactly one Modbus-TCP session
and one transaction at a time. A second client, two transactions on the same
session, or a FIN in the middle of a transaction wedge it (port open but mute
or refused; only an AC power cycle or ~5-7 min of true client silence helps;
incidents 2026-06-10/11 and 2026-07-16).

Before this module the guarantee rested on caller discipline: every caller of
a plugin had to hold the registry's ``poll_lock``, the SE30K commit write ran
as a fire-and-forget task, connect + DPC init ran unlocked, and the web UI /
scanner opened their own clients. Here the guarantee is structural instead:

* One :class:`ModbusGateway` per inverter HOST (module registry, see
  :func:`get_gateway`) owns the only ``AsyncModbusTcpClient`` for that host.
* Every Modbus access (poll, write, commit, verify, connect + DPC init, the
  config "test connection" button, the Sungrow probe, the network scanner)
  goes through :meth:`ModbusGateway.transaction` / :meth:`probe_transaction`,
  which hold one ``asyncio.Lock``. Plugins and handlers never see the client.
* A minimum gap between two wire transactions (``DEFAULT_MIN_GAP_S``) keeps the
  device from being hammered even when callers queue up.
* Writers that supersede each other ("only the newest limit counts") are
  coalesced with :meth:`run_latest`: a waiting write is dropped when a newer
  one has queued behind it.
* While the device is in QUIESCE (ConnectionManager) the gateway refuses ALL
  traffic, including web-UI tests and reconnects, until the window ends.
* Reconnects after a failed connect are throttled (``CONNECT_MIN_INTERVAL_S``)
  so nothing can loop-hammer a wedged server.
"""
from __future__ import annotations

import asyncio
import inspect
import ipaddress
import socket
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Awaitable, Callable

import structlog
from pymodbus.client import AsyncModbusTcpClient

logger = structlog.get_logger()

#: Minimum pause between the END of one wire transaction and the START of the
#: next on the same host. The SE30K answers a 52-register read in well under
#: 100 ms on the LAN, so 150 ms never delays the 1 Hz poll (one transaction per
#: second) but it does space the burst sequences (DPC init: ~14 transactions,
#: verify, queued writes) so the single-threaded SunSpec stack always has
#: breathing room. Range from the review: 100-250 ms; 150 ms is the lower-middle
#: value because connect + DPC init at 250 ms would take ~3.5 s, at 150 ms ~2 s.
DEFAULT_MIN_GAP_S = 0.15

#: After a FAILED connect attempt the next one is refused for this long. The
#: poll loop's own backoff (5-60 s) is far above it; this is the structural
#: floor for every other caller (web UI test button, scanner, future code).
CONNECT_MIN_INTERVAL_S = 2.0

#: Default bound for waiting on the gateway lock (matches the former
#: WRITE_LOCK_TIMEOUT_S: one full poll cycle + a connect attempt, with margin).
DEFAULT_WAIT_TIMEOUT_S = 12.0

#: HARD upper limit ``close()`` waits for the lock holder (poll, DPC init,
#: verify block, queued writes - a multi-step block can take far longer than the
#: 10 s stop drain) before it drops the session anyway. Only a lock holder that
#: is stuck far beyond every pymodbus timeout reaches this; it is logged as an
#: error because the FIN then lands mid-transaction. Shutdown must not hang.
CLOSE_DRAIN_TIMEOUT_S = 60.0

#: A cancelled caller (web request gone, shutdown) must not tear down a request
#: that is already on the wire: ``_call`` keeps waiting for the device up to this
#: long before the host is put to rest. Above every pymodbus timeout used here.
CANCEL_DRAIN_S = 15.0

#: Rest period applied to a host when a request was still unanswered after the
#: cancel drain (the session is then closed mid-request and the SE30K needs quiet).
CANCEL_QUIESCE_S = 300.0

#: Web-probe circuit breaker: after this many consecutive SILENT failures
#: (timeout / no answer on a probe session) further probe sessions are refused
#: for PROBE_BREAKER_BASE_S, doubling per repeat up to PROBE_BREAKER_MAX_S. A
#: mute Modbus server (TCP up, no answers) is the SE30K wedge signature; repeated
#: web probes must not keep opening sessions against it.
PROBE_BREAKER_AFTER = 3
PROBE_BREAKER_BASE_S = 60.0
PROBE_BREAKER_MAX_S = 600.0

#: Returned by :meth:`ModbusGateway.run_latest` when a newer call replaced it.
SUPERSEDED = object()


class GatewayError(Exception):
    """Base class for gateway refusals (not Modbus protocol errors)."""


class GatewayQuiesced(GatewayError):
    """The host is in a QUIESCE window; no traffic allowed until it ends."""


class GatewayWaitTimeout(GatewayError):
    """The gateway lock was not acquired within the wait timeout."""


class ModbusNotConnected(GatewayError):
    """No live session; connect first (inside a transaction)."""


class ModbusConnectError(GatewayError):
    """Connecting failed or was refused by the throttle."""


class GatewayPortConflict(GatewayError):
    """A different port of this host already has the live session."""


class GatewayHostClaimed(GatewayError):
    """A configured device owns this host; the caller must not touch it."""


class GatewayUnresolved(GatewayError):
    """Host name cannot be resolved: no gateway is created for it.

    A gateway keyed by an unresolved NAME could later share its target with a
    second gateway keyed by the IP - two sessions to a single-session inverter.
    """


class GatewayProbeBreaker(GatewayError):
    """Probe sessions are refused after repeated silent failures (circuit breaker)."""


def unit_kwarg(unit_id: int) -> dict[str, int]:
    """pymodbus 3.x renamed ``slave`` to ``device_id`` (3.10); pass the right one.

    The ONLY place that knows the argument name: every Modbus call in the
    project (gateway, scripts, web handlers) must build its unit argument here.
    """
    return {_UNIT_ARG: unit_id}


_unit_kwarg = unit_kwarg  # backwards-compatible alias


def _detect_unit_arg() -> str:
    try:
        params = inspect.signature(AsyncModbusTcpClient.read_holding_registers).parameters
    except (TypeError, ValueError):  # pragma: no cover - exotic pymodbus builds
        return "device_id"
    return "device_id" if "device_id" in params else "slave"


_UNIT_ARG = _detect_unit_arg()


class ModbusTransaction:
    """Handle valid only inside ``async with gateway.transaction()``.

    All wire access of one lock holder. The handle goes dead on exit, so a
    leaked reference cannot talk to the device without the lock.
    """

    def __init__(self, gateway: "ModbusGateway", client_override: Any = None):
        self._gw = gateway
        self._active = True
        self._client_override = client_override

    # -- state ----------------------------------------------------------
    @property
    def connected(self) -> bool:
        client = self._client()
        return client is not None and bool(getattr(client, "connected", False))

    def _client(self) -> Any:
        return self._client_override if self._client_override is not None else self._gw._client

    async def _call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        if not self._active:
            raise RuntimeError("ModbusTransaction used after its lock was released")
        client = self._client()
        if client is None or not getattr(client, "connected", False):
            raise ModbusNotConnected(f"{self._gw.host}: not connected")
        wire = asyncio.ensure_future(self._gw._wire(getattr(client, name), *args, **kwargs))
        try:
            resp = await asyncio.shield(wire)
        except asyncio.CancelledError:
            await self._drain_cancelled(wire)
            raise
        except GatewayError:
            raise
        except Exception:
            self._note(False)
            raise
        # pymodbus reports a timeout as a ModbusIOException RESPONSE; a device
        # that answers with a Modbus exception code is alive (not silent).
        self._note(type(resp).__name__ != "ModbusIOException")
        return resp

    async def _drain_cancelled(self, wire: "asyncio.Future[Any]") -> None:
        """The caller was cancelled while a request is on the wire.

        Closing the session now would FIN in the middle of the request (SE30K
        wedge). Wait for the device's answer (bounded); count it like any other
        result. If it never comes, put the host to rest before anything else
        may touch it.
        """
        try:
            await asyncio.wait({wire}, timeout=CANCEL_DRAIN_S)
        except asyncio.CancelledError:
            # cancelled again while draining: the request cannot be awaited any more
            self._gw.enter_quiesce(CANCEL_QUIESCE_S)
            self._note(False)
            raise
        if not wire.done():
            logger.error("gateway_cancel_drain_timeout", host=self._gw.host)
            self._gw.enter_quiesce(CANCEL_QUIESCE_S)
            self._note(False)
            wire.add_done_callback(lambda f: f.cancelled() or f.exception())
            return
        if wire.cancelled() or wire.exception() is not None:
            self._note(False)
            return
        self._note(type(wire.result()).__name__ != "ModbusIOException")

    def _note(self, answered: bool) -> None:
        if self._client_override is not None:  # temporary probe session only
            self._gw._note_probe_result(answered)

    # -- connection (under the lock) -------------------------------------
    async def connect(self, *, timeout: float | None = None, retries: int | None = None) -> None:
        """Open the single session (no-op when already connected)."""
        if not self._active:
            raise RuntimeError("ModbusTransaction used after its lock was released")
        await self._gw._connect_locked(timeout=timeout, retries=retries)

    # -- wire primitives ---------------------------------------------------
    async def read_holding(self, address: int, count: int, unit_id: int) -> Any:
        return await self._call(
            "read_holding_registers", address, count=count, **_unit_kwarg(unit_id)
        )

    async def read_input(self, address: int, count: int, unit_id: int) -> Any:
        return await self._call(
            "read_input_registers", address, count=count, **_unit_kwarg(unit_id)
        )

    async def write_registers(self, address: int, values: list[int], unit_id: int) -> Any:
        return await self._call(
            "write_registers", address, values, **_unit_kwarg(unit_id)
        )

    async def write_register(self, address: int, value: int, unit_id: int) -> Any:
        return await self._call(
            "write_register", address, value, **_unit_kwarg(unit_id)
        )


class ModbusGateway:
    """The one-and-only Modbus path to a single inverter host."""

    def __init__(
        self,
        host: str,
        port: int = 502,
        *,
        min_gap_s: float | None = None,
        client_factory: Callable[..., Any] | None = None,
    ):
        self.host = host
        self.port = port
        self.min_gap_s = DEFAULT_MIN_GAP_S if min_gap_s is None else min_gap_s
        # None = look up AsyncModbusTcpClient at call time (patchable in tests).
        self._client_factory = client_factory
        self._client: Any = None
        self._client_port: int | None = None
        self._timeout = 2.0
        self._retries = 1
        self._lock = asyncio.Lock()
        self._last_end = 0.0  # monotonic end of the last wire transaction
        self._in_flight = 0
        self._quiesce_until = 0.0
        self._last_connect_failed_at: float | None = None
        self._latest: dict[str, int] = {}
        self._probe_silent = 0          # consecutive silent probe failures
        self._probe_breaker_until = 0.0
        self._probe_breaker_trips = 0
        self._owners: set[int] = set()
        self._waiters = 0               # callers queued for / racing for the lock
        self._close_task: asyncio.Task | None = None
        # Diagnostics (and the invariant the tests assert on).
        self.max_in_flight = 0
        self.transactions = 0
        self.connects = 0

    # -- ownership (is a configured device using this host?) ---------------
    def claim(self, owner: object) -> None:
        self._owners.add(id(owner))

    def release(self, owner: object) -> None:
        self._owners.discard(id(owner))

    @property
    def claimed(self) -> bool:
        """True while a configured device plugin owns this host."""
        return bool(self._owners)

    # -- state ---------------------------------------------------------------
    @property
    def connected(self) -> bool:
        return self._client is not None and bool(getattr(self._client, "connected", False))

    @property
    def client(self) -> Any:
        """The live client or None. Read-only view for the legacy ``plugin._client``."""
        return self._client

    def adopt_client(self, client: Any) -> None:
        """Install an already-built client (legacy/test construction)."""
        self._client = client
        self._client_port = self.port if client is not None else None

    @property
    def quiesced(self) -> bool:
        return time.monotonic() < self._quiesce_until

    def enter_quiesce(self, duration_s: float) -> None:
        """Refuse all traffic for ``duration_s`` (ConnectionManager QUIESCE)."""
        self._quiesce_until = max(self._quiesce_until, time.monotonic() + duration_s)
        logger.warning("gateway_quiesce", host=self.host, duration_s=duration_s)

    def clear_quiesce(self) -> None:
        self._quiesce_until = 0.0

    # -- the lock ---------------------------------------------------------------
    @asynccontextmanager
    async def transaction(
        self, *, wait_timeout: float | None = DEFAULT_WAIT_TIMEOUT_S,
        allow_quiesced: bool = False,
    ) -> AsyncIterator[ModbusTransaction]:
        """Take the host's one lock; yield a handle for wire access."""
        await self._acquire(wait_timeout)
        txn = ModbusTransaction(self)
        try:
            if self.quiesced and not allow_quiesced:
                raise GatewayQuiesced(f"{self.host}: quiesce window active")
            yield txn
        finally:
            txn._active = False
            self._lock.release()

    @asynccontextmanager
    async def probe_transaction(
        self, port: int, *, timeout: float = 5.0, retries: int = 0,
        wait_timeout: float | None = DEFAULT_WAIT_TIMEOUT_S,
        refuse_claimed: bool = False,
    ) -> AsyncIterator[ModbusTransaction]:
        """Lock-guarded access for web-UI tests, probes and the scanner.

        Never creates a second connection: a live session on the same port is
        reused as is; otherwise a temporary client is opened (and closed on exit)
        while holding the lock. A live session on ANOTHER port is refused.

        ``refuse_claimed=True`` (scanner, Venus write): refuse when a configured
        device owns the host. The check runs AFTER the lock is taken, so a device
        started in the meantime is honoured (its connect queues on this lock).
        Temporary sessions are subject to the probe circuit breaker.
        """
        await self._acquire(wait_timeout)
        temp: Any = None
        txn = ModbusTransaction(self)
        try:
            if self.quiesced:
                raise GatewayQuiesced(f"{self.host}: quiesce window active")
            if refuse_claimed and self.claimed:
                raise GatewayHostClaimed(f"{self.host}: owned by a configured device")
            if self.connected:
                if self._client_port not in (None, port):
                    raise GatewayPortConflict(
                        f"{self.host}: live session is on port {self._client_port}"
                    )
            else:
                self._check_probe_breaker()
                self._check_connect_throttle()
                await self._respect_gap()
                temp = self._new_client(
                    self.host, port=port, timeout=timeout, retries=retries,
                    reconnect_delay=0,
                )
                self.connects += 1
                await temp.connect()
                if not getattr(temp, "connected", False):
                    self._last_connect_failed_at = time.monotonic()
                    raise ModbusConnectError(f"{self.host}:{port}: connection refused")
                txn._client_override = temp
            yield txn
        finally:
            txn._active = False
            if temp is not None:
                try:
                    temp.close()
                except Exception:  # pragma: no cover - best effort
                    logger.debug("gateway_probe_close_failed", host=self.host, exc_info=True)
                self._last_end = time.monotonic()
            self._lock.release()

    async def tcp_probe(
        self, port: int, *, timeout: float = 0.5,
        wait_timeout: float | None = DEFAULT_WAIT_TIMEOUT_S,
        refuse_claimed: bool = True,
    ) -> bool:
        """Raw TCP port check (scanner phase 1) - under the host lock, like any session.

        Refuses (raises) for a claimed host, a QUIESCE window, a live session
        and an open probe breaker; connect and close both happen while the lock
        is held, so the probe is never concurrent with a device session or with
        another probe of the same host.
        """
        await self._acquire(wait_timeout)
        try:
            if self.quiesced:
                raise GatewayQuiesced(f"{self.host}: quiesce window active")
            if refuse_claimed and self.claimed:
                raise GatewayHostClaimed(f"{self.host}: owned by a configured device")
            if self.connected:
                raise GatewayPortConflict(f"{self.host}: a live session exists")
            self._check_probe_breaker()
            await self._respect_gap()
            try:
                _reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, port), timeout=timeout,
                )
            except (OSError, asyncio.TimeoutError):
                return False
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # pragma: no cover - best effort
                pass
            return True
        finally:
            self._last_end = time.monotonic()
            self._lock.release()

    async def run_latest(
        self, key: str, op: Callable[[ModbusTransaction], Awaitable[Any]],
        *, wait_timeout: float | None = DEFAULT_WAIT_TIMEOUT_S,
    ) -> Any:
        """Run ``op`` in a transaction unless a newer call with the same key queued.

        Write coalescing: ticket taken BEFORE waiting for the lock; when the lock
        arrives and a newer ticket exists, this call returns :data:`SUPERSEDED`
        without touching the wire (the newer call carries the newer value).
        """
        ticket = self._latest.get(key, 0) + 1
        self._latest[key] = ticket
        async with self.transaction(wait_timeout=wait_timeout) as txn:
            if self._latest.get(key) != ticket:
                return SUPERSEDED
            return await op(txn)

    async def close(self, *, drain_timeout: float = CLOSE_DRAIN_TIMEOUT_S) -> None:
        """Drop the session AFTER the lock holder finished (no FIN mid-transaction).

        Waits up to ``drain_timeout`` (default 60 s: longer than any multi-step
        block). If the holder is still running after that, ``close`` returns
        (shutdown must not hang) but the session is NOT closed without the lock:
        a background task waits for the holder and closes then. Logged as an
        error because the holder is stuck far beyond every pymodbus timeout.
        """
        try:
            await self._acquire(drain_timeout)
        except GatewayWaitTimeout:
            logger.error(
                "gateway_close_drain_timeout", host=self.host, timeout_s=drain_timeout,
                msg="lock holder did not finish within the hard limit - the session "
                    "is closed as soon as it releases the lock",
            )
            if self._close_task is None or self._close_task.done():
                self._close_task = asyncio.ensure_future(self._close_when_free())
            return
        self._close_locked()

    def _close_locked(self) -> None:
        """Drop the session; the caller holds the lock and releases it here."""
        try:
            client, self._client, self._client_port = self._client, None, None
            if client is not None:
                client.close()
        finally:
            self._lock.release()

    async def _close_when_free(self) -> None:
        await self._acquire(None)
        self._close_locked()

    async def wait_closed(self) -> None:
        """Wait for a deferred close started by :meth:`close` (tests, shutdown)."""
        task = self._close_task
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    @property
    def busy(self) -> bool:
        """True while some caller holds the gateway lock OR waits for it.

        A released lock with a queued waiter is not ``locked()`` for a moment,
        yet the waiter still references this gateway object.
        """
        return self._lock.locked() or self._waiters > 0

    # -- internals -----------------------------------------------------------------
    async def _acquire(self, wait_timeout: float | None) -> None:
        self._waiters += 1
        try:
            if wait_timeout is None:
                await self._lock.acquire()
                return
            try:
                await asyncio.wait_for(self._lock.acquire(), timeout=wait_timeout)
            except asyncio.TimeoutError:
                raise GatewayWaitTimeout(
                    f"{self.host}: gateway lock not acquired within {wait_timeout}s"
                ) from None
        finally:
            self._waiters -= 1

    async def _respect_gap(self) -> None:
        """Honour the minimum gap since the last wire activity before a (re)connect."""
        gap = self.min_gap_s - (time.monotonic() - self._last_end)
        if gap > 0:
            await asyncio.sleep(gap)

    def _check_probe_breaker(self) -> None:
        if time.monotonic() < self._probe_breaker_until:
            raise GatewayProbeBreaker(
                f"{self.host}: probe sessions refused after repeated silent failures"
            )

    def _note_probe_result(self, answered: bool) -> None:
        if answered:
            self._probe_silent = 0
            self._probe_breaker_trips = 0
            return
        self._probe_silent += 1
        if self._probe_silent >= PROBE_BREAKER_AFTER:
            window = min(PROBE_BREAKER_BASE_S * (2 ** self._probe_breaker_trips),
                         PROBE_BREAKER_MAX_S)
            self._probe_breaker_until = time.monotonic() + window
            self._probe_breaker_trips += 1
            self._probe_silent = 0
            logger.warning("gateway_probe_breaker", host=self.host, window_s=window)

    def _new_client(self, *args: Any, **kwargs: Any) -> Any:
        return (self._client_factory or AsyncModbusTcpClient)(*args, **kwargs)

    def _check_connect_throttle(self) -> None:
        failed = self._last_connect_failed_at
        if failed is not None and time.monotonic() - failed < CONNECT_MIN_INTERVAL_S:
            raise ModbusConnectError(f"{self.host}: connect throttled after recent failure")

    async def _connect_locked(self, *, timeout: float | None, retries: int | None) -> None:
        if timeout is not None:
            self._timeout = timeout
        if retries is not None:
            self._retries = retries
        if self.connected and self._client_port == self.port:
            return
        if self._client is not None:
            # Dead or wrong-port client: drop it BEFORE opening the next one.
            try:
                self._client.close()
            except Exception:  # pragma: no cover
                pass
            self._client = None
            self._client_port = None
        self._check_connect_throttle()
        await self._respect_gap()
        # reconnect_delay=0: pymodbus' own transport auto-reconnect is off; the
        # poll loop's managed path (connect() -> DPC re-init) is the only way.
        client = self._new_client(
            self.host, port=self.port, timeout=self._timeout, retries=self._retries,
            reconnect_delay=0,
        )
        self.connects += 1
        await client.connect()
        if not getattr(client, "connected", False):
            self._last_connect_failed_at = time.monotonic()
            try:
                client.close()
            except Exception:  # pragma: no cover
                pass
            raise ModbusConnectError(f"{self.host}:{self.port}: connect failed")
        self._last_connect_failed_at = None
        self._client = client
        self._client_port = self.port
        self._last_end = time.monotonic()

    async def _wire(self, func: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any) -> Any:
        """The ONLY place a Modbus request leaves the process. Lock is held."""
        gap = self.min_gap_s - (time.monotonic() - self._last_end)
        if gap > 0:
            await asyncio.sleep(gap)
        self._in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self._in_flight)
        if self._in_flight > 1:  # impossible while the lock holds; loud if it ever isn't
            logger.error("gateway_parallel_transaction", host=self.host, in_flight=self._in_flight)
        try:
            self.transactions += 1
            return await func(*args, **kwargs)
        finally:
            self._in_flight -= 1
            self._last_end = time.monotonic()


# ---------------------------------------------------------------------------
# Registry: one gateway per host, shared by plugins, web handlers and scanner.
# ---------------------------------------------------------------------------
_GATEWAYS: dict[str, ModbusGateway] = {}
_RESOLVED: dict[str, tuple[float, str]] = {}
_RESOLVE_TTL_S = 300.0


def _key(host: str) -> str:
    """Registry key: the RESOLVED IPv4/IPv6 address, so aliases share one gateway.

    "198.51.100.5", " 198.051.100.005"-style spellings and a DNS name pointing at
    the same address all map to one key (one lock, one session, one QUIESCE).
    A name that cannot be resolved raises :class:`GatewayUnresolved` (no
    gateway under the name, so name and IP can never become two gateways for
    one target); an expired earlier resolution is reused as stale-on-error so
    an existing gateway keeps its identity through a DNS outage.
    """
    h = host.strip().lower()
    try:
        return str(ipaddress.ip_address(h))
    except ValueError:
        pass
    now = time.monotonic()
    hit = _RESOLVED.get(h)
    if hit is not None and now - hit[0] < _RESOLVE_TTL_S:
        return hit[1]
    try:
        infos = socket.getaddrinfo(h, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        addrs = sorted({str(ipaddress.ip_address(i[4][0].split("%")[0])) for i in infos})
    except (OSError, UnicodeError, ValueError):
        addrs = []
    if not addrs:
        if hit is not None:
            logger.warning("gateway_host_resolution_stale", host=h)
            return hit[1]
        logger.warning("gateway_host_unresolved", host=h)
        raise GatewayUnresolved(f"{h}: host name cannot be resolved")
    _RESOLVED[h] = (now, addrs[0])
    return addrs[0]


def get_gateway(host: str, port: int | None = None) -> ModbusGateway:
    """Return the host's gateway, creating it on first use.

    ``port`` updates the target port used by the NEXT connect (a host has one
    session, whatever the port; a reconfigure to another port reconnects).
    """
    key = _key(host)
    gw = _GATEWAYS.get(key)
    if gw is None:
        # Connect to the KEY (the resolved address), never to the name again:
        # key and wire target cannot drift apart (multi-address names, DNS change).
        gw = ModbusGateway(key, port if port is not None else 502)
        _GATEWAYS[key] = gw
    elif port is not None:
        gw.port = port
    return gw


def find_gateway(host: str) -> ModbusGateway | None:
    try:
        return _GATEWAYS.get(_key(host))
    except GatewayUnresolved:
        return None


def is_host_claimed(host: str) -> bool:
    """True if a configured device plugin owns a session to this host."""
    try:
        gw = _GATEWAYS.get(_key(host))
    except GatewayUnresolved:
        return False  # never had a gateway under an unresolvable name
    return gw is not None and gw.claimed


async def probe_read(
    host: str, port: int, unit_id: int, address: int, count: int,
    *, input_registers: bool = False, timeout: float = 5.0,
) -> Any:
    """One register read for web-UI test/probe buttons - always via the host's gateway.

    Never opens a second session next to a configured device: a live session
    on the same port is reused, a QUIESCE window refuses the call (raises
    :class:`GatewayQuiesced`), and calls for the same host queue on its lock.
    Uses ``get_gateway(host)`` WITHOUT a port so a probe never retargets the
    port of a configured device.
    """
    gateway = get_gateway(host)
    async with gateway.probe_transaction(port, timeout=timeout) as t:
        if input_registers:
            return await t.read_input(address, count, unit_id)
        return await t.read_holding(address, count, unit_id)


def prune_idle_gateways() -> int:
    """Drop gateways nobody owns, uses or needs to remember (e.g. after a scan)."""
    drop = [
        k for k, g in _GATEWAYS.items()
        if not g.claimed and not g.busy and not g.connected and not g.quiesced
        and g._last_connect_failed_at is None
        and g._probe_silent == 0 and g._probe_breaker_trips == 0
        and time.monotonic() >= g._probe_breaker_until
    ]
    for k in drop:
        del _GATEWAYS[k]
    return len(drop)


async def shutdown_gateways(timeout: float = 15.0) -> None:
    """Shutdown: close every gateway in order, waiting (bounded) for deferred closes.

    ``close()`` waits for the lock holder (no FIN mid-transaction); a close the
    holder delayed beyond its drain limit runs as a background task - without
    waiting for it here, process exit would end that session abruptly.
    """
    gws = list(_GATEWAYS.values())
    if not gws:
        return
    async def _all() -> None:
        await asyncio.gather(
            *(g.close(drain_timeout=timeout) for g in gws), return_exceptions=True,
        )
        await asyncio.gather(*(g.wait_closed() for g in gws), return_exceptions=True)

    try:
        await asyncio.wait_for(_all(), timeout)
    except asyncio.TimeoutError:
        logger.error("gateway_shutdown_close_timeout", timeout_s=timeout)


def reset_gateways() -> None:
    """Forget all gateways (tests, hot reload of the whole registry)."""
    _GATEWAYS.clear()
    _RESOLVED.clear()


def peek_gateway(plugin: object) -> "ModbusGateway | None":
    """The plugin's gateway, or None if it has none or its host never resolved.

    ``getattr(plugin, "gateway", None)`` only absorbs AttributeError; the
    ``gateway`` property raises :class:`GatewayUnresolved` for a host name that
    could not be resolved. Cleanup paths (remove/disable/quiesce) must still work.
    """
    try:
        gw = getattr(plugin, "gateway", None)
    except GatewayUnresolved:
        return None
    return gw if isinstance(gw, ModbusGateway) else None
