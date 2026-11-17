from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal

import structlog

from pv_inverter_master.modbus_gateway import GatewayUnresolved, peek_gateway


logger = structlog.get_logger()

ThrottleMode = Literal["proportional", "binary", "none"]

#: Upper bound for a writer waiting on the per-device poll lock
#: (defense-in-depth). Worst case the lock is
#: held by a full poll cycle (~8s on the slowest two-read SE30K path) plus
#: a connect attempt — 12s covers that with margin. If the lock is leaked
#: by ANY future bug path, writers degrade to a logged failed WriteResult
#: (the Venus control loop re-ticks every 5s) instead of hanging forever.
WRITE_LOCK_TIMEOUT_S = 12.0

#: ``WriteResult.error`` of a write that was dropped because a newer write for
#: the same device queued behind it (gateway write coalescing). Not a failure of
#: the device: callers must neither book it as applied nor warn about it.
SUPERSEDED_ERROR = "superseded"


@dataclass
class PollResult:
    """Result from polling an inverter."""
    common_registers: list[int]    # 67 registers (DID + len + 65 data)
    inverter_registers: list[int]  # 52 registers (DID + len + 50 data)
    success: bool
    error: str | None = None
    #: Source-stale flag: the poll "succeeded" (no hard error) but the
    #: plugin has NO fresh data because the upstream source is unreachable /
    #: its last frame is too old (e.g. OpenDTU NRF radio gap). The device
    #: therefore reads 0W with no error — a state that downstream consumers
    #: (anomaly detector, UI) must distinguish from a genuinely-producing-0W
    #: inverter, so a transient radio gap is not mistaken for a dead inverter.
    #: Default False: any plugin that does not set it behaves exactly as
    #: before (the consumer's fail-safe path treats "not stale").
    stale: bool = False


@dataclass
class WriteResult:
    """Result from writing a power limit to the inverter."""
    success: bool
    error: str | None = None


@dataclass(frozen=True)
class ThrottleCaps:
    """Throttle capabilities declared by an inverter plugin."""
    mode: ThrottleMode
    response_time_s: float
    cooldown_s: float
    startup_delay_s: float


def compute_throttle_score(caps: ThrottleCaps) -> float:
    """Compute throttle speed score 0-10 from capabilities.

    Higher = faster regulation. Proportional > binary > none.
    """
    if caps.mode == "none":
        return 0.0
    if caps.mode == "proportional":
        base = 7.0
    else:  # binary
        base = 3.0
    response_bonus = max(0.0, 3.0 * (1.0 - caps.response_time_s / 10.0))
    cooldown_penalty = min(2.0, caps.cooldown_s / 150.0)
    startup_penalty = min(1.0, caps.startup_delay_s / 30.0)
    score = base + response_bonus - cooldown_penalty - startup_penalty
    return round(max(0.0, min(10.0, score)), 1)


def get_throttle_caps(plugin: object) -> ThrottleCaps | None:
    """Safely extract ThrottleCaps from a plugin, returning None if unavailable."""
    if hasattr(plugin, "throttle_capabilities"):
        return plugin.throttle_capabilities
    return None


async def locked_write_power_limit(
    plugin: object, enable: bool, limit_pct: float,
    *, force: bool = False, poll_lock: object = None,
) -> WriteResult:
    """The ONE write choke point: write_power_limit under the device's poll lock.

    Serializes Modbus writes against the per-device poll/drain lock (the same
    asyncio.Lock instance the poll loop and stop_device use), so stop_device's
    drain provably covers in-flight WRITES too, not just reads.
    Without this, a webapp/distributor write racing stop_device's drain window
    can land on the single-connection SunSpec stack and get FIN'd mid-write —
    the SE30K wedge mechanism.

    Plugins that own a ModbusGateway (SolarEdge, Sungrow) ignore ``poll_lock``
    here on purpose: the gateway lock is the structural guarantee (also covers
    callers that pass poll_lock=None) and its close() drains in-flight
    transactions, so stop_device's drain still holds. Queuing writers on the
    poll_lock FIRST would also defeat write coalescing. poll_lock=None for other
    plugins degrades to a plain unlocked call (they have no single-session
    hardware constraint).

    The lock acquire is BOUNDED (WRITE_LOCK_TIMEOUT_S): if the lock never
    frees (a leaked lock from any future bug path), the write fails clean
    with error="write_lock_timeout" instead of hanging the caller forever —
    callers like distribute()'s gather and the Venus control path keep
    ticking and retry on the next cycle.
    """
    try:
        return await _locked_write(plugin, enable, limit_pct, force, poll_lock)
    except GatewayUnresolved:
        # The host name never resolved: there is no gateway to write through.
        logger.warning("write_host_unresolved", enable=enable, limit_pct=limit_pct)
        return WriteResult(success=False, error="host_unresolved")


