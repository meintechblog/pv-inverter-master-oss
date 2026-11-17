"""Tests for the SENEC.Home plugin (lala.cgi local JSON API).

Covers:
- SENEC-01: SenecPlugin implements the InverterPlugin ABC
- SENEC-02: decode_senec_value handles every type prefix
- SENEC-03: poll() returns a PollResult with SunSpec Model 103 registers
- SENEC-04: per-string DC channels are surfaced
- SENEC-05: missing fields (VARIABLE_NOT_FOUND) degrade to 0, no crash
- SENEC-06: lifetime AC energy is integrated over time
- SENEC-07: integrated energy persists across plugin instances
- SENEC-08: monitoring-only — throttle mode is "none"
"""
from __future__ import annotations

import struct
from unittest.mock import AsyncMock, patch

from pv_inverter_master.plugin import InverterPlugin, PollResult
from pv_inverter_master.plugins.senec import (
    SenecPlugin,
    decode_senec_value,
)


def _fl(value: float) -> str:
    """Encode a float the way SENEC's lala.cgi does (fl_<8 hex>)."""
    return "fl_" + struct.pack(">f", value).hex().upper()


# A realistic lala.cgi response with clean round values for stable asserts.
SENEC_RESPONSE = {
    "ENERGY": {
        "GUI_INVERTER_POWER": _fl(3000.0),
        "GUI_HOUSE_POW": _fl(2000.0),
        "GUI_GRID_POW": _fl(-500.0),
        "GUI_BAT_DATA_POWER": _fl(0.0),
        "GUI_BAT_DATA_FUEL_CHARGE": _fl(0.0),
        "GUI_BAT_DATA_VOLTAGE": _fl(0.0),
        "GUI_BAT_DATA_CURRENT": _fl(0.0),
        "STAT_STATE": "u8_46",
        "STAT_HOURS_OF_OPERATION": "u3_0000C6A9",
    },
    "PV1": {
        "MPP_POWER": [_fl(1500.0), _fl(1000.0), _fl(500.0)],
        "MPP_VOL": [_fl(300.0), _fl(300.0), _fl(300.0)],
        "MPP_CUR": [_fl(5.0), _fl(3.0), _fl(2.0)],
    },
    "PM1OBJ1": {
        "U_AC": [_fl(230.0), _fl(230.0), _fl(230.0)],
        "I_AC": [_fl(4.0), _fl(4.0), _fl(4.0)],
        "P_AC": [_fl(-160.0), _fl(-170.0), _fl(-170.0)],
        "FREQ": _fl(50.0),
    },
    "TEMPMEASURE": {
        "CASE_TEMP": _fl(35.0),
        "MCU_TEMP": _fl(48.0),
        "BATTERY_TEMP": _fl(-126.0),
    },
    "FACTORY": {"DEVICE_ID": "st_21627161095585799540095811", "SYS_TYPE": "u8_12"},
    "SYS_UPDATE": {"NPU_VER": "u1_000A", "UPDATE_AVAILABLE": "u8_00"},
}

ALL_MISSING = {
    "ENERGY": {k: "VARIABLE_NOT_FOUND" for k in SENEC_RESPONSE["ENERGY"]},
    "PV1": {k: "VARIABLE_NOT_FOUND" for k in SENEC_RESPONSE["PV1"]},
    "PM1OBJ1": {k: "VARIABLE_NOT_FOUND" for k in SENEC_RESPONSE["PM1OBJ1"]},
    "TEMPMEASURE": {k: "VARIABLE_NOT_FOUND" for k in SENEC_RESPONSE["TEMPMEASURE"]},
}


# --- SENEC-02: value decoding ----------------------------------------

