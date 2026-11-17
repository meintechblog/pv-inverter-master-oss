"""AggregationLayer: sum N device register sets into one virtual Fronius inverter.

Decodes per-device SunSpec Model 103 registers to physical values,
sums power/current/energy, averages voltage/frequency, takes max temperature,
and re-encodes with consistent fixed scale factors into the shared RegisterCache
that Venus OS reads via Modbus TCP.
"""
from __future__ import annotations

import time

import structlog

from pv_inverter_master.config import Config, inverter_counts_for_aggregate
from pv_inverter_master.context import AppContext
from pv_inverter_master.proxy import COMMON_CACHE_ADDR, INVERTER_CACHE_ADDR
from pv_inverter_master.register_cache import RegisterCache
from pv_inverter_master.sunspec_models import (
    COMMON_ADDR,
    COMMON_DID,
    COMMON_LENGTH,
    PROXY_UNIT_ID,
    _int16_as_uint16,
    encode_string,
)

logger = structlog.get_logger()


def decode_model_103_to_physical(inverter_regs: list[int]) -> dict:
    """Decode Model 103 register list to physical values.

    Args:
        inverter_regs: 52 uint16 values (DID + Length + 50 data fields)

    Returns:
        Dict with physical values in standard units (W, A, V, Hz, Wh, C)
    """
    if len(inverter_regs) < 40:
        return None  # Insufficient register data

    def _sf(idx: int) -> int:
        raw = inverter_regs[idx]
        return raw - 65536 if raw > 32767 else raw

    def _val(idx: int, sf_idx: int) -> float:
        if idx >= len(inverter_regs) or sf_idx >= len(inverter_regs):
            return 0.0
        raw = inverter_regs[idx]
        if raw in (0x8000, 0xFFFF):
            return 0.0
        return raw * (10 ** _sf(sf_idx))

    return {
        "ac_current_a": _val(2, 6),
        "ac_current_l1_a": _val(3, 6),
        "ac_current_l2_a": _val(4, 6),
        "ac_current_l3_a": _val(5, 6),
        "ac_voltage_ab_v": _val(7, 13),
        "ac_voltage_bc_v": _val(8, 13),
        "ac_voltage_ca_v": _val(9, 13),
        "ac_voltage_an_v": _val(10, 13),
        "ac_voltage_bn_v": _val(11, 13),
        "ac_voltage_cn_v": _val(12, 13),
        "ac_power_w": _val(14, 15),
        "ac_frequency_hz": _val(16, 17),
        "ac_va": _val(18, 19),
        "ac_var": _val(20, 21),
        "ac_pf": _val(22, 23),
        "energy_total_wh": ((inverter_regs[24] << 16) | inverter_regs[25]) * (10 ** _sf(26)),
        "dc_current_a": _val(27, 28),
        "dc_voltage_v": _val(29, 30),
        "dc_power_w": _val(31, 32),
        "temperature_c": _val(33, 37),
        "temperature_sink_c": _val(34, 37),
        "status_code": inverter_regs[38],
        "status_vendor": inverter_regs[39] if len(inverter_regs) > 39 else 0,
    }


def _apply_phase_routing(decoded: dict, entry) -> None:
    """Route a single-phase inverter's current and voltage onto the L_n slot
    that matches its physical phase assignment.

    Three-phase devices (SE30K, Sungrow, etc.) report current and voltage on
    all three phases natively — they are detected by having non-zero L2 or
    L3 current and are left untouched.

    Single-phase devices (Hoymiles HMS, OpenDTU, Shelly) populate only L1
    in the SunSpec frame because that's where the upstream plugin places
    them. When the user installs such an inverter on L2 or L3, the
    `phase` field on InverterEntry tells us where to actually route the
    values so the aggregate per-phase view matches the wired reality.
    """
    if decoded.get("ac_current_l2_a") or decoded.get("ac_current_l3_a"):
        return  # three-phase device, no rewrite needed
    phase = getattr(entry, "phase", 1) or 1
    if phase not in (2, 3):
        return  # default L1, already correct
    i_l1 = decoded.get("ac_current_l1_a", 0) or 0
    v_an = decoded.get("ac_voltage_an_v", 0) or 0
    if phase == 2:
        decoded["ac_current_l2_a"] = i_l1
        if v_an > 0:
            decoded["ac_voltage_bn_v"] = v_an
    elif phase == 3:
        decoded["ac_current_l3_a"] = i_l1
        if v_an > 0:
            decoded["ac_voltage_cn_v"] = v_an
    decoded["ac_current_l1_a"] = 0
    if v_an > 0:
        decoded["ac_voltage_an_v"] = 0


