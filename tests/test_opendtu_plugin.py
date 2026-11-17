"""Tests for the OpenDTU plugin (Hoymiles micro-inverters via REST API).

Covers requirements DTU-01 through DTU-05:
- DTU-01: Poll OpenDTU REST API, translate JSON to SunSpec registers
- DTU-02: Serial-based filtering for multi-inverter gateways
- DTU-03: Power limit via POST /api/limit/config with Basic Auth
- DTU-04: Implements all InverterPlugin ABC methods
- DTU-05: Dead-time guard suppresses re-sends for 30s
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import aiohttp
import pytest

from pv_inverter_master.plugins.opendtu import (
    OpenDTUPlugin,
    _GATEWAY_CACHE,
    _GATEWAY_CACHE_TTL_S,
    MIN_POLL_INTERVAL_S,
    STALE_THRESHOLD_S,
)
from pv_inverter_master.plugin import InverterPlugin, PollResult, WriteResult
from pv_inverter_master.config import GatewayConfig
from pv_inverter_master.sunspec_models import _int16_as_uint16


@pytest.fixture(autouse=True)
def _clear_gateway_cache():
    """Per-gateway HTTP cache is module-level — clear between tests."""
    _GATEWAY_CACHE.clear()
    yield
    _GATEWAY_CACHE.clear()


# --- Sample OpenDTU API response ---

SAMPLE_LIVEDATA = {
    "inverters": [
        {
            "serial": "112100000001",
            "name": "Shed",
            "data_age": 4,
            "reachable": True,
            "producing": True,
            "limit_relative": 100.0,
            "limit_absolute": 400.0,
            "AC": {"0": {
                "Power": {"v": 285.3, "u": "W", "d": 1},
                "Voltage": {"v": 230.1, "u": "V", "d": 1},
                "Current": {"v": 1.24, "u": "A", "d": 2},
                "Frequency": {"v": 50.01, "u": "Hz", "d": 2},
            }},
            "DC": {
                "0": {
                    "Power": {"v": 145.2, "u": "W", "d": 1},
                    "Voltage": {"v": 32.1, "u": "V", "d": 1},
                    "Current": {"v": 4.52, "u": "A", "d": 2},
                    "YieldTotal": {"v": 1234.567, "u": "kWh", "d": 3},
                    "YieldDay": {"v": 1.23, "u": "kWh", "d": 3},
                },
                "1": {
                    "Power": {"v": 140.0, "u": "W", "d": 1},
                    "Voltage": {"v": 31.5, "u": "V", "d": 1},
                    "Current": {"v": 4.44, "u": "A", "d": 2},
                    "YieldTotal": {"v": 1100.0, "u": "kWh", "d": 3},
                    "YieldDay": {"v": 1.10, "u": "kWh", "d": 3},
                },
            },
            "INV": {"0": {"Temperature": {"v": 35.2, "u": "\u00b0C", "d": 1}}},
        },
        {
            "serial": "114100000002",
            "name": "Balcony",
            "data_age": 3,
            "reachable": True,
            "producing": True,
            "limit_relative": 100.0,
            "limit_absolute": 600.0,
            "AC": {"0": {
                "Power": {"v": 410.0, "u": "W", "d": 1},
                "Voltage": {"v": 229.8, "u": "V", "d": 1},
                "Current": {"v": 1.78, "u": "A", "d": 2},
                "Frequency": {"v": 50.00, "u": "Hz", "d": 2},
            }},
            "DC": {
                "0": {
                    "Power": {"v": 210.0, "u": "W", "d": 1},
                    "Voltage": {"v": 33.0, "u": "V", "d": 1},
                    "Current": {"v": 6.36, "u": "A", "d": 2},
                    "YieldTotal": {"v": 2500.0, "u": "kWh", "d": 3},
                    "YieldDay": {"v": 2.50, "u": "kWh", "d": 3},
                },
                "1": {
                    "Power": {"v": 200.0, "u": "W", "d": 1},
                    "Voltage": {"v": 32.5, "u": "V", "d": 1},
                    "Current": {"v": 6.15, "u": "A", "d": 2},
                    "YieldTotal": {"v": 2400.0, "u": "kWh", "d": 3},
                    "YieldDay": {"v": 2.40, "u": "kWh", "d": 3},
                },
            },
            "INV": {"0": {"Temperature": {"v": 33.0, "u": "\u00b0C", "d": 1}}},
        },
    ],
    "total": {},
    "hints": {"time_sync": False, "radio_problem": False, "default_password": True},
}


# --- Helpers ---

def _make_gateway(host: str = "192.0.2.98") -> GatewayConfig:
    return GatewayConfig(host=host, user="admin", password="openDTU42")


def _make_plugin(serial: str = "112100000001", name: str = "Shed") -> OpenDTUPlugin:
    return OpenDTUPlugin(
        gateway_config=_make_gateway(),
        serial=serial,
        name=name,
    )


def _mock_session(json_response=None, raise_error=None):
    """Create a mock aiohttp.ClientSession that returns json_response on GET."""
    session = AsyncMock(spec=aiohttp.ClientSession)
    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value=json_response or SAMPLE_LIVEDATA)

    # session.get() returns an async context manager
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=mock_response)
    cm.__aexit__ = AsyncMock(return_value=False)

    if raise_error:
        cm.__aenter__.side_effect = raise_error

    session.get.return_value = cm

    # session.post() returns an async context manager
    post_response = AsyncMock()
    post_response.status = 200
    post_response.json = AsyncMock(return_value={"type": "success"})
    post_cm = AsyncMock()
    post_cm.__aenter__ = AsyncMock(return_value=post_response)
    post_cm.__aexit__ = AsyncMock(return_value=False)
    session.post.return_value = post_cm

    # closed property
    type(session).closed = PropertyMock(return_value=False)

    return session


# --- Tests ---

class TestABCCompliance:
    """DTU-04: Plugin implements all InverterPlugin ABC methods."""

    def test_abc_compliance(self):
        plugin = _make_plugin()
        assert isinstance(plugin, InverterPlugin)

    def test_all_abstract_methods_implemented(self):
        """All 7 abstract methods must be present."""
        plugin = _make_plugin()
        assert hasattr(plugin, "connect")
        assert hasattr(plugin, "poll")
        assert hasattr(plugin, "get_static_common_overrides")
        assert hasattr(plugin, "get_model_120_registers")
        assert hasattr(plugin, "write_power_limit")
        assert hasattr(plugin, "reconfigure")
        assert hasattr(plugin, "close")


class TestPollSuccess:
    """DTU-01: Poll OpenDTU REST API and translate JSON to SunSpec registers."""

    @pytest.mark.asyncio
    async def test_poll_success(self):
        """Given valid JSON for serial 112100000001 producing 285.3W,
        poll() returns PollResult(success=True) with correct AC power register."""
        plugin = _make_plugin()
        plugin._session = _mock_session()

        result = await plugin.poll()

        assert result.success is True
        assert result.error is None
        # Fresh producing frame -> NOT source-stale.
        assert result.stale is False
        assert len(result.common_registers) == 67
        assert len(result.inverter_registers) == 52
        # AC Power at offset 14, SF=0 -> 285W (rounded from 285.3)
        assert result.inverter_registers[14] == 285
        # AC Power SF at offset 15 = 0
        assert result.inverter_registers[15] == 0

    @pytest.mark.asyncio
    async def test_poll_serial_filter(self):
        """DTU-02: Given 2 inverters, plugin for serial A only sees serial A data."""
        plugin = _make_plugin(serial="112100000001")
        plugin._session = _mock_session()

        result = await plugin.poll()

        assert result.success is True
        # Should see 285W (Shed), not 410W (Balcony)
        assert result.inverter_registers[14] == 285

    @pytest.mark.asyncio
    async def test_poll_other_serial(self):
        """Plugin for second serial sees second inverter data."""
        plugin = _make_plugin(serial="114100000002", name="Balcony")
        plugin._session = _mock_session()

        result = await plugin.poll()

        assert result.success is True
        assert result.inverter_registers[14] == 410

    @pytest.mark.asyncio
    async def test_poll_unreachable_inverter(self):
        """Given JSON where target serial has reachable=false, returns success=True (graceful)."""
        unreachable_data = {
            "inverters": [{
                "serial": "112100000001",
                "name": "Shed",
                "reachable": False,
                "producing": False,
                "AC": {"0": {}},
                "DC": {},
                "INV": {"0": {}},
            }],
        }
        plugin = _make_plugin()
        plugin._session = _mock_session(json_response=unreachable_data)

        result = await plugin.poll()

        # Unreachable via radio is not a connection failure — stays "connected"
        assert result.success is True
        assert result.error is None
        # ...but it IS source-stale: no fresh data (DTU<->micro radio gap), so
        # the anomaly detector can tell this apart from a producing-0W inverter.
        assert result.stale is True

    @pytest.mark.asyncio
    async def test_poll_serial_not_found(self):
        """Serial not in response returns success=False."""
        plugin = _make_plugin(serial="999999999999")
        plugin._session = _mock_session()

        result = await plugin.poll()

        assert result.success is False
        assert "999999999999" in result.error

    @pytest.mark.asyncio
    async def test_poll_gateway_offline(self):
        """Given aiohttp.ClientError, poll() returns success=False."""
        plugin = _make_plugin()
        plugin._session = _mock_session(raise_error=aiohttp.ClientError("Connection refused"))

        result = await plugin.poll()

        assert result.success is False
        assert result.error is not None


class TestRegisterEncoding:
    """DTU-01: SunSpec Model 103 register encoding from physical values."""

    @pytest.mark.asyncio
    async def test_register_encoding_ac_current(self):
        """AC current 1.24A with SF=-2 encodes to register value 124."""
        plugin = _make_plugin()
        plugin._session = _mock_session()

        result = await plugin.poll()

        # AC Current at offset 2, SF=-2 -> 1.24 * 100 = 124
        assert result.inverter_registers[2] == 124
        # AC Current L1 at offset 3 (same as total for single-phase)
        assert result.inverter_registers[3] == 124
        # SF at offset 6
        assert result.inverter_registers[6] == _int16_as_uint16(-2)

    @pytest.mark.asyncio
    async def test_register_encoding_ac_voltage(self):
        """AC voltage 230.1V with SF=-1 encodes to register value 2301."""
        plugin = _make_plugin()
        plugin._session = _mock_session()

        result = await plugin.poll()

        # AC Voltage AN at offset 10, SF=-1 -> 230.1 * 10 = 2301
        assert result.inverter_registers[10] == 2301
        # SF at offset 13
        assert result.inverter_registers[13] == _int16_as_uint16(-1)

    @pytest.mark.asyncio
    async def test_register_encoding_dc_channels_summed(self):
        """Two DC channels (145.2W + 140.0W) sum to 285W for DC power register."""
        plugin = _make_plugin()
        plugin._session = _mock_session()

        result = await plugin.poll()

        # DC Power at offset 31, SF=0
        # 145.2 + 140.0 = 285.2 -> rounded to 285
        assert result.inverter_registers[31] == 285
        assert result.inverter_registers[32] == 0  # DC Power SF

    @pytest.mark.asyncio
    async def test_register_encoding_energy_total(self):
        """YieldTotal 1234.567 + 1100.0 kWh = 2334567 Wh as acc32."""
        plugin = _make_plugin()
        plugin._session = _mock_session()

        result = await plugin.poll()

        # Total energy: (1234.567 + 1100.0) * 1000 = 2334567 Wh
        total_wh = 2334567
        high_word = (total_wh >> 16) & 0xFFFF
        low_word = total_wh & 0xFFFF
        assert result.inverter_registers[24] == high_word
        assert result.inverter_registers[25] == low_word
        assert result.inverter_registers[26] == 0  # Energy SF

    @pytest.mark.asyncio
    async def test_register_encoding_frequency(self):
        """AC frequency 50.01Hz with SF=-2 encodes to 5001."""
        plugin = _make_plugin()
        plugin._session = _mock_session()

        result = await plugin.poll()

        assert result.inverter_registers[16] == 5001
        assert result.inverter_registers[17] == _int16_as_uint16(-2)

    @pytest.mark.asyncio
    async def test_register_encoding_temperature(self):
        """Temperature 35.2C with SF=-1 encodes to 352."""
        plugin = _make_plugin()
        plugin._session = _mock_session()

        result = await plugin.poll()

        assert result.inverter_registers[33] == 352
        assert result.inverter_registers[37] == _int16_as_uint16(-1)

    @pytest.mark.asyncio
    async def test_register_encoding_status_producing(self):
        """Producing inverter has status 4 (MPPT)."""
        plugin = _make_plugin()
        plugin._session = _mock_session()

        result = await plugin.poll()

        assert result.inverter_registers[38] == 4  # MPPT

    @pytest.mark.asyncio
    async def test_register_encoding_dc_current_summed(self):
        """DC current summed across channels: 4.52 + 4.44 = 8.96A -> 896."""
        plugin = _make_plugin()
        plugin._session = _mock_session()

        result = await plugin.poll()

        # DC Current at offset 27, SF=-2
        assert result.inverter_registers[27] == 896
        assert result.inverter_registers[28] == _int16_as_uint16(-2)


class TestWritePowerLimit:
    """DTU-03: Power limit via POST /api/limit/config with Basic Auth."""

    @pytest.mark.asyncio
    async def test_write_power_limit_success(self):
        """write_power_limit(True, 50.0) POSTs correct payload."""
        plugin = _make_plugin()
        session = _mock_session()
        plugin._session = session

        result = await plugin.write_power_limit(True, 50.0)

        assert result.success is True
        # Verify POST was called
        session.post.assert_called_once()
        call_kwargs = session.post.call_args
        # Check URL contains /api/limit/config
        assert "/api/limit/config" in call_kwargs[0][0] or "/api/limit/config" in str(call_kwargs)
        # Check form-data body (OpenDTU expects data= with JSON string)
        import json
        form_data = call_kwargs[1].get("data", call_kwargs.kwargs.get("data"))
        posted_json = json.loads(form_data["data"])
        assert posted_json["serial"] == "112100000001"
        assert posted_json["limit_type"] == 257  # Relative %, non-persistent (temporary)
        assert posted_json["limit_value"] == 50.0

    @pytest.mark.asyncio
    async def test_write_power_limit_disabled(self):
        """write_power_limit(False, 100.0) POSTs limit_value=100."""
        plugin = _make_plugin()
        session = _mock_session()
        plugin._session = session

        result = await plugin.write_power_limit(False, 100.0)

        assert result.success is True
        import json
        call_kwargs = session.post.call_args
        form_data = call_kwargs[1].get("data", call_kwargs.kwargs.get("data"))
        posted_json = json.loads(form_data["data"])
        assert posted_json["limit_value"] == 100.0


class TestDeadTimeGuard:
    """DTU-05: Dead-time guard suppresses re-sends for 30s."""

    @pytest.mark.asyncio
    async def test_dead_time_guard(self):
        """After successful write, calling again within 30s returns success without POST."""
        plugin = _make_plugin()
        session = _mock_session()
        plugin._session = session

        # First call should POST
        result1 = await plugin.write_power_limit(True, 50.0)
        assert result1.success is True
        assert session.post.call_count == 1

        # Second call within dead time should NOT POST
        result2 = await plugin.write_power_limit(True, 50.0)
        assert result2.success is True
        assert session.post.call_count == 1  # Still 1, no new POST

    @pytest.mark.asyncio
    async def test_dead_time_guard_expired(self):
        """After 30s+ elapsed, write_power_limit sends new POST."""
        plugin = _make_plugin()
        session = _mock_session()
        plugin._session = session

        # First call
        await plugin.write_power_limit(True, 50.0)
        assert session.post.call_count == 1

        # Simulate time passing beyond dead time
        with patch("pv_inverter_master.plugins.opendtu.time") as mock_time:
            # Set monotonic to return a value 31s after the first call
            mock_time.monotonic.return_value = time.monotonic() + 31.0
            result = await plugin.write_power_limit(True, 60.0)

        assert result.success is True
        assert session.post.call_count == 2  # New POST


class TestSessionLifecycle:
    """Session management and cleanup."""

    @pytest.mark.asyncio
    async def test_close_cleans_session(self):
        """After close(), aiohttp session is closed."""
        plugin = _make_plugin()
        session = _mock_session()
        plugin._session = session

        await plugin.close()

        session.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_connect_creates_session(self):
        """connect() creates an aiohttp.ClientSession."""
        plugin = _make_plugin()

        with patch("pv_inverter_master.plugins.opendtu.aiohttp.ClientSession") as mock_cls:
            await plugin.connect()
            mock_cls.assert_called_once()
            assert plugin._session is not None


class TestCommonRegisters:
    """Common Model register generation."""

    def test_common_registers(self):
        """get_static_common_overrides() includes Hoymiles manufacturer."""
        plugin = _make_plugin()
        overrides = plugin.get_static_common_overrides()

        # Should have DID=1, Length=65
        assert overrides[0] == 1
        assert overrides[1] == 65
        # Manufacturer "Hoymiles" at offset 2-17 (16 registers)
        # First two chars "Ho" = 0x486F
        assert overrides[2] == 0x486F

    def test_model_120_nameplate(self):
        """get_model_120_registers() returns 28 regs with DID=120, WRtg from max_power."""
        plugin = _make_plugin()
        plugin._max_power_w = 400

        regs = plugin.get_model_120_registers()

        assert len(regs) == 28
        assert regs[0] == 120  # DID
        assert regs[1] == 26   # Length
        # WRtg at offset 4 (index 4 since DID=0, Length=1, DERTyp=2, WRtg=3... wait)
        # Per plan: WRtg at offset 4, WRtg_SF at offset 5
        # But DID=idx0, Length=idx1, DERTyp=idx2, WRtg=idx3, WRtg_SF=idx4
        # Plan says offset 4 and 5 -- let's match the plan
        assert regs[3] == 400  # WRtg
        assert regs[4] == 0    # WRtg_SF


class TestReconfigure:
    """Reconfigure is a no-op for OpenDTU but should close the session."""

    @pytest.mark.asyncio
    async def test_reconfigure_closes_session(self):
        """reconfigure() closes the existing session."""
        plugin = _make_plugin()
        session = _mock_session()
        plugin._session = session

        await plugin.reconfigure("192.0.2.99", 80, 1)

        session.close.assert_called_once()


class TestConnectAuthDefaults:
    """Empty user/password must fall back to OpenDTU factory defaults."""

    @pytest.mark.asyncio
    async def test_empty_creds_use_admin_openDTU42(self):
        """Empty gateway_user/password -> BasicAuth('admin', 'openDTU42').

        Regression for: user adds an OpenDTU via the webapp without typing
        the default credentials, plugin sends empty Basic Auth, gateway
        rejects requests on hardened installs.
        """
        gw = GatewayConfig(host="192.0.2.113", user="", password="")
        plugin = OpenDTUPlugin(gateway_config=gw, serial="138200000003")

        with patch("pv_inverter_master.plugins.opendtu.aiohttp.ClientSession") as mock_cls, \
             patch("pv_inverter_master.plugins.opendtu.aiohttp.BasicAuth") as mock_auth:
            await plugin.connect()
            mock_auth.assert_called_once_with("admin", "openDTU42")
            mock_cls.assert_called_once()

    @pytest.mark.asyncio
    async def test_explicit_creds_take_precedence(self):
        """Custom user/password are used as-is when set."""
        gw = GatewayConfig(host="192.0.2.113", user="custom", password="secret")
        plugin = OpenDTUPlugin(gateway_config=gw, serial="138200000003")

        with patch("pv_inverter_master.plugins.opendtu.aiohttp.ClientSession"), \
             patch("pv_inverter_master.plugins.opendtu.aiohttp.BasicAuth") as mock_auth:
            await plugin.connect()
            mock_auth.assert_called_once_with("custom", "secret")

    @pytest.mark.asyncio
    async def test_only_user_blank_falls_back(self):
        """Empty user with set password -> 'admin' + custom password."""
        gw = GatewayConfig(host="192.0.2.113", user="", password="my-pw")
        plugin = OpenDTUPlugin(gateway_config=gw, serial="138200000003")

        with patch("pv_inverter_master.plugins.opendtu.aiohttp.ClientSession"), \
             patch("pv_inverter_master.plugins.opendtu.aiohttp.BasicAuth") as mock_auth:
            await plugin.connect()
            mock_auth.assert_called_once_with("admin", "my-pw")


class TestSerialAutoAdoption:
    """Empty serial + single-inverter gateway -> plugin adopts the gateway's serial."""

    def test_auto_adopts_when_serial_empty_and_one_inverter(self):
        """Plugin with empty serial picks up the only inverter's serial."""
        gw = GatewayConfig(host="192.0.2.113", user="admin", password="openDTU42")
        plugin = OpenDTUPlugin(gateway_config=gw, serial="")

        data = {
            "inverters": [
                {"serial": "138200000003", "name": "Garden", "reachable": True},
            ],
        }
        match = plugin._find_inverter(data)
        assert match is not None
        assert match["serial"] == "138200000003"
        # Adoption persists on the plugin so subsequent polls match by it
        assert plugin.serial == "138200000003"

    def test_no_auto_adopt_when_serial_set(self):
        """Plugin with a specific serial does not adopt a different one."""
        gw = GatewayConfig(host="192.0.2.113", user="admin", password="openDTU42")
        plugin = OpenDTUPlugin(gateway_config=gw, serial="111111111111")

        data = {
            "inverters": [
                {"serial": "138200000003", "name": "Garden", "reachable": True},
            ],
        }
        match = plugin._find_inverter(data)
        assert match is None
        assert plugin.serial == "111111111111"  # unchanged

    def test_no_auto_adopt_when_multiple_inverters(self):
        """Multi-inverter gateway with empty serial returns None — user must pick."""
        gw = GatewayConfig(host="192.0.2.113", user="admin", password="openDTU42")
        plugin = OpenDTUPlugin(gateway_config=gw, serial="")

        data = {
            "inverters": [
                {"serial": "138200000003", "name": "Garden", "reachable": True},
                {"serial": "999999999999", "name": "Other", "reachable": True},
            ],
        }
        match = plugin._find_inverter(data)
        assert match is None
        assert plugin.serial == ""  # unchanged

    def test_no_auto_adopt_when_zero_inverters(self):
        """Empty inverter list with empty serial returns None safely."""
        gw = GatewayConfig(host="192.0.2.113", user="admin", password="openDTU42")
        plugin = OpenDTUPlugin(gateway_config=gw, serial="")

        match = plugin._find_inverter({"inverters": []})
        assert match is None
        assert plugin.serial == ""


