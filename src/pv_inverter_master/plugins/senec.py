"""SENEC.Home local-API plugin — battery storage / PV system.

Talks to a SENEC appliance over its undocumented local JSON API
(``lala.cgi`` — the same endpoint the SENEC web UI uses). A POST of a
nested ``{"SECTION": {"FIELD": ""}}`` request returns the requested
values as type-tagged strings: ``fl_`` IEEE-754 float32, ``u8_``/``u3_``
hex integers, ``st_`` strings, or the literal ``VARIABLE_NOT_FOUND``.

Recent SENEC firmware removed the ``STATISTIC`` section, so there is no
lifetime energy counter to read — this plugin integrates AC power over
time and persists the running total so the SunSpec energy register
survives a proxy restart.

The SENEC local ``lala.cgi`` API exposes no PV-curtailment control, so
the plugin is monitoring-only (``throttle mode = none``) and
``write_power_limit`` is a no-op. ``PV1.POWER_RATIO`` (reads 100 %) was
investigated and ruled out — see ``write_power_limit`` for the evidence.
"""
from __future__ import annotations

import asyncio
import json
import os
import struct
import tempfile
import time

import aiohttp
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

POLL_TIMEOUT_S = 8.0
ENERGY_SAVE_INTERVAL_S = 60.0
# A poll gap longer than this is treated as a restart/outage — skip the
# energy integration step rather than booking a huge bogus increment.
MAX_ENERGY_GAP_S = 600.0

# lala.cgi request bodies. An empty-string value asks the box to fill
# that field in its response.
SENEC_POLL_REQUEST = {
    "ENERGY": {
        "GUI_INVERTER_POWER": "",
        "GUI_HOUSE_POW": "",
        "GUI_GRID_POW": "",
        "GUI_BAT_DATA_POWER": "",
        "GUI_BAT_DATA_FUEL_CHARGE": "",
        "GUI_BAT_DATA_VOLTAGE": "",
        "GUI_BAT_DATA_CURRENT": "",
        "STAT_STATE": "",
        "STAT_HOURS_OF_OPERATION": "",
    },
    "PV1": {"MPP_POWER": "", "MPP_VOL": "", "MPP_CUR": ""},
    "PM1OBJ1": {"U_AC": "", "I_AC": "", "P_AC": "", "FREQ": ""},
    "TEMPMEASURE": {"CASE_TEMP": "", "MCU_TEMP": "", "BATTERY_TEMP": ""},
}
SENEC_IDENTITY_REQUEST = {
    "FACTORY": {"SYS_TYPE": "", "DEVICE_ID": "", "DESIGN_CAPACITY": ""},
    "SYS_UPDATE": {"NPU_VER": "", "UPDATE_AVAILABLE": ""},
}