class TestDecodeSenecValue:
    def test_float(self):
        assert abs(decode_senec_value(_fl(2918.9)) - 2918.9) < 0.1

    def test_negative_float(self):
        assert decode_senec_value(_fl(-500.0)) == -500.0

    def test_uint8(self):
        assert decode_senec_value("u8_46") == 70

    def test_uint32_hex(self):
        assert decode_senec_value("u3_0000C6A9") == 50857

    def test_signed_int_negative(self):
        # i1_FFFF -> two's complement 16-bit -> -1
        assert decode_senec_value("i1_FFFF") == -1

    def test_signed_int_positive(self):
        assert decode_senec_value("i1_0064") == 100

    def test_string(self):
        assert decode_senec_value("st_ABC123") == "ABC123"

    def test_variable_not_found(self):
        assert decode_senec_value("VARIABLE_NOT_FOUND") is None

    def test_garbage(self):
        assert decode_senec_value("not-a-value") is None
        assert decode_senec_value(None) is None
        assert decode_senec_value(123) is None

    def test_list(self):
        decoded = decode_senec_value([_fl(1.0), _fl(2.0), _fl(3.0)])
        assert decoded == [1.0, 2.0, 3.0]


# --- SENEC-01 / SENEC-08: ABC + throttle -----------------------------

class TestPluginContract:
    def test_implements_abc(self):
        assert issubclass(SenecPlugin, InverterPlugin)

    def test_all_abstract_methods_present(self):
        plugin = SenecPlugin(host="203.0.113.101")
        for method in (
            "connect", "poll", "get_static_common_overrides",
            "get_model_120_registers", "write_power_limit",
            "reconfigure", "close",
        ):
            assert hasattr(plugin, method)

    def test_throttle_mode_is_none(self):
        # Monitoring only — SENEC must never enter the curtailment waterfall.
        caps = SenecPlugin(host="203.0.113.101").throttle_capabilities
        assert caps.mode == "none"

    def test_model_120_is_pv(self):
        regs = SenecPlugin(host="x", rated_power=10000).get_model_120_registers()
        assert regs[0] == 120
        assert regs[2] == 4        # DERTyp = PV
        assert regs[3] == 10000    # WRtg


# --- SENEC-03 / 04 / 05: poll + register encoding --------------------

class TestPoll:
    async def test_poll_produces_registers(self):
        plugin = SenecPlugin(host="203.0.113.101", name="Test SENEC")
        plugin._session = AsyncMock()
        with patch(
            "pv_inverter_master.plugins.senec.senec_request",
            new=AsyncMock(return_value=SENEC_RESPONSE),
        ):
            result = await plugin.poll()

        assert isinstance(result, PollResult)
        assert result.success is True
        assert len(result.common_registers) == 67
        assert len(result.inverter_registers) == 52
        ir = result.inverter_registers
        assert ir[0] == 103 and ir[1] == 50
        assert ir[14] == 3000          # AC power = GUI_INVERTER_POWER
        assert ir[16] == 5000          # AC frequency 50.00 Hz, SF -2
        assert ir[31] == 3000          # DC power = sum of MPP_POWER
        assert ir[33] == 350           # cabinet temp = CASE_TEMP 35.0, SF -1
        assert ir[34] == 480           # heat-sink temp = MCU_TEMP 48.0, SF -1
        assert ir[38] == 4             # status MPPT (producing)
        assert plugin.senec_status["case_temp_c"] == 35.0
        assert plugin.senec_status["mcu_temp_c"] == 48.0

    async def test_poll_surfaces_dc_channels(self):
        plugin = SenecPlugin(host="203.0.113.101")
        plugin._session = AsyncMock()
        with patch(
            "pv_inverter_master.plugins.senec.senec_request",
            new=AsyncMock(return_value=SENEC_RESPONSE),
        ):
            await plugin.poll()

        assert len(plugin.dc_channels) == 3
        assert plugin.dc_channels[0]["name"] == "PV1"
        assert plugin.dc_channels[0]["power_w"] == 1500.0
        assert plugin.senec_status["state_name"] == "REMOTE SHUTDOWN"
        assert plugin.senec_status["pv_power_w"] == 3000
        # Battery temp -126 is the SENEC "no battery" sentinel -> None.
        assert plugin.senec_status["battery_temp_c"] is None

    async def test_poll_missing_fields_no_crash(self):
        plugin = SenecPlugin(host="203.0.113.101")
        plugin._session = AsyncMock()
        with patch(
            "pv_inverter_master.plugins.senec.senec_request",
            new=AsyncMock(return_value=ALL_MISSING),
        ):
            result = await plugin.poll()

        assert result.success is True
        assert result.inverter_registers[14] == 0   # AC power degrades to 0
        assert result.inverter_registers[38] == 2   # status SLEEPING

    async def test_poll_request_failure_returns_unsuccessful(self):
        plugin = SenecPlugin(host="203.0.113.101")
        plugin._session = AsyncMock()
        with patch(
            "pv_inverter_master.plugins.senec.senec_request",
            new=AsyncMock(side_effect=OSError("connection refused")),
        ):
            result = await plugin.poll()

        assert result.success is False
        assert plugin.senec_status["reachable"] is False

    def test_encode_model_103_shape(self):
        plugin = SenecPlugin(host="x")
        regs = plugin._encode_model_103(
            ac_power_w=1234.0, ac_voltage_v=230.0, ac_current_a=5.4,
            ac_freq_hz=50.0, dc_power_w=1250.0, dc_voltage_v=300.0,
            dc_current_a=4.2, temperature_c=30.0, heatsink_temp_c=48.0,
            energy_total_wh=123456.0, status_code=4,
        )
        assert len(regs) == 52
        assert regs[14] == 1234
        # acc32 energy split across regs[24]/[25]
        assert (regs[24] << 16 | regs[25]) == 123456
        # cabinet temp -> regs[33], heat-sink temp -> regs[34], SF=-1
        assert regs[33] == 300
        assert regs[34] == 480


