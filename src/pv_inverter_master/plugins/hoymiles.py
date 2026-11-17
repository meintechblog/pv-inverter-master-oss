"""Hoymiles HMS (built-in WLAN DTU) plugin.

Talks directly to Hoymiles HMS-series micro-inverters with built-in WiFi
(HMS-300/350/400/500/600/700/800/1000/1500/1600/1800/2000, single- or
two-MPPT). No external DTU/OpenDTU gateway — the inverter exposes a
plain TCP protobuf service on port 10081.

Built on `hoymiles-wifi` (PyPI, MIT). The library handles framing,
CRC16, multi-packet stitching, and the 2 s minimum inter-call gap.

Behavioural notes baked into this plugin:
  - Inverter sleeps at night (no DC) and stops answering on TCP. Poll
    timeouts are treated as "asleep" — returned as success=True with
    empty registers so DeviceRegistry doesn't mark the device dead.
  - `set_power_limit` writes EEPROM on every call. The upstream README
    flatly warns about wear. Dead-time guard suppresses re-sends within
    60 s, AND a 2 % minimum delta guard skips no-op nudges from the
    waterfall distributor. `force=True` bypasses dead-time only (still
    honours bounds).
  - Hoymiles firmware enforces ~30 s between commands at the device
    side; pace polling >= 15 s in the registry to leave headroom for
    occasional limit writes.
"""
from __future__ import annotations

import asyncio
import time

import structlog

from pv_inverter_master.plugin import (
    InverterPlugin,
    PollResult,
    ThrottleCaps,
    WriteResult,
)
from pv_inverter_master.sunspec_models import (
    COMMON_DID,
    COMMON_LENGTH,
    NAMEPLATE_DID,
    NAMEPLATE_LENGTH,
    PROXY_UNIT_ID,
    _int16_as_uint16,
    encode_string,
)

log = structlog.get_logger()

DTU_PORT = 10081
POLL_TIMEOUT_S = 10.0
# EEPROM-wear protection. set_power_limit writes the inverter's EEPROM on
# every call (upstream README warns about wear). Live testing on an
# HMS-800W-2T showed the limit is applied within ~5 s and is genuinely
# proportional, but the persistent EEPROM write means we must NOT
# modulate continuously — so a 5-minute floor between actual writes.
DEAD_TIME_S = 300.0
MIN_LIMIT_DELTA_PCT = 2.0
APP_INFO_REFRESH_S = 600.0
# Hoymiles HMS built-in WiFi DTUs are tiny embedded modules. The vendor
# cloud polls them roughly every 15 min; community tooling (the Home
# Assistant integration) every ~40 s. Polling much faster overwhelms
# the DTU — it then returns degraded packets (0 W AC, collapsed DC
# voltage, or — over a longer window — bit-identical "frozen" frames)
# while the inverter is actually feeding in fine.
#
# Empirical history on the live fleet (mixed firmware: two FW 267
# units, one FW 4097):
#   - 2 s: instant freeze on all units (the original outage).
#   - 15 s: froze all three within minutes.
#   - 20 s: passed a 6 h afternoon soak (2026-05-23) cleanly, BUT
#     2026-05-24 the two FW 267 units froze ~5 h after the dawn
#     wake-up while the FW 4097 unit stayed fine. The afternoon-only
#     soak missed the full day+night+day cycle.
#   - 30 s: rock-solid across multiple full day/night cycles.
#
# 30 s is the safe baseline (all firmware revisions). FW >= 4097
# empirically tolerates 20 s indefinitely; the plugin reads the
# DTU's `dtu_sw_version` (auto-adopted into ``entry.firmware_version``
# on the first app-info refresh and persisted by the registry) and
# returns the tighter floor for those units.
MIN_POLL_INTERVAL_S = 30.0
MIN_POLL_INTERVAL_FAST_S = 20.0
FW_TOLERATES_FAST_POLL = 4097
# Night-mode backoff. At night an HMS inverter produces nothing and its
# built-in WiFi DTU answers with empty/None frames (or stops answering
# entirely). Once a few polls in a row yield no data we stop hammering
# it: most poll ticks become an instant no-op (zero network traffic),
# and only every NIGHT_PROBE_INTERVAL_S do we run one real poll to
# notice the inverter waking up at dawn.
NIGHT_MODE_AFTER_POLLS = 3
NIGHT_PROBE_INTERVAL_S = 600.0