# Numeric SENEC system-state codes -> label. Source: pysenec
# (github.com/mchwalisz/pysenec), the de-facto reference map.
SENEC_STATE_NAMES = {
    0: "INITIAL STATE", 1: "ERROR INVERTER COMMUNICATION",
    2: "ERROR ELECTRICITY METER", 3: "RIPPLE CONTROL RECEIVER",
    4: "INITIAL CHARGE", 5: "MAINTENANCE CHARGE", 6: "MAINTENANCE READY",
    7: "MAINTENANCE REQUIRED", 8: "MAN. SAFETY CHARGE",
    9: "SAFETY CHARGE READY", 10: "FULL CHARGE", 11: "EQUALIZATION: CHARGE",
    12: "DESULFATATION: CHARGE", 13: "BATTERY FULL", 14: "CHARGE",
    15: "BATTERY EMPTY", 16: "DISCHARGE", 17: "PV + DISCHARGE",
    18: "GRID + DISCHARGE", 19: "PASSIVE", 20: "OFF", 21: "OWN CONSUMPTION",
    22: "RESTART", 23: "MAN. EQUALIZATION: CHARGE",
    24: "MAN. DESULFATATION: CHARGE", 25: "SAFETY CHARGE",
    26: "BATTERY PROTECTION MODE", 27: "EG ERROR", 28: "EG CHARGE",
    29: "EG DISCHARGE", 30: "EG PASSIVE", 31: "EG PROHIBIT CHARGE",
    32: "EG PROHIBIT DISCHARGE", 33: "EMERGENCY CHARGE", 34: "SOFTWARE UPDATE",
    35: "NSP ERROR", 36: "NSP ERROR: GRID", 37: "NSP ERROR: HARDWARE",
    38: "NO SERVER CONNECTION", 39: "BMS ERROR", 40: "MAINTENANCE: FILTER",
    41: "SLEEPING MODE", 42: "WAITING EXCESS", 43: "CAPACITY TEST: CHARGE",
    44: "CAPACITY TEST: DISCHARGE", 45: "MAN. DESULFATATION: WAIT",
    46: "MAN. DESULFATATION: READY", 47: "MAN. DESULFATATION: ERROR",
    48: "EQUALIZATION: WAIT", 49: "EMERGENCY CHARGE: ERROR",
    50: "MAN. EQUALIZATION: WAIT", 51: "MAN. EQUALIZATION: ERROR",
    52: "MAN. EQUALIZATION: READY", 53: "AUTO. DESULFATATION: WAIT",
    54: "ABSORPTION PHASE", 55: "DC-SWITCH OFF", 56: "PEAK-SHAVING: WAIT",
    57: "ERROR BATTERY INVERTER", 58: "NPU-ERROR", 59: "BMS OFFLINE",
    60: "MAINTENANCE CHARGE ERROR", 61: "MAN. SAFETY CHARGE ERROR",
    62: "SAFETY CHARGE ERROR", 63: "NO CONNECTION TO MASTER",
    64: "LITHIUM SAFE MODE ACTIVE", 65: "LITHIUM SAFE MODE DONE",
    66: "BATTERY VOLTAGE ERROR", 67: "BMS DC SWITCHED OFF",
    68: "GRID INITIALIZATION", 69: "GRID STABILIZATION",
    70: "REMOTE SHUTDOWN", 71: "OFFPEAK-CHARGE", 72: "ERROR HALFBRIDGE",
    73: "BMS: ERROR OPERATING TEMPERATURE", 74: "FACTORY SETTINGS NOT FOUND",
    75: "BACKUP POWER MODE - ACTIVE", 76: "BACKUP POWER MODE - BATTERY EMPTY",
    77: "BACKUP POWER MODE ERROR", 78: "INITIALISING", 79: "INSTALLATION MODE",
    80: "GRID OFFLINE", 81: "BMS UPDATE NEEDED", 82: "BMS CONFIGURATION NEEDED",
    83: "INSULATION TEST", 84: "SELFTEST", 85: "EXTERNAL CONTROL",
    86: "ERROR: TEMPERATURE SENSOR", 87: "GRID OPERATOR: CHARGE PROHIBITED",
    88: "GRID OPERATOR: DISCHARGE PROHIBITED", 89: "SPARE CAPACITY",
    90: "SELFTEST ERROR", 91: "EARTH FAULT", 92: "PV-MODE",
    93: "REMOTE DISCONNECTION", 94: "ERROR DRM0", 95: "BATTERY DIAGNOSIS",
}


class SenecError(Exception):
    """A SENEC lala.cgi request failed."""


def decode_senec_value(raw: object) -> object:
    """Decode one type-tagged SENEC value, or a list of them.

    ``fl_`` -> float, ``st_``/``ch_`` -> str, ``i*_`` -> signed int,
    ``u*_`` -> unsigned int (payload is hex). ``VARIABLE_NOT_FOUND`` and
    anything unrecognised -> ``None``.
    """
    if isinstance(raw, list):
        return [decode_senec_value(v) for v in raw]
    if not isinstance(raw, str) or "_" not in raw:
        return None
    prefix, _, payload = raw.partition("_")
    if prefix == "fl":
        try:
            return struct.unpack(">f", bytes.fromhex(payload))[0]
        except (ValueError, struct.error):
            return None
    if prefix in ("st", "ch"):
        return payload
    if prefix[:1] in ("i", "u"):
        try:
            value = int(payload, 16)
        except ValueError:
            return None
        if prefix[0] == "i":  # two's-complement signed
            bits = len(payload) * 4
            if bits and value >= (1 << (bits - 1)):
                value -= 1 << bits
        return value
    return None


def _num(value: object) -> float:
    """Coerce a decoded SENEC value to float, treating None/garbage as 0."""
    return float(value) if isinstance(value, (int, float)) else 0.0