def encode_aggregated_model_103(totals: dict) -> list[int]:
    """Encode aggregated physical values to 52 uint16 SunSpec Model 103 registers.

    Uses FIXED scale factors for consistency:
    - Power: SF=0 (watts as integer)
    - Current: SF=-2 (0.01A resolution)
    - Voltage: SF=-1 (0.1V resolution)
    - Frequency: SF=-2 (0.01Hz resolution)
    - Energy: SF=0 (Wh as integer)
    - Temperature: SF=-1 (0.1C resolution)
    """
    regs = [0] * 52
    regs[0] = 103  # DID
    regs[1] = 50   # Length

    # AC Current (SF=-2)
    regs[2] = int(round(totals["ac_current_a"] * 100)) & 0xFFFF
    regs[3] = int(round(totals["ac_current_l1_a"] * 100)) & 0xFFFF
    regs[4] = int(round(totals["ac_current_l2_a"] * 100)) & 0xFFFF
    regs[5] = int(round(totals["ac_current_l3_a"] * 100)) & 0xFFFF
    regs[6] = _int16_as_uint16(-2)

    # AC Voltage (SF=-1)
    # SunSpec Model 103 layout: regs[7..9] are line-to-line, regs[10..12] are
    # line-to-neutral. Earlier revisions only set reg[10] (L1-N) — Venus then
    # rendered L2/L3 at 0 V and showed per-phase power = V*I = 0 even when
    # current was correctly distributed across phases.
    regs[7] = int(round(totals.get("ac_voltage_ab_v", 0) * 10)) & 0xFFFF
    regs[8] = int(round(totals.get("ac_voltage_bc_v", 0) * 10)) & 0xFFFF
    regs[9] = int(round(totals.get("ac_voltage_ca_v", 0) * 10)) & 0xFFFF
    regs[10] = int(round(totals["ac_voltage_an_v"] * 10)) & 0xFFFF
    regs[11] = int(round(totals.get("ac_voltage_bn_v", 0) * 10)) & 0xFFFF
    regs[12] = int(round(totals.get("ac_voltage_cn_v", 0) * 10)) & 0xFFFF
    regs[13] = _int16_as_uint16(-1)

    # AC Power (SF=0)
    regs[14] = int(round(totals["ac_power_w"])) & 0xFFFF
    regs[15] = 0

    # AC Frequency (SF=-2)
    regs[16] = int(round(totals["ac_frequency_hz"] * 100)) & 0xFFFF
    regs[17] = _int16_as_uint16(-2)

    # Energy (SF=0, Wh) -- uint32 split across 2 registers
    energy_wh = int(round(totals["energy_total_wh"]))
    regs[24] = (energy_wh >> 16) & 0xFFFF
    regs[25] = energy_wh & 0xFFFF
    regs[26] = 0

    # DC Current (SF=-2)
    regs[27] = int(round(totals["dc_current_a"] * 100)) & 0xFFFF
    regs[28] = _int16_as_uint16(-2)

    # DC Voltage (SF=-1)
    regs[29] = int(round(totals["dc_voltage_v"] * 10)) & 0xFFFF
    regs[30] = _int16_as_uint16(-1)

    # DC Power (SF=0)
    regs[31] = int(round(totals["dc_power_w"])) & 0xFFFF
    regs[32] = 0

    # Temperature (SF=-1)
    regs[33] = int(round(totals["temperature_c"] * 10)) & 0xFFFF
    regs[37] = _int16_as_uint16(-1)

    # Status -- worst-case across devices
    regs[38] = totals["status_code"]

    return regs


