"""SolarEdge SE30K inverter plugin.

Polls the SE30K via Modbus TCP and provides register data
translated for Fronius proxy serving.
"""
from __future__ import annotations

import asyncio
import logging
import struct

from pv_inverter_master.modbus_gateway import (
    SUPERSEDED,
    GatewayError,
    GatewayQuiesced,
    GatewayWaitTimeout,
    ModbusGateway,
    ModbusTransaction,
    get_gateway,
)
from pv_inverter_master.plugin import (
    SUPERSEDED_ERROR,
    WRITE_LOCK_TIMEOUT_S,
    InverterPlugin,
    PollResult,
    ThrottleCaps,
    WriteResult,
)
from pv_inverter_master.sunspec_models import (
    encode_string,
    _int16_as_uint16,
    COMMON_DID,
    COMMON_LENGTH,
    NAMEPLATE_DID,
    NAMEPLATE_LENGTH,
    PROXY_UNIT_ID,
)

logger = logging.getLogger(__name__)

# SE30K connection defaults
DEFAULT_HOST = "192.0.2.18"
DEFAULT_PORT = 1502
DEFAULT_UNIT_ID = 1

# Register read parameters
COMMON_READ_ADDR = 40002   # Common Model DID register
COMMON_READ_COUNT = 67     # DID + Length + 65 data
INVERTER_READ_ADDR = 40069 # Inverter Model DID register
INVERTER_READ_COUNT = 52   # DID + Length + 50 data

# Enhanced Dynamic Power Control block (SolarEdge "Power Control Protocol",
# Technical Note v1.3 Nov. 2017, pp. 12-14). Writing the dynamic limit
# (0xF322) with a Command Timeout (0xF310) armed makes the inverter revert
# to the fallback limit (0xF312) by itself when our writes stop — the
# structural fix for the latched-limit incidents (5%=1500W stuck for a
# full production day while the Modbus service was wedged).
DPC_ENABLE_REG = 0xF300            # uint16: Enable Dynamic Power Control
DPC_MAX_ACTIVE_POWER_REG = 0xF304  # float32 RO: inverter rated active power [W]
DPC_ACTIVE_POWER_LIMIT_REG = 0xF30C  # float32: ceiling the dynamic % refers to [W]
DPC_COMMAND_TIMEOUT_REG = 0xF310   # uint32: watchdog window [s]
DPC_FALLBACK_PCT_REG = 0xF312      # float32: fallback active power limit [%]
DPC_DYNAMIC_LIMIT_REG = 0xF322     # float32: dynamic active power limit [% of 0xF30C]
ADV_PWR_CONTROL_EN_REG = 0xF142    # int32: grid control functionality enable
REACTIVE_PWR_CONFIG_REG = 0xF104   # int32: 4 = RRCR mode (required for dynamic)

# The Technical Note does not specify 32-bit word order for the 0xF3xx
# block and community implementations disagree, so connect() probes the
# read-only Max Active Power register and keeps whichever order decodes
# to a plausible inverter rating.
_DPC_PLAUSIBLE_W = (1_000.0, 200_000.0)

LEGACY_LIMIT_REG = 61441  # 0xF001 simple dynamic limit: NO timeout/fallback,
                          # latches until inverter restart — the incident
                          # register. Init normalizes it to 100 so a residual
                          # pre-watchdog clamp cannot survive underneath the
                          # dynamic path.

# Re-verify the watchdog is still armed every N dynamic writes (~5 min at
# the distributor's 5 s cadence). Catches an inverter-side reset of the
# 0xF3xx block that no reconnect surfaced: a verify
# miss triggers a full re-init; if that fails the plugin disarms loudly.
DPC_VERIFY_EVERY_N_WRITES = 60


def _f32_to_regs(value: float, high_word_first: bool) -> list[int]:
    raw = struct.pack(">f", value)
    hi, lo = struct.unpack(">HH", raw)
    return [hi, lo] if high_word_first else [lo, hi]