class HoymilesPlugin(InverterPlugin):
    """Local-protocol plugin for Hoymiles HMS WLAN inverters."""

    def __init__(
        self,
        host: str,
        name: str = "",
        serial: str = "",
        rated_power: int = 0,
        firmware_version: str = "",
    ):
        self.host = host
        self.name = name or f"Hoymiles {host}"
        self.serial = serial
        # DTU sw version as a string (e.g. "4097"). Read at construction
        # from the persisted entry; refreshed live from the app-info
        # call. Drives `min_poll_interval` so the floor matches the
        # DTU's actual tolerance.
        self.firmware_version: str = str(firmware_version or "")
        self._max_power_w = rated_power if rated_power > 0 else 800
        # Lazy import keeps test collection fast and avoids a hard import
        # failure if the optional dep is missing in a stripped env.
        from hoymiles_wifi.dtu import DTU
        self._DTU = DTU
        self._dtu = DTU(host=host)
        self._connected = False
        # Live caches surfaced to the dashboard JSON snapshot.
        self.dc_channels: list[dict] = []
        self.hoymiles_status: dict = {
            "reachable": False,
            "producing": False,
            "limit_pct": 100,
            "signal_strength": None,
            "firmware_version": "",
        }
        # EEPROM wear protection
        self._last_limit_ts: float = 0.0
        self._last_limit_pct: float = 100.0
        self._limit_pending: bool = False
        # App-info refresh throttle
        self._app_info_ts: float = 0.0
        # Night-mode backoff state
        self._consecutive_unreachable: int = 0
        self._night_mode: bool = False
        self._last_night_probe: float = 0.0

    async def connect(self) -> None:
        self._connected = True
        log.info(
            "hoymiles_plugin_connected",
            host=self.host,
            name=self.name,
            serial=self.serial,
        )

    def _note_unreachable(self) -> None:
        """Count an unreachable poll; flip to night mode after a few."""
        self._consecutive_unreachable += 1
        if (
            not self._night_mode
            and self._consecutive_unreachable >= NIGHT_MODE_AFTER_POLLS
        ):
            self._night_mode = True
            self._last_night_probe = time.monotonic()
            log.info(
                "hoymiles_night_mode_entered",
                host=self.host,
                after_polls=self._consecutive_unreachable,
            )

    async def poll(self) -> PollResult:
        if not self._connected:
            return PollResult([], [], success=False, error="Not connected")

        # Night-mode backoff: the inverter is asleep. Skip the query
        # entirely on most ticks; every NIGHT_PROBE_INTERVAL_S fall
        # through to one real poll — if it yields production data we wake
        # up, otherwise we stay asleep. (A powered-down HMS keeps its TCP
        # port open, so only real data is a usable wake signal.)
        if self._night_mode:
            now = time.monotonic()
            if now - self._last_night_probe < NIGHT_PROBE_INTERVAL_S:
                return PollResult([], [], success=True, error=None)
            self._last_night_probe = now
            log.debug("hoymiles_night_probe", host=self.host)

        try:
            data = await asyncio.wait_for(
                self._dtu.async_get_real_data_new(),
                timeout=POLL_TIMEOUT_S,
            )
        except (asyncio.TimeoutError, OSError, ConnectionError) as e:
            # Night / unplugged / radio asleep — not a hard failure.
            self.hoymiles_status["reachable"] = False
            self.hoymiles_status["producing"] = False
            self._note_unreachable()
            log.debug("hoymiles_poll_timeout", host=self.host, error=str(e))
            return PollResult([], [], success=True, error=None)
        except Exception as e:
            self.hoymiles_status["reachable"] = False
            log.warning("hoymiles_poll_failed", host=self.host, error=str(e))
            return PollResult([], [], success=False, error=str(e))

        if data is None:
            # No usable frame. Usually the inverter is asleep: a
            # powered-down HMS keeps its TCP port open but answers with
            # an unparseable/empty frame. Count it toward night mode.
            self.hoymiles_status["reachable"] = False
            self.hoymiles_status["producing"] = False
            self._note_unreachable()
            return PollResult([], [], success=True, error=None)

        sgs_list = list(getattr(data, "sgs_data", []) or [])
        pv_list = list(getattr(data, "pv_data", []) or [])

        if not sgs_list and not pv_list:
            # DTU answered cleanly but with no inverter data — idle/asleep.
            self.hoymiles_status["reachable"] = True
            self.hoymiles_status["producing"] = False
            self._note_unreachable()
            return PollResult([], [], success=True, error=None)

        # Real inverter data — it is awake.
        if self._night_mode:
            log.info("hoymiles_woke_up", host=self.host)
        self._night_mode = False
        self._consecutive_unreachable = 0

        # Auto-adopt DTU serial on first successful poll
        dtu_serial = str(getattr(data, "device_serial_number", "") or "")
        if not self.serial and dtu_serial:
            self.serial = dtu_serial
            log.info("hoymiles_serial_auto_adopted", host=self.host, serial=self.serial)

        # AC side
        ac_power_w = sum(getattr(s, "active_power", 0) for s in sgs_list) / 10.0
        ac_voltage_v = (sgs_list[0].voltage / 10.0) if sgs_list else 0.0
        ac_freq_hz = (sgs_list[0].frequency / 100.0) if sgs_list else 0.0
        ac_current_a = sum(getattr(s, "current", 0) for s in sgs_list) / 100.0
        temperature_c = (sgs_list[0].temperature / 10.0) if sgs_list else 0.0

        # DC side — per-PV channels
        dc_power_w = 0.0
        dc_current_a = 0.0
        dc_voltage_power_sum = 0.0
        dc_power_sum = 0.0
        total_energy_wh = 0
        today_energy_wh = 0
        cached_dc: list[dict] = []
        for idx, pv in enumerate(pv_list):
            v = getattr(pv, "voltage", 0) / 10.0
            a = getattr(pv, "current", 0) / 100.0
            p = getattr(pv, "power", 0) / 10.0
            yt = int(getattr(pv, "energy_total", 0))
            yd = int(getattr(pv, "energy_daily", 0))
            dc_power_w += p
            dc_current_a += a
            dc_voltage_power_sum += v * p
            dc_power_sum += p
            total_energy_wh += yt
            today_energy_wh += yd
            port = getattr(pv, "port_number", idx + 1)
            cached_dc.append({
                "name": f"PV{port}",
                "voltage_v": round(v, 1),
                "current_a": round(a, 2),
                "power_w": round(p, 1),
                "yield_day_wh": yd,
                "yield_total_kwh": round(yt / 1000.0, 1),
            })
        dc_voltage_v = (dc_voltage_power_sum / dc_power_sum) if dc_power_sum > 0 else 0.0
        self.dc_channels = cached_dc

        producing = ac_power_w > 1.0
        status_code = 4 if producing else 2  # 4 = MPPT, 2 = SLEEPING

        # power_limit field in protobuf is documented as percent in some
        # firmware revs and 0.1 % in others. Treat any value > 100 as 0.1 %
        # encoding and divide.
        raw_limit = getattr(sgs_list[0], "power_limit", 0) if sgs_list else 0
        limit_pct = raw_limit / 10.0 if raw_limit > 100 else float(raw_limit)
        if not (0 < limit_pct <= 100):
            limit_pct = 100.0
        self.hoymiles_status.update({
            "reachable": True,
            "producing": producing,
            "limit_pct": int(round(limit_pct)),
        })

        inverter_regs = self._encode_model_103(
            ac_power_w=ac_power_w,
            ac_voltage_v=ac_voltage_v,
            ac_current_a=ac_current_a,
            ac_freq_hz=ac_freq_hz,
            dc_power_w=dc_power_w,
            dc_voltage_v=dc_voltage_v,
            dc_current_a=dc_current_a,
            temperature_c=temperature_c,
            energy_total_wh=total_energy_wh,
            yield_day_wh=today_energy_wh,
            status_code=status_code,
        )
        common_regs = self._build_common_registers()

        # Periodic firmware / signal strength refresh
        now = time.monotonic()
        if now - self._app_info_ts > APP_INFO_REFRESH_S:
            self._app_info_ts = now
            try:
                info = await asyncio.wait_for(
                    self._dtu.async_app_information_data(),
                    timeout=POLL_TIMEOUT_S,
                )
                dtu_info = getattr(info, "dtu_info", None) if info is not None else None
                if dtu_info is not None:
                    self.hoymiles_status["signal_strength"] = getattr(dtu_info, "signal_strength", None)
                    sw_ver = getattr(dtu_info, "dtu_sw_version", "")
                    if sw_ver:
                        sw_ver_str = str(sw_ver)
                        self.hoymiles_status["firmware_version"] = sw_ver_str
                        # Surface to a top-level attribute so the registry
                        # can auto-persist it to entry.firmware_version,
                        # giving the next start_device call the right
                        # per-firmware poll floor immediately.
                        self.firmware_version = sw_ver_str
            except Exception:
                pass  # non-fatal, surface stale firmware/signal if any

        return PollResult(common_regs, inverter_regs, success=True)

    def _encode_model_103(
        self,
        *,
        ac_power_w: float,
        ac_voltage_v: float,
        ac_current_a: float,
        ac_freq_hz: float,
        dc_power_w: float,
        dc_voltage_v: float,
        dc_current_a: float,
        temperature_c: float,
        energy_total_wh: int,
        yield_day_wh: int,
        status_code: int,
    ) -> list[int]:
        regs = [0] * 52
        regs[0] = 103
        regs[1] = 50

        # AC Current (offset 2-6), SF=-2
        ac_i_x100 = int(round(abs(ac_current_a) * 100))
        regs[2] = ac_i_x100
        regs[3] = ac_i_x100
        regs[6] = _int16_as_uint16(-2)

        # AC Voltage AN (offset 10), SF=-1
        regs[10] = int(round(ac_voltage_v * 10))
        regs[13] = _int16_as_uint16(-1)

        # AC Power (offset 14-15), SF=0
        regs[14] = int(round(abs(ac_power_w))) & 0xFFFF
        regs[15] = 0

        # AC Frequency (offset 16-17), SF=-2
        regs[16] = int(round(ac_freq_hz * 100))
        regs[17] = _int16_as_uint16(-2)

        # AC Energy total Wh (acc32, offset 24-26), SF=0
        e = max(0, int(energy_total_wh))
        regs[24] = (e >> 16) & 0xFFFF
        regs[25] = e & 0xFFFF
        regs[26] = 0

        # DC Current (offset 27-28), SF=-2
        regs[27] = int(round(abs(dc_current_a) * 100))
        regs[28] = _int16_as_uint16(-2)

        # DC Voltage (offset 29-30), SF=-1
        regs[29] = int(round(dc_voltage_v * 10))
        regs[30] = _int16_as_uint16(-1)

        # DC Power (offset 31-32), SF=0
        regs[31] = int(round(abs(dc_power_w)))
        regs[32] = 0

        # Cabinet Temperature (offset 33-37), SF=-1
        regs[33] = int(round(temperature_c * 10))
        regs[37] = _int16_as_uint16(-1)

        # Operating state
        regs[38] = status_code
        return regs

    def _build_common_registers(self) -> list[int]:
        regs = [0] * 67
        regs[0] = COMMON_DID
        regs[1] = COMMON_LENGTH
        regs[2:18] = encode_string("Hoymiles", 16)
        regs[18:34] = encode_string(self.name, 16)
        regs[50:66] = encode_string(self.serial, 16)
        regs[66] = PROXY_UNIT_ID
        return regs

    def get_static_common_overrides(self) -> dict[int, int]:
        ov: dict[int, int] = {0: COMMON_DID, 1: COMMON_LENGTH}
        for i, val in enumerate(encode_string("Hoymiles", 16)):
            ov[2 + i] = val
        for i, val in enumerate(encode_string(self.name, 16)):
            ov[18 + i] = val
        ov[66] = PROXY_UNIT_ID
        return ov

    def get_model_120_registers(self) -> list[int]:
        regs = [0] * 28
        regs[0] = NAMEPLATE_DID
        regs[1] = NAMEPLATE_LENGTH
        regs[2] = 4  # DERTyp = PV
        regs[3] = self._max_power_w
        regs[4] = 0
        return regs

    async def write_power_limit(
        self,
        enable: bool,
        limit_pct: float,
        *,
        force: bool = False,
    ) -> WriteResult:
        if not self._connected:
            return WriteResult(success=False, error="Not connected")

        target = 100.0 if not enable else max(0.0, min(100.0, float(limit_pct)))
        now = time.monotonic()
        if not force and self._limit_pending:
            if (now - self._last_limit_ts) < DEAD_TIME_S:
                return WriteResult(success=True)
            if abs(target - self._last_limit_pct) < MIN_LIMIT_DELTA_PCT:
                return WriteResult(success=True)

        try:
            resp = await asyncio.wait_for(
                self._dtu.async_set_power_limit(int(round(target))),
                timeout=POLL_TIMEOUT_S,
            )
        except Exception as e:
            log.warning("hoymiles_power_limit_failed", host=self.host, error=str(e))
            return WriteResult(success=False, error=str(e))

        if resp is None:
            return WriteResult(success=False, error="No response (device asleep or CRC fail)")

        self._last_limit_ts = now
        self._last_limit_pct = target
        self._limit_pending = True
        log.info(
            "hoymiles_power_limit_set",
            host=self.host,
            target_pct=target,
            enable=enable,
            forced=force,
        )
        return WriteResult(success=True)

    async def send_power_command(self, action: str) -> WriteResult:
        if not self._connected:
            return WriteResult(success=False, error="Not connected")
        if not self.serial:
            return WriteResult(success=False, error="Serial not yet adopted — poll the inverter once first")

        try:
            if action == "on":
                resp = await asyncio.wait_for(self._dtu.async_turn_on_inverter(self.serial), timeout=POLL_TIMEOUT_S)
            elif action == "off":
                resp = await asyncio.wait_for(self._dtu.async_turn_off_inverter(self.serial), timeout=POLL_TIMEOUT_S)
            elif action == "restart":
                resp = await asyncio.wait_for(self._dtu.async_reboot_inverter(self.serial), timeout=POLL_TIMEOUT_S)
            else:
                return WriteResult(success=False, error=f"Unknown action: {action}")
        except Exception as e:
            log.warning("hoymiles_power_command_failed", host=self.host, action=action, error=str(e))
            return WriteResult(success=False, error=str(e))

        if resp is None:
            return WriteResult(success=False, error="No response from inverter")
        log.info("hoymiles_power_command", host=self.host, action=action)
        return WriteResult(success=True)

    @property
    def min_poll_interval(self) -> float:
        # Protect the rate-limited HMS DTU. Newer firmware
        # (>= FW_TOLERATES_FAST_POLL) holds up at 20 s indefinitely;
        # older firmware needs 30 s to survive a full day cycle. An
        # unknown / non-numeric firmware string falls back to the
        # safe baseline.
        try:
            fw = int(self.firmware_version)
        except (TypeError, ValueError):
            fw = 0
        return MIN_POLL_INTERVAL_FAST_S if fw >= FW_TOLERATES_FAST_POLL else MIN_POLL_INTERVAL_S

    async def get_inverter_status(self) -> dict:
        return dict(self.hoymiles_status)

    async def reconfigure(self, host: str, port: int, unit_id: int) -> None:
        # port / unit_id are SolarEdge concepts; only host matters here.
        if host != self.host:
            self.host = host
            self._dtu = self._DTU(host=host)
            # New host — drop any night-mode state from the old one.
            self._night_mode = False
            self._consecutive_unreachable = 0
            log.info("hoymiles_reconfigured", host=self.host)

    @property
    def throttle_capabilities(self) -> ThrottleCaps:
        # Values from a live regulation test on an HMS-800W-2T
        # (FW 4097) reachable over WLAN:
        #   - read round-trip ~0.2 s, fresh data every 5 s, no rate-limit
        #   - set_power_limit round-trip ~0.2 s, accepted as a true
        #     proportional limit (50 % -> power_limit register 500),
        #     reflected within one 5 s poll  -> response_time ~10 s once
        #     the inverter's own ramp is included.
        #   - BUT every limit write hits the EEPROM (persistent), so the
        #     device must not be modulated continuously -> a long
        #     cooldown is the honest capability, not a fast one.
        # Score = 7.0 (base) + 0 (response_bonus, response>=10 s)
        #         - 2.0 (cooldown penalty, capped) = 5.0. That places
        # HMS-WLAN below radio-Hoymiles via OpenDTU (7.0) and SolarEdge
        # (9.7) but above binary Shelly (2.8) — the distributor reaches
        # for it only after the freely-modulatable inverters.
        return ThrottleCaps(
            mode="proportional",
            response_time_s=10.0,
            cooldown_s=300.0,
            startup_delay_s=0.0,
        )

    async def close(self) -> None:
        self._connected = False
        log.info("hoymiles_plugin_closed", host=self.host)