async def _locked_write(plugin, enable, limit_pct, force, poll_lock) -> WriteResult:
    if peek_gateway(plugin) is not None or poll_lock is None:
        return await plugin.write_power_limit(enable, limit_pct, force=force)
    try:
        await asyncio.wait_for(poll_lock.acquire(), timeout=WRITE_LOCK_TIMEOUT_S)
    except asyncio.TimeoutError:
        logger.warning(
            "write_lock_timeout",
            timeout_s=WRITE_LOCK_TIMEOUT_S,
            enable=enable,
            limit_pct=limit_pct,
            msg="poll lock not acquired within timeout — write skipped, "
            "caller retries on next control tick",
        )
        return WriteResult(success=False, error="write_lock_timeout")
    try:
        return await plugin.write_power_limit(enable, limit_pct, force=force)
    finally:
        poll_lock.release()


class InverterPlugin(ABC):
    """Interface for inverter brand plugins.

    Each brand plugin implements this ABC to provide:
    - Connection management to the physical inverter
    - Polling for live register data
    - Static identity overrides (manufacturer string, etc.)
    - Synthesized model data (Model 120 Nameplate)
    """

    @abstractmethod
    async def connect(self) -> None:
        """Establish connection to the inverter."""

    @abstractmethod
    async def poll(self) -> PollResult:
        """Read all registers needed for the SunSpec model chain.

        Returns PollResult with:
        - common_registers: 67 uint16 values (Model 1 DID + Length + 65 data fields)
        - inverter_registers: 52 uint16 values (Model 103 DID + Length + 50 data fields)
        - success: True if read succeeded
        - error: Error message if success is False
        """

    def get_register_read_policy(self) -> list[dict]:
        """Describe which register blocks this plugin reads periodically.

        Surfaced to the UI so the register view can state what is ACTUALLY
        polled and how often, instead of the frontend hardcoding an assumption
        that silently drifts from the code. Intentionally concrete (`NOT`
        abstract) with an empty default: plugins that synthesise their blocks
        (shelly, senec, sungrow) or fetch over HTTP have no meaningful Modbus
        block cadence, so the UI simply omits the section for them.

        Each entry: ``{"block": str, "detail": str, "cadence": str}``.
        """
        return []

    @abstractmethod
    def get_static_common_overrides(self) -> dict[int, int]:
        """Return register offset -> value for static Common Model fields.

        Offsets are relative to Common Model DID register (40002).
        E.g., {0: 1, 1: 65, 2: 0x4672, ...} for DID, Length, Manufacturer.
        """

    @abstractmethod
    def get_model_120_registers(self) -> list[int]:
        """Return 28 uint16 values for synthesized Model 120 (Nameplate).

        Includes DID (120) and Length (26) as first two values,
        followed by 26 data registers.
        """

    @abstractmethod
    async def write_power_limit(self, enable: bool, limit_pct: float, *, force: bool = False) -> WriteResult:
        """Write power limit to the inverter.

        Args:
            enable: True to enable dynamic power control, False to disable
            limit_pct: Power limit as float percentage (0.0-100.0)

        Returns:
            WriteResult with success/error status
        """

    @abstractmethod
    async def reconfigure(self, host: str, port: int, unit_id: int) -> None:
        """Reconfigure connection parameters for hot-reload.

        Closes existing connection and updates host/port/unit_id.
        Does NOT reconnect -- the poll loop's ConnectionManager handles that.
        """

    @abstractmethod
    async def close(self) -> None:
        """Clean up connection resources."""

    @property
    @abstractmethod
    def throttle_capabilities(self) -> ThrottleCaps:
        """Declare this device's throttle capabilities."""

    @property
    def min_poll_interval(self) -> float:
        """Minimum seconds between polls this device can tolerate.

        The device registry clamps the configured poll interval up to
        this floor. Default 0.0 means no constraint. Plugins talking to
        rate-limited hardware (e.g. Hoymiles HMS built-in WiFi DTUs)
        override this to keep the device from being overwhelmed.
        """
        return 0.0