def _regs_to_f32(regs: list[int], high_word_first: bool) -> float:
    hi, lo = (regs[0], regs[1]) if high_word_first else (regs[1], regs[0])
    return struct.unpack(">f", struct.pack(">HH", hi, lo))[0]


def _u32_to_regs(value: int, high_word_first: bool) -> list[int]:
    hi, lo = (value >> 16) & 0xFFFF, value & 0xFFFF
    return [hi, lo] if high_word_first else [lo, hi]


def _regs_to_u32(regs: list[int], high_word_first: bool) -> int:
    hi, lo = (regs[0], regs[1]) if high_word_first else (regs[1], regs[0])
    return (hi << 16) | lo


class SolarEdgePlugin(InverterPlugin):
    """SolarEdge SE30K inverter plugin.

    Polls the SE30K via Modbus TCP and provides register data
    translated for Fronius proxy serving.
    """

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        unit_id: int = DEFAULT_UNIT_ID,
        dynamic_power_control: bool = True,
        dpc_command_timeout_s: int = 120,
        dpc_fallback_pct: float = 100.0,
    ):
        self.host = host
        self.port = port
        self.unit_id = unit_id
        self.dynamic_power_control = dynamic_power_control
        self.dpc_command_timeout_s = dpc_command_timeout_s
        self.dpc_fallback_pct = dpc_fallback_pct
        # Set by _init_dynamic_power_control() on every connect(). False =
        # legacy static write path (today's behavior, no watchdog).
        self._dynamic_ok = False
        self._high_word_first = True
        self._writes_since_verify = 0
        # The ONLY Modbus path is the per-host ModbusGateway (one TCP session,
        # one transaction at a time, minimum gap, coalesced writes). The plugin
        # never holds a client of its own; ``_client`` below is a read/adopt view.
        self._gateway: ModbusGateway | None = None
        # Common Model (40002/67) is STATIC identity — manufacturer, model,
        # serial. It never changes within a Modbus session, so it is read once
        # per connect() and replayed from here on every poll instead of being
        # re-read each cycle. On a single-connection inverter that halves the
        # periodic transaction count, which is the whole point: session
        # pressure is what wedges this device.
        #
        # Lifetime is intentionally tied to the SESSION, not to a timer: the
        # plugin instance is REUSED across quiesce-close/reconfigure, so a
        # time-based cache would survive a reconnect and could replay the
        # identity of a device that is no longer on the other end (wrong
        # serial after a hardware swap). connect() clears it before re-reading,
        # so a stale identity cannot outlive the session that produced it.
        self._cached_common: list[int] = []

    @property
    def gateway(self) -> ModbusGateway:
        """The host's one Modbus gateway (created on first use, shared registry)."""
        if self._gateway is None:
            self._gateway = get_gateway(self.host, self.port)
        else:
            self._gateway.port = self.port
        return self._gateway

    @property
    def _client(self):
        """Live pymodbus client or None (read view; tests/legacy may assign)."""
        return self.gateway.client

    @_client.setter
    def _client(self, client) -> None:
        self.gateway.adopt_client(client)

    async def connect(self) -> None:
        """Open the session, arm the DPC watchdog, read identity: ONE gateway transaction.

        The whole sequence (connect, ~10 DPC reads/writes, Common Model read)
        holds the gateway lock, so no poll, write, UI test or scan can interleave
        with it; ``_dpc_init_pending`` is no longer needed to fend off writers.
        """
        gw = self.gateway
        gw.claim(self)
        # Aggressive timeout/retry to keep clamp round-trips fast: 2s x 1 retry
        # bounds the worst case per write at ~4s. reconnect_delay=0 (set by the
        # gateway) disables pymodbus' transport-level auto-reconnect so
        # reconnects always go through this managed path and the watchdog
        # re-init below runs on EVERY new session.
        async with gw.transaction() as t:
            await t.connect(timeout=2, retries=1)
            logger.info("Connected to SolarEdge at %s:%d", self.host, self.port)
            # Arm the dynamic-limit watchdog. Idempotent and re-run on every
            # connect (quiesce reconnects included) because the Technical Note
            # does not guarantee 0xF3xx settings persist across inverter
            # restarts. Any failure degrades to the legacy static path - never
            # blocks the connection.
            self._dynamic_ok = False
            self._writes_since_verify = 0
            if self.dynamic_power_control:
                try:
                    self._dynamic_ok = await self._init_dynamic_power_control(t)
                except GatewayError:
                    raise
                except Exception as e:
                    logger.warning(
                        "Dynamic power control init failed (%s) - "
                        "falling back to legacy static limit writes", e,
                    )
            if self.dynamic_power_control and not self._dynamic_ok:
                logger.warning(
                    "SolarEdge %s: limit watchdog NOT armed - a limit written "
                    "before a comms loss will latch until the next write",
                    self.host,
                )

            # Read the static Common Model ONCE per session. Cleared first so a
            # failed read cannot replay the previous session's identity: an empty
            # cache degrades the dashboard's manufacturer/model/serial display
            # (consumers gate on len >= 66) which is strictly better than showing
            # a confidently wrong serial. Non-fatal by design.
            self._cached_common = []
            try:
                common = await self._read_regs(t, COMMON_READ_ADDR, COMMON_READ_COUNT)
                if common is not None:
                    self._cached_common = common
                else:
                    logger.warning(
                        "SolarEdge %s: Common Model read returned a Modbus error - "
                        "identity fields will be blank this session",
                        self.host,
                    )
            except GatewayError:
                raise
            except Exception as e:
                logger.warning(
                    "SolarEdge %s: Common Model read failed (%s) - "
                    "identity fields will be blank this session", self.host, e,
                )

    async def _read_regs(
        self, t: ModbusTransaction, address: int, count: int,
    ) -> list[int] | None:
        """Read holding registers; None on Modbus error."""
        result = await t.read_holding(address, count, self.unit_id)
        if result.isError():
            return None
        return list(result.registers)

    async def _init_dynamic_power_control(self, t: ModbusTransaction) -> bool:
        """Probe + arm the Enhanced Dynamic Power Control block (0xF300).

        Sequence (read-first, write only on mismatch, verify by read-back):
        1. Word-order probe on read-only Max Active Power (0xF304).
        2. Grid-code gate: AdvancedPwrControlEn==1 and ReactivePwrConfig==4
           must ALREADY be set — both are grid-relevant, we never write
           them autonomously (R39); if unset -> legacy path.
        3. Ensure ceiling (0xF30C)=Max Active Power, Command Timeout
           (0xF310), Fallback (0xF312); enable (0xF300)=1 written LAST.
        4. Read back; any mismatch -> legacy path.
        """
        # --- 1. word-order probe -------------------------------------------
        probe = await self._read_regs(t, DPC_MAX_ACTIVE_POWER_REG, 2)
        if probe is None:
            logger.warning("DPC probe: Max Active Power (0xF304) unreadable")
            return False
        max_power = None
        for hwf in (True, False):
            val = _regs_to_f32(probe, hwf)
            if _DPC_PLAUSIBLE_W[0] <= val <= _DPC_PLAUSIBLE_W[1]:
                self._high_word_first = hwf
                max_power = val
                break
        if max_power is None:
            logger.warning(
                "DPC probe: 0xF304 regs %s decode to no plausible rating "
                "in either word order", probe,
            )
            return False

        # --- 2. grid-code gate (read-only) ---------------------------------
        # The gate registers live in the 0xF1xx block, whose word order may
        # differ from the probed 0xF3xx order. Values 1 and 4 decode
        # unambiguously (the wrong order yields 65536/262144), so accept
        # either order instead of letting a block-order mismatch silently
        # kill the feature.
        adv = await self._read_regs(t, ADV_PWR_CONTROL_EN_REG, 2)
        react = await self._read_regs(t, REACTIVE_PWR_CONFIG_REG, 2)
        if adv is None or react is None:
            logger.warning("DPC gate: 0xF142/0xF104 unreadable")
            return False
        adv_ok = 1 in (_regs_to_u32(adv, True), _regs_to_u32(adv, False))
        react_ok = 4 in (_regs_to_u32(react, True), _regs_to_u32(react, False))
        if not adv_ok or not react_ok:
            logger.warning(
                "DPC gate: AdvancedPwrControlEn regs=%s ReactivePwrConfig "
                "regs=%s (need 1/4) — grid-relevant settings, not changing "
                "them autonomously; staying on legacy path", adv, react,
            )
            return False

        # --- 2b. clear legacy latch residue --------------------------------
        # 0xF001 has no timeout: a sub-100% value written there (by the
        # legacy path before this deploy, or by a tick that raced the init
        # window) survives until inverter restart UNDERNEATH the dynamic
        # limit. Normalize it on this verified-healthy session so the
        # watchdog guarantee holds by construction.
        legacy = await self._read_regs(t, LEGACY_LIMIT_REG, 1)
        if legacy is None or legacy[0] != 100:
            result = await t.write_registers(LEGACY_LIMIT_REG, [100], self.unit_id)
            if result.isError():
                logger.warning("DPC init: legacy limit normalize failed: %s", result)
                return False

        # --- 3. ensure settings (write only on mismatch) -------------------
        hwf = self._high_word_first
        desired: list[tuple[int, list[int], str]] = []
        ceiling = await self._read_regs(t, DPC_ACTIVE_POWER_LIMIT_REG, 2)
        if ceiling is None or abs(_regs_to_f32(ceiling, hwf) - max_power) > 0.5:
            desired.append((
                DPC_ACTIVE_POWER_LIMIT_REG, _f32_to_regs(max_power, hwf),
                "ceiling",
            ))
        timeout = await self._read_regs(t, DPC_COMMAND_TIMEOUT_REG, 2)
        if timeout is None or _regs_to_u32(timeout, hwf) != self.dpc_command_timeout_s:
            desired.append((
                DPC_COMMAND_TIMEOUT_REG,
                _u32_to_regs(self.dpc_command_timeout_s, hwf),
                "command timeout",
            ))
        fallback = await self._read_regs(t, DPC_FALLBACK_PCT_REG, 2)
        if fallback is None or abs(_regs_to_f32(fallback, hwf) - self.dpc_fallback_pct) > 0.1:
            desired.append((
                DPC_FALLBACK_PCT_REG,
                _f32_to_regs(self.dpc_fallback_pct, hwf),
                "fallback limit",
            ))
        for address, regs, label in desired:
            result = await t.write_registers(address, regs, self.unit_id)
            if result.isError():
                logger.warning("DPC init: %s write failed: %s", label, result)
                return False
        # Enable LAST so the watchdog never runs with half-written settings.
        enable = await self._read_regs(t, DPC_ENABLE_REG, 1)
        if enable is None or enable[0] != 1:
            result = await t.write_registers(DPC_ENABLE_REG, [1], self.unit_id)
            if result.isError():
                logger.warning("DPC init: enable write failed: %s", result)
                return False

        # --- 4. read-back verify -------------------------------------------
        back_enable = await self._read_regs(t, DPC_ENABLE_REG, 1)
        back_ceiling = await self._read_regs(t, DPC_ACTIVE_POWER_LIMIT_REG, 2)
        back_timeout = await self._read_regs(t, DPC_COMMAND_TIMEOUT_REG, 2)
        back_fallback = await self._read_regs(t, DPC_FALLBACK_PCT_REG, 2)
        if (
            back_enable is None or back_enable[0] != 1
            or back_ceiling is None
            or abs(_regs_to_f32(back_ceiling, hwf) - max_power) > 0.5
            or back_timeout is None
            or _regs_to_u32(back_timeout, hwf) != self.dpc_command_timeout_s
            or back_fallback is None
            or abs(_regs_to_f32(back_fallback, hwf) - self.dpc_fallback_pct) > 0.1
        ):
            logger.warning("DPC init: read-back mismatch — staying on legacy path")
            return False

        logger.info(
            "SolarEdge %s: dynamic limit watchdog armed (ceiling=%.0fW, "
            "timeout=%ds, fallback=%.0f%%, word_order=%s)",
            self.host, max_power, self.dpc_command_timeout_s,
            self.dpc_fallback_pct, "high-first" if hwf else "low-first",
        )
        return True

    async def poll(self) -> PollResult:
        if self._gateway is None or not self._gateway.connected:
            return PollResult(
                common_registers=[], inverter_registers=[],
                success=False, error="Not connected",
            )
        try:
            # Common Model is NOT re-read here — it is static identity, read
            # once per session in connect() and replayed from the cache. This
            # halves the periodic Modbus transactions on a single-connection
            # inverter (session pressure is what wedges it).
            #
            # Side effect, intentional: dropping the read also removes ~2 s of
            # timeout latency that used to be spent BEFORE the Inverter-103
            # error path could be reached on a wedged inverter, so a wedge is
            # now surfaced sooner rather than later.
            #
            # Read Inverter Model 103 (52 registers starting at 40069). This
            # one stays at full cadence: it carries AC power, which feeds the
            # distributor and the Venus-facing aggregate.
            async with self.gateway.transaction() as t:
                inverter = await t.read_holding(
                    INVERTER_READ_ADDR, INVERTER_READ_COUNT, self.unit_id,
                )

            if inverter.isError():
                return PollResult(
                    common_registers=[], inverter_registers=[],
                    success=False, error=f"Inverter read error: {inverter}",
                )

            return PollResult(
                # Replayed from the session cache (see connect()). Kept in the
                # PollResult so the contract is unchanged for every consumer —
                # dashboard collector, register viewer and the other plugins
                # that synthesise this block all keep seeing a full 67-register
                # list. Empty only if the one-shot read failed this session.
                common_registers=list(self._cached_common),
                inverter_registers=list(inverter.registers),
                success=True,
            )
        except Exception as e:
            logger.warning("Poll failed: %s", e)
            return PollResult(
                common_registers=[], inverter_registers=[],
                success=False, error=str(e),
            )

    def get_register_read_policy(self) -> list[dict]:
        """Report the ACTUAL periodic read policy (see poll() / connect()).

        Kept next to the code it describes so it cannot drift: if someone
        reinstates the per-cycle Common read, this list is right here to be
        updated. The UI renders it verbatim.
        """
        policy = [
            {
                "block": "Common Model",
                "detail": f"{COMMON_READ_ADDR}/{COMMON_READ_COUNT}",
                "cadence": "1× beim Verbinden — statische Identität, ändert sich in einer Session nicht",
            },
            {
                "block": "Inverter Model 103",
                "detail": f"{INVERTER_READ_ADDR}/{INVERTER_READ_COUNT}",
                "cadence": "jeder Poll-Zyklus — trägt die AC-Leistung für Regelung und Aggregat",
            },
        ]
        if self.dynamic_power_control:
            policy.append({
                "block": "Dynamic Power Control",
                "detail": "0xF3xx",
                "cadence": "beim Verbinden armiert, danach nur Schreibzugriffe (Watchdog)",
            })
        return policy

    def get_static_common_overrides(self) -> dict[int, int]:
        """Return offset -> value for Common Model static overrides.

        Offsets relative to Common Model DID register (40002).
        Overrides: DID, Length, Manufacturer ("Fronius"), DeviceAddress (126).
        """
        overrides: dict[int, int] = {0: COMMON_DID, 1: COMMON_LENGTH}
        fronius_regs = encode_string("Fronius", 16)
        for i, val in enumerate(fronius_regs):
            overrides[2 + i] = val  # C_Manufacturer at offset 2-17
        overrides[66] = PROXY_UNIT_ID  # C_DeviceAddress
        return overrides

    def get_model_120_registers(self) -> list[int]:
        """Return 28 uint16 values for synthesized Model 120 (Nameplate).

        SE30K does not provide Model 120; we synthesize from datasheet specs.
        """
        regs = [0] * 28  # DID + Length + 26 data
        regs[0] = NAMEPLATE_DID      # 120
        regs[1] = NAMEPLATE_LENGTH   # 26
        regs[2] = 4       # DERTyp = PV
        regs[3] = 30000   # WRtg = 30kW
        regs[4] = 0       # WRtg_SF
        regs[5] = 30000   # VARtg = 30kVA
        regs[6] = 0       # VARtg_SF
        regs[7] = 18000   # VArRtgQ1
        regs[8] = 18000   # VArRtgQ2
        regs[9] = _int16_as_uint16(-18000)   # VArRtgQ3
        regs[10] = _int16_as_uint16(-18000)  # VArRtgQ4
        regs[11] = 0      # VArRtg_SF
        regs[12] = 44     # ARtg = 44A
        regs[13] = 0      # ARtg_SF
        regs[14] = 100    # PFRtgQ1
        regs[15] = 100    # PFRtgQ2
        regs[16] = _int16_as_uint16(-100)    # PFRtgQ3
        regs[17] = _int16_as_uint16(-100)    # PFRtgQ4
        regs[18] = _int16_as_uint16(-2)      # PFRtg_SF
        # 19-27 = zeros (storage ratings N/A, padding)
        return regs

    async def write_power_limit(self, enable: bool, limit_pct: float, *, force: bool = False) -> WriteResult:
        """Write power limit to the SE30K via the gateway (one transaction block).

        Dynamic path (watchdog armed): a single write to 0xF322; the percentage
        refers to the 0xF30C ceiling, which init pinned to the rated power -
        identical semantics to the legacy 61441 percent. A failed dynamic write
        NEVER falls back to a legacy static write: a static limit written into
        flaky comms is exactly the latch this path removes.

        Legacy path (watchdog not armed): enable 61762, limit 61441, commit
        61696 - all inside the SAME gateway transaction block, in order. The
        commit used to be a fire-and-forget task OUTSIDE any lock that could
        overlap the next poll; now it is a plain write that holds the lock.

        Coalescing: if a newer write queued while this one waited for the
        gateway, this one is dropped (error=SUPERSEDED_ERROR, success=False -
        the caller must not book it as applied; the newer call carries the
        newer value). To disable: limit 100% (full power). EDPC stays enabled -
        disabling it (61762=[0,0]) drops the inverter to 0W.
        """
        gw = self.gateway
        if not gw.connected:
            return WriteResult(success=False, error="Not connected")
        try:
            outcome = await gw.run_latest(
                # Per unit id: units of one host share the gateway, but each
                # unit's newest limit must survive (a host-wide key would drop
                # the other units' queued setpoints).
                f"power_limit:{self.unit_id}",
                lambda t: self._write_locked(t, enable, limit_pct),
                wait_timeout=WRITE_LOCK_TIMEOUT_S,
            )
        except GatewayWaitTimeout:
            logger.warning(
                "write_lock_timeout", timeout_s=WRITE_LOCK_TIMEOUT_S,
                enable=enable, limit_pct=limit_pct,
                msg="gateway not acquired within timeout - write skipped, "
                "caller retries on next control tick",
            )
            return WriteResult(success=False, error="write_lock_timeout")
        except GatewayQuiesced:
            return WriteResult(success=False, error="quiesced")
        except GatewayError as e:
            return WriteResult(success=False, error=str(e))
        except Exception as e:
            return WriteResult(success=False, error=str(e))
        if outcome is SUPERSEDED:
            return WriteResult(success=False, error=SUPERSEDED_ERROR)
        return outcome

    async def _write_locked(
        self, t: ModbusTransaction, enable: bool, limit_pct: float,
    ) -> WriteResult:
        """The write itself. Runs inside the gateway lock (``t`` is the handle)."""
        if not t.connected:
            return WriteResult(success=False, error="Not connected")
        if self._dynamic_ok:
            pct = max(1.0, min(100.0, float(limit_pct))) if enable else 100.0
            try:
                # Periodic armed-check: an inverter-side reset clears the
                # volatile 0xF3xx block without any reconnect we would see.
                # Every N writes confirm Enable is still 1; on a miss,
                # re-init inline (same transaction block). If re-arming
                # fails, disarm loudly - the caller's failed write is more
                # honest than an ACK'd no-op.
                self._writes_since_verify += 1
                if self._writes_since_verify >= DPC_VERIFY_EVERY_N_WRITES:
                    self._writes_since_verify = 0
                    armed = await self._read_regs(t, DPC_ENABLE_REG, 1)
                    if armed is None or armed[0] != 1:
                        logger.warning(
                            "DPC verify: watchdog no longer armed (0xF300=%s) "
                            "- re-initializing", armed,
                        )
                        self._dynamic_ok = await self._init_dynamic_power_control(t)
                        if not self._dynamic_ok:
                            return WriteResult(
                                success=False, error="dpc disarmed, re-init failed",
                            )
                result = await t.write_registers(
                    DPC_DYNAMIC_LIMIT_REG,
                    _f32_to_regs(pct, self._high_word_first),
                    self.unit_id,
                )
            except Exception as e:
                return WriteResult(success=False, error=str(e))
            if result.isError():
                return WriteResult(success=False, error=f"Dynamic limit write failed: {result}")
            return WriteResult(success=True)
        try:
            # Step 1: Always enable EDPC (register 61762, int32 = 2 registers)
            result = await t.write_registers(61762, [0, 1], self.unit_id)
            if result.isError():
                return WriteResult(success=False, error=f"Enable write failed: {result}")

            # Step 2: Write limit percentage (register 61441, uint16)
            # If disabling, set to 100% (full power)
            pct_int = max(1, min(100, int(round(limit_pct)))) if enable else 100
            result = await t.write_registers(61441, [pct_int], self.unit_id)
            if result.isError():
                return WriteResult(success=False, error=f"Limit write failed: {result}")

            # Step 3: Commit power control (register 61696 / 0xF100), in the
            # same lock block. The commit may time out on TCP but the limit
            # write still takes effect, so its outcome is not fatal. Worst
            # case it costs (retries+1) x timeout = 4 s of lock time on this
            # legacy-only path; callers' wait is bounded (WRITE_LOCK_TIMEOUT_S).
            try:
                await t.write_registers(61696, [1], self.unit_id)
            except Exception:
                logger.debug("commit_write_failed", host=self.host, exc_info=True)
            return WriteResult(success=True)
        except Exception as e:
            return WriteResult(success=False, error=str(e))

    async def reconfigure(self, host: str, port: int, unit_id: int) -> None:
        """Reconfigure connection parameters. Closes existing connection."""
        await self.close()
        self.release()
        self.host = host
        self.port = port
        self.unit_id = unit_id
        self._gateway = None  # new host -> that host's gateway on next use
        logger.info("Reconfigured SolarEdge to %s:%d unit=%d", host, port, unit_id)

    def release(self) -> None:
        """Give up the host claim (device removed); the session stays closed."""
        if self._gateway is not None:
            self._gateway.release(self)

    @property
    def min_poll_interval(self) -> float:
        """The SE30K SunSpec server is single-threaded: never poll faster than 1 Hz."""
        return 1.0

    @property
    def throttle_capabilities(self) -> ThrottleCaps:
        return ThrottleCaps(mode="proportional", response_time_s=1.0, cooldown_s=0.0, startup_delay_s=0.0)

    async def close(self) -> None:
        """Drop the session after the in-flight gateway transaction finished.

        The gateway waits for the lock holder (poll, write, commit) before it
        closes, so there is never a FIN in the middle of a transaction (the
        wedge signature). Writers queued behind close() find no session and
        get a clean "Not connected".
        """
        if self._gateway is not None:
            await self._gateway.close()
        logger.info("Disconnected from SolarEdge")