async def discover_hoymiles_devices(
    subnet_override: str | None = None,
    skip_ips: set[str] | None = None,
    tcp_timeout: float = 0.6,
    probe_timeout: float = 8.0,
    concurrency: int = 40,
) -> list[dict]:
    """Scan a subnet for Hoymiles HMS WLAN inverters.

    HMS inverters expose a built-in WiFi DTU on TCP 10081 and do NOT
    announce via mDNS, so discovery is a port sweep plus a protobuf
    probe:
      Phase 1 — concurrent TCP probe of port 10081 across the subnet.
      Phase 2 — hoymiles protobuf probe of each open host for serial,
                panel count and live AC power.

    `subnet_override` accepts CIDR / "x"-shorthand / single-IP just like
    the SunSpec scanner, so VPN-reachable remote networks work too.
    Returns one dict per open host: {host, serial, firmware, panels,
    producing, ac_power_w, sleeping}. A host whose port is open but
    gives no parseable response is returned with sleeping=True so the
    UI can still offer it.
    """
    from pv_inverter_master.scanner import (
        _probe_port,
        detect_subnet,
        parse_subnet_override,
    )

    skip = skip_ips or set()
    subnet = (
        parse_subnet_override(subnet_override)
        if subnet_override
        else detect_subnet()
    )
    # Already-configured hosts are still scanned — they are kept in the
    # results flagged ``already_added`` so the UI greys them out.
    hosts = [str(ip) for ip in subnet.hosts()]

    # Phase 1: concurrent TCP sweep of port 10081
    sem = asyncio.Semaphore(concurrency)

    async def _sweep(ip: str) -> tuple[str, bool]:
        async with sem:
            return ip, await _probe_port(ip, DTU_PORT, tcp_timeout)

    swept = await asyncio.gather(*(_sweep(ip) for ip in hosts))
    open_hosts = [ip for ip, is_open in swept if is_open]

    log.info(
        "hoymiles_discover_sweep",
        subnet=str(subnet),
        scanned=len(hosts),
        open_10081=len(open_hosts),
    )

    # Phase 2: protobuf probe each open host
    from hoymiles_wifi.dtu import DTU

    found: list[dict] = []
    for ip in open_hosts:
        entry: dict = {
            "host": ip,
            "serial": "",
            "firmware": "",
            "panels": 0,
            "producing": False,
            "ac_power_w": 0,
            "sleeping": True,
            "already_added": ip in skip,
        }
        try:
            rd = await asyncio.wait_for(
                DTU(host=ip).async_get_real_data_new(),
                timeout=probe_timeout,
            )
        except Exception:
            rd = None
        if rd is not None:
            sgs = list(getattr(rd, "sgs_data", []) or [])
            pv = list(getattr(rd, "pv_data", []) or [])
            ac = sum(getattr(s, "active_power", 0) for s in sgs) / 10.0
            entry.update({
                "serial": str(getattr(rd, "device_serial_number", "") or ""),
                "panels": len(pv),
                "producing": ac > 1.0,
                "ac_power_w": round(ac),
                "sleeping": False,
            })
        found.append(entry)

    return found