async def senec_request(
    session: aiohttp.ClientSession,
    host: str,
    body: dict,
    timeout: float = POLL_TIMEOUT_S,
) -> dict:
    """POST a lala.cgi request and return the raw (still type-tagged) tree.

    SENEC ships a self-signed certificate, so TLS verification is off.
    """
    url = f"https://{host}/lala.cgi"
    async with session.post(
        url,
        json=body,
        ssl=False,
        timeout=aiohttp.ClientTimeout(total=timeout),
    ) as resp:
        if resp.status != 200:
            raise SenecError(f"HTTP {resp.status}")
        return await resp.json(content_type=None)


class SenecPlugin(InverterPlugin):
    """Monitoring plugin for SENEC.Home appliances via the lala.cgi API."""

    def __init__(
        self,
        host: str,
        name: str = "",
        rated_power: int = 0,
        data_dir: str | None = None,
        device_id: str = "",
    ):
        self._host = host
        self._name = name or f"SENEC {host}"
        self._rated_power = rated_power
        self._device_id = device_id
        self._session: aiohttp.ClientSession | None = None
        # Locally integrated lifetime AC energy (Wh). SENEC firmware no
        # longer exposes a counter, so we accumulate and persist it.
        self._energy_wh: float = 0.0
        self._energy_path = (
            os.path.join(data_dir, "senec_energy.json") if data_dir else None
        )
        self._last_energy_ts: float | None = None
        self._last_energy_save: float = 0.0
        # Identity — learned once, then surfaced + persisted by the registry.
        self.serial: str = ""
        self.firmware: str = ""
        # Live caches surfaced to the dashboard JSON snapshot.
        self.dc_channels: list[dict] = []
        self.senec_status: dict = {
            "reachable": False,
            "producing": False,
            "pv_power_w": 0.0,
            "soc_pct": 0.0,
            "battery_power_w": 0.0,
            "battery_voltage_v": 0.0,
            "battery_temp_c": None,
            "house_power_w": 0.0,
            "grid_power_w": 0.0,
            "case_temp_c": None,
            "mcu_temp_c": None,
            "state_code": None,
            "state_name": "",
            "hours_of_operation": 0,
            "update_available": False,
        }

    async def connect(self) -> None:
        self._session = aiohttp.ClientSession()
        self._load_energy()
        # One-shot identity fetch — non-fatal if it fails.
        try:
            raw = await senec_request(self._session, self._host, SENEC_IDENTITY_REQUEST)
            dev_id = decode_senec_value(raw.get("FACTORY", {}).get("DEVICE_ID"))
            if dev_id:
                self.serial = str(dev_id)
            npu = decode_senec_value(raw.get("SYS_UPDATE", {}).get("NPU_VER"))
            if isinstance(npu, int):
                self.firmware = f"NPU v{npu}"
            upd = decode_senec_value(raw.get("SYS_UPDATE", {}).get("UPDATE_AVAILABLE"))
            self.senec_status["update_available"] = bool(upd)
        except Exception as e:
            log.debug("senec_identity_fetch_failed", host=self._host, error=str(e))
        log.info(
            "senec_plugin_connected",
            host=self._host,
            name=self._name,
            serial=self.serial,
        )

    async def poll(self) -> PollResult:
        if self._session is None:
            return PollResult([], [], success=False, error="Not connected")

        try:
            raw = await senec_request(self._session, self._host, SENEC_POLL_REQUEST)
        except Exception as e:
            self.senec_status["reachable"] = False
            log.warning("senec_poll_failed", host=self._host, error=str(e))
            return PollResult([], [], success=False, error=str(e))

        energy = raw.get("ENERGY", {}) or {}
        pv = raw.get("PV1", {}) or {}
        meter = raw.get("PM1OBJ1", {}) or {}
        temps = raw.get("TEMPMEASURE", {}) or {}

        pv_power_w = _num(decode_senec_value(energy.get("GUI_INVERTER_POWER")))
        house_w = _num(decode_senec_value(energy.get("GUI_HOUSE_POW")))
        grid_w = _num(decode_senec_value(energy.get("GUI_GRID_POW")))
        bat_power_w = _num(decode_senec_value(energy.get("GUI_BAT_DATA_POWER")))
        soc = _num(decode_senec_value(energy.get("GUI_BAT_DATA_FUEL_CHARGE")))
        bat_v = _num(decode_senec_value(energy.get("GUI_BAT_DATA_VOLTAGE")))
        state_code = decode_senec_value(energy.get("STAT_STATE"))
        hours = decode_senec_value(energy.get("STAT_HOURS_OF_OPERATION"))

        mpp_power = decode_senec_value(pv.get("MPP_POWER")) or []
        mpp_vol = decode_senec_value(pv.get("MPP_VOL")) or []
        mpp_cur = decode_senec_value(pv.get("MPP_CUR")) or []

        u_ac = [v for v in (decode_senec_value(meter.get("U_AC")) or []) if _num(v) > 0]
        freq = _num(decode_senec_value(meter.get("FREQ")))
        case_temp = _num(decode_senec_value(temps.get("CASE_TEMP")))
        mcu_temp = _num(decode_senec_value(temps.get("MCU_TEMP")))
        bat_temp = decode_senec_value(temps.get("BATTERY_TEMP"))

        # Per-string DC channels.
        dc_channels: list[dict] = []
        dc_power = dc_current = dc_volt_power = dc_power_sum = 0.0
        for i in range(max(len(mpp_power), len(mpp_vol), len(mpp_cur))):
            p = _num(mpp_power[i]) if i < len(mpp_power) else 0.0
            v = _num(mpp_vol[i]) if i < len(mpp_vol) else 0.0
            a = _num(mpp_cur[i]) if i < len(mpp_cur) else 0.0
            dc_power += p
            dc_current += a
            dc_volt_power += v * p
            dc_power_sum += p
            dc_channels.append({
                "name": f"PV{i + 1}",
                "voltage_v": round(v, 1),
                "current_a": round(a, 2),
                "power_w": round(p, 1),
            })
        self.dc_channels = dc_channels
        dc_voltage = (dc_volt_power / dc_power_sum) if dc_power_sum > 0 else 0.0

        # AC side. SENEC does not expose the inverter's own AC current, so
        # derive it from PV power and the grid-meter voltage.
        ac_voltage = (sum(u_ac) / len(u_ac)) if u_ac else 0.0
        ac_current = (pv_power_w / ac_voltage) if ac_voltage > 0 else 0.0

        # Integrate lifetime AC energy.
        now = time.monotonic()
        if self._last_energy_ts is not None:
            dt = now - self._last_energy_ts
            if 0 < dt < MAX_ENERGY_GAP_S:
                self._energy_wh += pv_power_w * dt / 3600.0
        self._last_energy_ts = now
        self._maybe_save_energy()

        producing = pv_power_w > 1.0
        status_code = 4 if producing else 2  # 4 = MPPT, 2 = SLEEPING

        bat_temp_c = None
        if isinstance(bat_temp, (int, float)) and bat_temp > -100:
            bat_temp_c = round(float(bat_temp), 1)

        self.senec_status.update({
            "reachable": True,
            "producing": producing,
            "pv_power_w": round(pv_power_w),
            "soc_pct": round(soc, 1),
            "battery_power_w": round(bat_power_w),
            "battery_voltage_v": round(bat_v, 1),
            "battery_temp_c": bat_temp_c,
            "house_power_w": round(house_w),
            "grid_power_w": round(grid_w),
            "case_temp_c": round(case_temp, 1) if case_temp else None,
            "mcu_temp_c": round(mcu_temp, 1) if mcu_temp else None,
            "state_code": state_code if isinstance(state_code, int) else None,
            "state_name": (
                SENEC_STATE_NAMES.get(state_code, f"STATE {state_code}")
                if isinstance(state_code, int) else ""
            ),
            "hours_of_operation": int(hours) if isinstance(hours, int) else 0,
        })

        inverter_regs = self._encode_model_103(
            ac_power_w=pv_power_w,
            ac_voltage_v=ac_voltage,
            ac_current_a=ac_current,
            ac_freq_hz=freq,
            dc_power_w=dc_power,
            dc_voltage_v=dc_voltage,
            dc_current_a=dc_current,
            temperature_c=case_temp,
            heatsink_temp_c=mcu_temp,
            energy_total_wh=self._energy_wh,
            status_code=status_code,
        )
        common_regs = self._build_common_registers()
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
        heatsink_temp_c: float,
        energy_total_wh: float,
        status_code: int,
    ) -> list[int]:
        regs = [0] * 52
        regs[0] = 103
        regs[1] = 50

        # AC Current (offset 2-6), SF=-2
        ac_i_x100 = int(round(abs(ac_current_a) * 100))
        regs[2] = ac_i_x100 & 0xFFFF
        regs[3] = ac_i_x100 & 0xFFFF
        regs[6] = _int16_as_uint16(-2)

        # AC Voltage AN (offset 10), SF=-1
        regs[10] = int(round(ac_voltage_v * 10)) & 0xFFFF
        regs[13] = _int16_as_uint16(-1)

        # AC Power (offset 14-15), SF=0
        regs[14] = int(round(abs(ac_power_w))) & 0xFFFF
        regs[15] = 0

        # AC Frequency (offset 16-17), SF=-2
        regs[16] = int(round(ac_freq_hz * 100)) & 0xFFFF
        regs[17] = _int16_as_uint16(-2)

        # AC Energy total Wh (acc32, offset 24-26), SF=0
        e = max(0, int(energy_total_wh))
        regs[24] = (e >> 16) & 0xFFFF
        regs[25] = e & 0xFFFF
        regs[26] = 0

        # DC Current (offset 27-28), SF=-2
        regs[27] = int(round(abs(dc_current_a) * 100)) & 0xFFFF
        regs[28] = _int16_as_uint16(-2)

        # DC Voltage (offset 29-30), SF=-1
        regs[29] = int(round(dc_voltage_v * 10)) & 0xFFFF
        regs[30] = _int16_as_uint16(-1)

        # DC Power (offset 31-32), SF=0
        regs[31] = int(round(abs(dc_power_w))) & 0xFFFF
        regs[32] = 0

        # Cabinet (CASE_TEMP) + Heatsink (MCU_TEMP) temperatures
        # (offset 33-37), SF=-1
        regs[33] = _int16_as_uint16(int(round(temperature_c * 10)))
        regs[34] = _int16_as_uint16(int(round(heatsink_temp_c * 10)))
        regs[37] = _int16_as_uint16(-1)

        # Operating state
        regs[38] = status_code
        return regs

    def _build_common_registers(self) -> list[int]:
        regs = [0] * 67
        regs[0] = COMMON_DID
        regs[1] = COMMON_LENGTH
        regs[2:18] = encode_string("SENEC", 16)
        regs[18:34] = encode_string(self._name, 16)
        regs[50:66] = encode_string(self.serial, 16)
        regs[66] = PROXY_UNIT_ID
        return regs

    def get_static_common_overrides(self) -> dict[int, int]:
        ov: dict[int, int] = {0: COMMON_DID, 1: COMMON_LENGTH}
        for i, val in enumerate(encode_string("SENEC", 16)):
            ov[2 + i] = val
        for i, val in enumerate(encode_string(self._name, 16)):
            ov[18 + i] = val
        ov[66] = PROXY_UNIT_ID
        return ov

    def get_model_120_registers(self) -> list[int]:
        regs = [0] * 28
        regs[0] = NAMEPLATE_DID
        regs[1] = NAMEPLATE_LENGTH
        regs[2] = 4  # DERTyp = PV
        regs[3] = self._rated_power & 0xFFFF
        regs[4] = 0
        return regs

    async def write_power_limit(
        self,
        enable: bool,
        limit_pct: float,
        *,
        force: bool = False,
    ) -> WriteResult:
        # No-op: SENEC has no PV-curtailment control over the local API.
        #
        # Researched 2026-05-21 (pysenec, marq24/ha-senec-v3, evcc):
        #  - PV1.POWER_RATIO (+_L1/_L2/_L3) reads a constant 100 % — it is
        #    inverter telemetry, not a setpoint. ha-senec-v3, the most
        #    complete integration, explicitly does NOT use POWER_RATIO.
        #  - lala.cgi LOCAL-WRITE only covers sockets / wallbox / safe-
        #    charge — there is no PV power-limit field.
        #  - SENEC export limiting ("peak shaving") exists only via the
        #    SENEC *cloud* web API at daily granularity — unusable as a
        #    real-time curtailment control.
        # Conclusion: SENEC stays monitoring-only (throttle mode = none).
        return WriteResult(success=True)

    async def get_inverter_status(self) -> dict:
        return dict(self.senec_status)

    async def reconfigure(self, host: str, port: int, unit_id: int) -> None:
        # port / unit_id are Modbus concepts; only host matters here.
        if host == self._host and self._session is not None:
            return
        await self.close()
        self._host = host
        await self.connect()

    @property
    def throttle_capabilities(self) -> ThrottleCaps:
        # Monitoring only — no curtailment path, so it never enters the
        # throttle waterfall (score 0).
        return ThrottleCaps(
            mode="none",
            response_time_s=0.0,
            cooldown_s=0.0,
            startup_delay_s=0.0,
        )

    async def close(self) -> None:
        self._save_energy()
        if self._session is not None and not self._session.closed:
            await self._session.close()
            log.info("senec_plugin_closed", host=self._host)
        self._session = None

    # ----- local energy persistence -------------------------------------

    def _load_energy(self) -> None:
        if not self._energy_path or not self._device_id:
            return
        try:
            with open(self._energy_path) as f:
                data = json.load(f)
            val = data.get(self._device_id) if isinstance(data, dict) else None
            if isinstance(val, (int, float)) and val >= 0:
                self._energy_wh = float(val)
                log.info(
                    "senec_energy_restored",
                    device_id=self._device_id,
                    energy_kwh=round(self._energy_wh / 1000.0, 1),
                )
        except (OSError, ValueError):
            pass

    def _maybe_save_energy(self) -> None:
        now = time.monotonic()
        if now - self._last_energy_save >= ENERGY_SAVE_INTERVAL_S:
            self._last_energy_save = now
            self._save_energy()

    def _save_energy(self) -> None:
        if not self._energy_path or not self._device_id:
            return
        try:
            data: dict = {}
            try:
                with open(self._energy_path) as f:
                    loaded = json.load(f)
                    if isinstance(loaded, dict):
                        data = loaded
            except (OSError, ValueError):
                data = {}
            data[self._device_id] = round(self._energy_wh, 1)
            fd, tmp = tempfile.mkstemp(
                dir=os.path.dirname(self._energy_path), suffix=".json"
            )
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)
            os.replace(tmp, self._energy_path)
        except OSError as e:
            log.debug("senec_energy_save_failed", error=str(e))