class TestMinPollInterval:
    """Plugin declares a floor so the registry's poll loop honours it."""

    def test_min_poll_interval_is_5s(self):
        plugin = _make_plugin()
        assert plugin.min_poll_interval == MIN_POLL_INTERVAL_S
        assert plugin.min_poll_interval == 5.0


class TestDataAgeGate:
    """data_age > STALE_THRESHOLD_S is treated as unreachable."""

    @pytest.mark.asyncio
    async def test_fresh_data_age_passes(self):
        """data_age below threshold → poll returns full registers."""
        data = {
            "inverters": [{
                "serial": "112100000001", "name": "Shed",
                "data_age": 5, "reachable": True, "producing": True,
                "limit_relative": 100, "limit_absolute": 400,
                "AC": {"0": {
                    "Power": {"v": 285.3}, "Voltage": {"v": 230.1},
                    "Current": {"v": 1.24}, "Frequency": {"v": 50.01},
                }},
                "DC": {"0": {"Power": {"v": 145.2}, "Voltage": {"v": 32.1},
                             "Current": {"v": 4.52}, "YieldTotal": {"v": 1.0},
                             "YieldDay": {"v": 0.1}}},
                "INV": {"0": {"Temperature": {"v": 35.0}}},
            }],
        }
        plugin = _make_plugin()
        plugin._session = _mock_session(json_response=data)

        result = await plugin.poll()
        assert result.success is True
        assert len(result.inverter_registers) == 52
        assert result.inverter_registers[14] == 285  # AC power

    @pytest.mark.asyncio
    async def test_stale_data_age_treated_as_unreachable(self):
        """data_age above threshold → empty registers, success=True (graceful)."""
        data = {
            "inverters": [{
                "serial": "112100000001", "name": "Shed",
                "data_age": int(STALE_THRESHOLD_S) + 30,  # well past threshold
                "reachable": True,  # OpenDTU still thinks it's reachable
                "producing": True,
                "limit_relative": 100, "limit_absolute": 400,
                "AC": {"0": {"Power": {"v": 1202.5}}},  # stale wattage
                "DC": {"0": {"Power": {"v": 0}}},
                "INV": {"0": {"Temperature": {"v": 0}}},
            }],
        }
        plugin = _make_plugin()
        plugin._session = _mock_session(json_response=data)

        result = await plugin.poll()
        # Stale frame = empty registers, success=True (same shape as
        # the unreachable path — keeps connection state "connected"
        # but starves the dashboard of phantom wattage).
        assert result.success is True
        assert result.error is None
        assert result.inverter_registers == []
        assert result.common_registers == []
        # A too-old frame is source-stale (radio went silent, frame is phantom).
        assert result.stale is True

    @pytest.mark.asyncio
    async def test_missing_data_age_field_passes(self):
        """Gateways that don't expose data_age (older firmware) keep working."""
        data = {
            "inverters": [{
                "serial": "112100000001", "name": "Shed",
                # no data_age key
                "reachable": True, "producing": True,
                "limit_relative": 100, "limit_absolute": 400,
                "AC": {"0": {
                    "Power": {"v": 100}, "Voltage": {"v": 230},
                    "Current": {"v": 0.4}, "Frequency": {"v": 50},
                }},
                "DC": {"0": {"Power": {"v": 110}, "Voltage": {"v": 30},
                             "Current": {"v": 3.5}, "YieldTotal": {"v": 1.0},
                             "YieldDay": {"v": 0.1}}},
                "INV": {"0": {"Temperature": {"v": 30}}},
            }],
        }
        plugin = _make_plugin()
        plugin._session = _mock_session(json_response=data)

        result = await plugin.poll()
        assert result.success is True
        assert result.inverter_registers[14] == 100