# --- SENEC-06 / 07: energy integration + persistence -----------------

class TestEnergy:
    async def test_energy_integration_accumulates(self):
        plugin = SenecPlugin(host="203.0.113.101")
        plugin._session = AsyncMock()
        # Two polls 300 s apart at a steady 3000 W -> +250 Wh. poll()
        # reads monotonic() twice (integration + save throttle), so the
        # first poll sits at t=1000 and the second at t=1300. The gap
        # must stay under MAX_ENERGY_GAP_S (600 s) to be integrated.
        ticks = [1000.0, 1000.0, 1300.0, 1300.0]

        def _clock():
            return ticks.pop(0) if ticks else 1300.0

        with patch(
            "pv_inverter_master.plugins.senec.senec_request",
            new=AsyncMock(return_value=SENEC_RESPONSE),
        ), patch(
            "pv_inverter_master.plugins.senec.time.monotonic",
            side_effect=_clock,
        ):
            await plugin.poll()   # establishes the baseline timestamp
            await plugin.poll()   # 300 s later

        assert abs(plugin._energy_wh - 250.0) < 1.0

    async def test_energy_persists_across_instances(self, tmp_path):
        data_dir = str(tmp_path)
        first = SenecPlugin(
            host="203.0.113.101", data_dir=data_dir, device_id="dev1",
        )
        first._energy_wh = 42000.0
        first._save_energy()

        second = SenecPlugin(
            host="203.0.113.101", data_dir=data_dir, device_id="dev1",
        )
        second._load_energy()
        assert abs(second._energy_wh - 42000.0) < 0.1

    async def test_energy_isolated_per_device(self, tmp_path):
        data_dir = str(tmp_path)
        a = SenecPlugin(host="x", data_dir=data_dir, device_id="A")
        a._energy_wh = 100.0
        a._save_energy()
        b = SenecPlugin(host="y", data_dir=data_dir, device_id="B")
        b._energy_wh = 999.0
        b._save_energy()

        reloaded = SenecPlugin(host="x", data_dir=data_dir, device_id="A")
        reloaded._load_energy()
        assert abs(reloaded._energy_wh - 100.0) < 0.1