async def probe_senec(host: str, timeout: float = 6.0) -> dict | None:
    """Probe one host for a SENEC appliance.

    Returns a dict with identity + a live PV reading on success, or
    ``None`` if the host does not answer the lala.cgi API.
    """
    try:
        async with aiohttp.ClientSession() as session:
            raw = await senec_request(
                session,
                host,
                {
                    "ENERGY": {"GUI_INVERTER_POWER": "", "STAT_STATE": ""},
                    "FACTORY": {"DEVICE_ID": "", "SYS_TYPE": ""},
                },
                timeout=timeout,
            )
    except Exception:
        return None

    energy = raw.get("ENERGY", {}) or {}
    if "GUI_INVERTER_POWER" not in energy:
        return None
    pv_power = decode_senec_value(energy.get("GUI_INVERTER_POWER"))
    if pv_power is None:
        return None
    dev_id = decode_senec_value(raw.get("FACTORY", {}).get("DEVICE_ID"))
    return {
        "host": host,
        "serial": str(dev_id) if dev_id else "",
        "pv_power_w": round(_num(pv_power)),
        "producing": _num(pv_power) > 1.0,
    }


async def discover_senec_devices(
    subnet_override: str | None = None,
    skip_ips: set[str] | None = None,
    tcp_timeout: float = 0.6,
    probe_timeout: float = 6.0,
    concurrency: int = 40,
) -> list[dict]:
    """Scan a subnet for SENEC appliances.

    Phase 1 — concurrent TCP probe of port 443 across the subnet.
    Phase 2 — lala.cgi probe of each open host to confirm it is a SENEC.

    ``subnet_override`` accepts CIDR / "x"-shorthand / single-IP just
    like the SunSpec scanner, so VPN-reachable remote networks work too.
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

    sem = asyncio.Semaphore(concurrency)

    async def _sweep(ip: str) -> tuple[str, bool]:
        async with sem:
            return ip, await _probe_port(ip, 443, tcp_timeout)

    swept = await asyncio.gather(*(_sweep(ip) for ip in hosts))
    open_hosts = [ip for ip, is_open in swept if is_open]

    log.info(
        "senec_discover_sweep",
        subnet=str(subnet),
        scanned=len(hosts),
        open_443=len(open_hosts),
    )

    found: list[dict] = []
    for ip in open_hosts:
        result = await probe_senec(ip, timeout=probe_timeout)
        if result is not None:
            result["already_added"] = ip in skip
            found.append(result)

    return found