class TestPerSerialURL:
    """Plugin must query ?inv=<serial> — newer OpenDTU firmware drops
    AC/DC blocks from the bulk endpoint."""

    @pytest.mark.asyncio
    async def test_poll_url_includes_inv_query(self):
        plugin = _make_plugin(serial="112100000001")
        session = _mock_session()
        plugin._session = session

        await plugin.poll()

        url = session.get.call_args[0][0]
        assert "/api/livedata/status" in url
        assert "inv=112100000001" in url

    @pytest.mark.asyncio
    async def test_poll_empty_serial_uses_bulk_endpoint(self):
        """With empty serial (auto-adopt path) the plugin falls back to
        the bulk endpoint so _find_inverter can resolve the single inverter."""
        plugin = OpenDTUPlugin(gateway_config=_make_gateway(), serial="")
        session = _mock_session()
        plugin._session = session

        await plugin.poll()

        url = session.get.call_args[0][0]
        assert "/api/livedata/status" in url
        assert "inv=" not in url


class TestGatewayCache:
    """Cache deduplicates back-to-back queries for the same (host, serial)."""

    @pytest.mark.asyncio
    async def test_repeat_poll_within_ttl_hits_cache(self):
        plugin = _make_plugin()
        session = _mock_session()
        plugin._session = session

        await plugin.poll()
        await plugin.poll()
        assert session.get.call_count == 1

    @pytest.mark.asyncio
    async def test_cache_expires_after_ttl(self):
        plugin = _make_plugin()
        session = _mock_session()
        plugin._session = session

        await plugin.poll()
        assert session.get.call_count == 1

        # Force the cache entry's timestamp into the past.
        _GATEWAY_CACHE[(plugin._gw.host, plugin.serial)]["ts"] -= (
            _GATEWAY_CACHE_TTL_S + 1.0
        )

        await plugin.poll()
        assert session.get.call_count == 2

    @pytest.mark.asyncio
    async def test_different_serials_do_not_share_cache(self):
        """Two inverters on the same gateway each have their own URL and
        their own cache entry — neither should poison the other."""
        plugin_a = _make_plugin(serial="112100000001", name="Shed")
        plugin_b = _make_plugin(serial="114100000002", name="Balcony")
        session_a = _mock_session()
        session_b = _mock_session()
        plugin_a._session = session_a
        plugin_b._session = session_b

        await plugin_a.poll()
        await plugin_b.poll()
        assert session_a.get.call_count == 1
        assert session_b.get.call_count == 1

    @pytest.mark.asyncio
    async def test_different_hosts_do_not_share_cache(self):
        plugin_a = OpenDTUPlugin(
            gateway_config=GatewayConfig(host="192.0.2.98", user="admin", password="x"),
            serial="112100000001",
        )
        plugin_b = OpenDTUPlugin(
            gateway_config=GatewayConfig(host="192.0.2.113", user="admin", password="x"),
            serial="112100000001",
        )
        session_a = _mock_session()
        session_b = _mock_session()
        plugin_a._session = session_a
        plugin_b._session = session_b

        await plugin_a.poll()
        await plugin_b.poll()
        assert session_a.get.call_count == 1
        assert session_b.get.call_count == 1

    @pytest.mark.asyncio
    async def test_concurrent_polls_dedupe_via_lock(self):
        """Two concurrent polls of the same (host, serial) collapse to
        one HTTP request — the lock guarantees only one coroutine fetches
        while the other waits and reads the populated cache."""
        plugin = _make_plugin()
        session = _mock_session()
        plugin._session = session

        await asyncio.gather(plugin.poll(), plugin.poll())
        assert session.get.call_count == 1