class AggregationLayer:
    """Sums all active device outputs into one virtual Fronius inverter.

    Called after each successful device poll via the on_poll_success callback.
    Reads DeviceState.last_poll_data from all devices in app_ctx.devices,
    decodes to physical values, aggregates, and writes to the shared
    RegisterCache that Venus OS reads.
    """

    #: Absolute floor for the per-device staleness bound.
    #:
    #: Intentionally generous, and NOT tied to ``proxy.staleness_timeout``.
    #: Measured on the live system (2026-07-18, 60 s window, n=24): the SE30K's
    #: poll age sawtooths 0.4 s → 17.1 s, with a 32 s sample observed earlier
    #: the same morning — while its CONFIGURED ``poll_interval`` is 0.0 (i.e.
    #: the 1 s proxy default). The configured interval therefore says nothing
    #: about a device's real cadence: a slow Modbus device issues several reads
    #: per cycle, and on failure the poll loop backs off (``conn_mgr``
    #: ``sleep_duration``, up to 60 s) rather than honouring ``poll_interval``.
    #:
    #: A tight bound would make a HEALTHY device flap in and out of the
    #: aggregate — intermittently dropping the main PV source is worse than
    #: carrying a dead one a little longer. The harm this gate exists to stop
    #: is data frozen for MINUTES (the July wedge froze it for an 8 min quiesce
    #: window), so 120 s catches it with room to spare.
    AGGREGATE_STALE_FLOOR_S = 120.0

    #: Multiple of a device's own configured cadence, used when that cadence is
    #: slower than the floor (e.g. a device intentionally polled every 60 s).
    STALE_CYCLE_FACTOR = 3.0

    def __init__(self, app_ctx: AppContext, cache: RegisterCache, config: Config, broadcast_fn=None) -> None:
        self._app_ctx = app_ctx
        self._cache = cache
        self._config = config
        self._broadcast_fn = broadcast_fn
        # Device IDs currently excluded as stale — used purely to log the
        # transition once instead of on every recalculate (which runs on each
        # successful poll of ANY device).
        self._stale_excluded: set[str] = set()

    def _device_data_is_fresh(self, ds, device_id: str, now: float) -> bool:
        """True if ``ds``'s data may contribute to the Venus-facing aggregate.

        Two independent conditions, because no single one covers every plugin:

        * **Age** — for plugins that report failure as failure (solaredge,
          sungrow, shelly, senec) a dead device stops advancing its success
          stamp, so the age bound catches it. This is the SE30K wedge case.
        * **source_stale** — the HTTP plugins (hoymiles, opendtu) intentionally
          report a timeout or an unparseable frame as ``success=True`` and flag
          ``source_stale`` instead. Their success stamp keeps advancing, so the
          age bound alone would NEVER fire for them. Today they also return
          empty register lists, which ``decode_model_103_to_physical`` rejects
          — but that is an incidental second line of defence, not this gate's
          doing. Checking ``source_stale`` makes the exclusion intentional, so
          a future plugin change (e.g. "serve last known registers on a stale
          frame") cannot silently reintroduce phantom power.

        Age is measured on the monotonic clock: a backward wall-clock step
        would otherwise yield a negative age, pass the ``<= max_age`` test and
        mark every device — including genuinely dead ones — fresh, without so
        much as a log line.

        Logs the stale/fresh transition once per direction so a dead device is
        visible in the log without spamming it on every poll.
        """
        last_ok = getattr(ds, "last_successful_poll_monotonic", None)
        interval = getattr(ds, "poll_interval", 0.0) or self._config.proxy.poll_interval
        max_age = max(self.AGGREGATE_STALE_FLOOR_S, interval * self.STALE_CYCLE_FACTOR)
        source_stale = bool(getattr(ds, "source_stale", False))

        age = (now - last_ok) if last_ok is not None else None
        fresh = age is not None and 0.0 <= age <= max_age and not source_stale

        if not fresh:
            if device_id not in self._stale_excluded:
                self._stale_excluded.add(device_id)
                logger.warning(
                    "aggregate_device_excluded_stale",
                    device_id=device_id,
                    age_s=round(age, 1) if age is not None else None,
                    max_age_s=round(max_age, 1),
                    reason=(
                        "never_polled" if age is None
                        else "source_stale" if source_stale
                        else "stale"
                    ),
                )
        elif device_id in self._stale_excluded:
            self._stale_excluded.discard(device_id)
            logger.info("aggregate_device_rejoined", device_id=device_id)

        return fresh

    async def recalculate(self, device_id: str) -> None:
        """Aggregate all active device data into the shared cache.

        Called after each successful poll. Takes a snapshot of device IDs
        to avoid RuntimeError from concurrent dict modification.

        Args:
            device_id: The device that just completed polling (for logging).
        """
        # Snapshot to avoid dict-changed-size-during-iteration
        device_ids = list(self._app_ctx.devices.keys())

        # Build set of device IDs that participate in aggregation
        aggregate_ids = {
            e.id for e in self._config.inverters
            if inverter_counts_for_aggregate(self._config, e)
        }

        # Collect active states paired with their config entry — we need the
        # entry to look up `phase` for single-phase devices, so per-device
        # current and voltage can be routed to the correct L1/L2/L3 slot.
        entry_by_id = {e.id: e for e in self._config.inverters}
        active_pairs: list[tuple[dict, "InverterEntry"]] = []
        # Sampled once so every device in this pass is judged against the same
        # instant. Monotonic — see _device_data_is_fresh.
        now = time.monotonic()
        # Drop bookkeeping for devices that no longer exist, so a removed and
        # later re-added ID does not log a "rejoined" it never left.
        self._stale_excluded &= set(device_ids)
        for did in device_ids:
            if did not in aggregate_ids:
                continue
            ds = self._app_ctx.devices.get(did)
            if ds is None:
                continue
            poll_data = ds.last_poll_data
            entry = entry_by_id.get(did)
            if poll_data is None or "inverter_registers" not in poll_data or entry is None:
                continue
            # Per-device freshness gate. `last_poll_data` is only written on a
            # successful poll and is NEVER cleared on failure, so a device that
            # stops polling keeps contributing its last frozen registers to the
            # sum Venus regulates on — indefinitely. The cache's own `is_stale`
            # cannot catch it: it tracks ONE global timestamp refreshed by ANY
            # device's successful poll, so a single healthy sibling masks every
            # dead one (phantom PV).
            #
            # Fail-safe direction (R33): drop the contribution rather than
            # invent one. Under-reporting PV is recoverable; regulating on
            # power that no longer exists is not.
            #
            # NOTE: this gate is not the only liveness signal in the system —
            # the distributor independently gates on `conn_mgr.state`
            # (distributor.py), which leaves CONNECTED after ~3 failed polls,
            # i.e. sooner than this bound. During that window the aggregate may
            # still count a device the distributor has already dropped from the
            # curtailment budget. Conservative (reported PV high, eligible
            # capacity low → healthy devices absorb the cut), but two liveness
            # truths in one control system can drift; unifying them is tracked
            # separately.
            if not self._device_data_is_fresh(ds, did, now):
                continue
            active_pairs.append((poll_data, entry))

        if not active_pairs:
            # No data -- cache stays stale, no update
            return

        # Decode each device's registers to physical values and apply
        # per-phase routing for single-phase units (Hoymiles HMS, OpenDTU,
        # Shelly EM/Pro etc.) so their current and voltage land on the
        # configured L1/L2/L3 slot instead of always defaulting to L1.
        decoded_list: list[dict] = []
        for poll_data, entry in active_pairs:
            decoded = decode_model_103_to_physical(poll_data["inverter_registers"])
            if decoded is None:
                continue
            _apply_phase_routing(decoded, entry)
            decoded_list.append(decoded)
        if not decoded_list:
            return

        # Aggregate: sum, average, max, worst-case
        n = len(decoded_list)

        # Fields to sum
        sum_keys = [
            "ac_power_w", "ac_current_a", "ac_current_l1_a",
            "ac_current_l2_a", "ac_current_l3_a", "energy_total_wh",
            "dc_current_a", "dc_power_w",
        ]
        # Phase voltages: take max across devices so a single-phase inverter
        # (which reports 0 V on the two phases it isn't on) doesn't drag the
        # per-phase average toward 0 — historic bug that made Venus render
        # L2/L3 at 0 V and per-phase power as 0 W. Frequency is a true global.
        max_keys = [
            "ac_voltage_ab_v", "ac_voltage_bc_v", "ac_voltage_ca_v",
            "ac_voltage_an_v", "ac_voltage_bn_v", "ac_voltage_cn_v",
        ]
        avg_keys = ["ac_frequency_hz"]

        totals: dict = {}

        for key in sum_keys:
            totals[key] = sum(d[key] for d in decoded_list)

        for key in max_keys:
            totals[key] = max((d.get(key, 0) or 0) for d in decoded_list)

        for key in avg_keys:
            totals[key] = sum(d[key] for d in decoded_list) / n

        # DC voltage: average only devices with actual DC power (skip Shelly/zero-DC)
        dc_devices = [d for d in decoded_list if d["dc_power_w"] > 0]
        totals["dc_voltage_v"] = (
            sum(d["dc_voltage_v"] for d in dc_devices) / len(dc_devices)
            if dc_devices else 0.0
        )

        # Max temperature
        totals["temperature_c"] = max(d["temperature_c"] for d in decoded_list)

        # Worst status (highest code -- 4=MPPT is best, higher is worse)
        totals["status_code"] = max(d["status_code"] for d in decoded_list)

        # Encode aggregated inverter registers
        inverter_regs = encode_aggregated_model_103(totals)
        self._cache.update(INVERTER_CACHE_ADDR, inverter_regs)

        # Build virtual Common Model registers
        virtual_common = self._build_virtual_common()
        self._cache.update(COMMON_CACHE_ADDR, virtual_common)

        # Update WRtg in Model 120
        self._update_wrtg(device_ids)

        logger.debug(
            "aggregation_complete",
            device_count=n,
            total_power_w=totals["ac_power_w"],
            trigger_device=device_id,
        )

        if self._broadcast_fn is not None:
            await self._broadcast_fn(device_id)

    def update_wrtg(self) -> None:
        """Recalculate and write WRtg to datablock. Called after device add/remove."""
        device_ids = list(self._app_ctx.devices.keys())
        self._update_wrtg(device_ids)

    def _update_wrtg(self, active_device_ids: list[str]) -> None:
        """Write WRtg (sum of active rated powers) to Model 120 in the datablock."""
        total_rated = 0
        for entry in self._config.inverters:
            if (
                entry.enabled
                and inverter_counts_for_aggregate(self._config, entry)
                and entry.id in active_device_ids
                and entry.rated_power > 0
            ):
                total_rated += entry.rated_power

        if total_rated > 0:
            # WRtg is at Model 120 offset 3 (DID=120, Len=26, DERTyp, WRtg)
            # Address 40124, datablock address = 40124 + 1 = 40125
            self._cache.datablock.setValues(40125, [total_rated])

    def _build_virtual_common(self) -> list[int]:
        """Build a 67-register Common Model block for the virtual inverter."""
        regs = [0] * 67

        # DID and Length
        regs[0] = COMMON_DID     # 1
        regs[1] = COMMON_LENGTH  # 65

        # C_Manufacturer (offset 2-17, 16 registers): "Fronius"
        regs[2:18] = encode_string("Fronius", 16)

        # C_Model (offset 18-33, 16 registers): user-defined name or default
        name = self._config.virtual_inverter.name
        if not name:
            name = "Fronius Proxy"
        regs[18:34] = encode_string(name, 16)

        # C_Options (offset 34-41, 8 registers): empty
        regs[34:42] = encode_string("", 8)

        # C_Version (offset 42-49, 8 registers): proxy version
        regs[42:50] = encode_string("v4.0", 8)

        # C_SerialNumber (offset 50-65, 16 registers): empty
        regs[50:66] = encode_string("", 16)

        # C_DeviceAddress (offset 66)
        regs[66] = PROXY_UNIT_ID  # 126

        return regs
