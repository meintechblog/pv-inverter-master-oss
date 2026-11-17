"""Tests for SolarEdge SE30K plugin."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pv_inverter_master.plugin import InverterPlugin, PollResult
from pv_inverter_master.plugins.solaredge import (
    SolarEdgePlugin,
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_UNIT_ID,
    COMMON_READ_ADDR,
    COMMON_READ_COUNT,
    INVERTER_READ_ADDR,
    INVERTER_READ_COUNT,
)
from pv_inverter_master.sunspec_models import PROXY_UNIT_ID


def _make_mock_response(registers, is_error=False):
    """Create a mock Modbus response."""
    resp = MagicMock()
    resp.isError.return_value = is_error
    resp.registers = registers
    return resp


# Sample register data for tests
SAMPLE_COMMON = list(range(67))   # 67 registers
SAMPLE_INVERTER = list(range(52)) # 52 registers


class TestPluginInterface:
    def test_plugin_interface(self):
        """SolarEdgePlugin is a subclass of InverterPlugin."""
        assert issubclass(SolarEdgePlugin, InverterPlugin)

    def test_plugin_instance(self):
        """SolarEdgePlugin can be instantiated."""
        plugin = SolarEdgePlugin()
        assert isinstance(plugin, InverterPlugin)


class TestConnectionParams:
    def test_plugin_default_connection_params(self):
        """Default connection parameters match SE30K network config."""
        plugin = SolarEdgePlugin()
        assert plugin.host == "192.0.2.18"
        assert plugin.port == 1502
        assert plugin.unit_id == 1

    def test_custom_connection_params(self):
        """Custom connection parameters are stored."""
        plugin = SolarEdgePlugin(host="203.0.113.1", port=502, unit_id=3)
        assert plugin.host == "203.0.113.1"
        assert plugin.port == 502
        assert plugin.unit_id == 3

    def test_default_constants(self):
        """Module-level defaults match expected values."""
        assert DEFAULT_HOST == "192.0.2.18"
        assert DEFAULT_PORT == 1502
        assert DEFAULT_UNIT_ID == 1


class TestPoll:
    @pytest.mark.asyncio
    async def test_poll_reads_only_inverter_and_replays_cached_common(self):
        """poll() issues ONE Modbus read (Inverter 103) and replays the static
        Common Model from the session cache filled by connect().

        The Common block is static identity; re-reading it every cycle doubled
        the transaction count on a single-connection inverter for no
        information gain. The PollResult contract is unchanged — consumers
        still see a full 67-register list.
        """
        plugin = SolarEdgePlugin()
        mock_client = AsyncMock()
        mock_client.connected = True

        inverter_resp = _make_mock_response(SAMPLE_INVERTER)
        mock_client.read_holding_registers = AsyncMock(return_value=inverter_resp)

        plugin._client = mock_client
        plugin._cached_common = list(SAMPLE_COMMON)  # as connect() would leave it
        result = await plugin.poll()

        assert result.success is True
        assert result.error is None
        assert result.common_registers == SAMPLE_COMMON
        assert result.inverter_registers == SAMPLE_INVERTER
        assert len(result.common_registers) == 67
        assert len(result.inverter_registers) == 52

        # Exactly one read, and it is the Inverter block — not Common.
        calls = mock_client.read_holding_registers.call_args_list
        assert len(calls) == 1
        assert calls[0].args == (INVERTER_READ_ADDR,)
        assert calls[0].kwargs["count"] == INVERTER_READ_COUNT
        assert calls[0].kwargs["device_id"] == 1
        assert all(c.args != (COMMON_READ_ADDR,) for c in calls)

    @pytest.mark.asyncio
    async def test_poll_succeeds_with_empty_common_cache(self):
        """A failed one-shot Common read leaves the cache empty. Polling must
        still succeed — identity has no bearing on power regulation, and blank
        identity fields beat a confidently wrong serial."""
        plugin = SolarEdgePlugin()
        mock_client = AsyncMock()
        mock_client.connected = True
        mock_client.read_holding_registers = AsyncMock(
            return_value=_make_mock_response(SAMPLE_INVERTER)
        )

        plugin._client = mock_client
        plugin._cached_common = []
        result = await plugin.poll()

        assert result.success is True
        assert result.common_registers == []
        assert result.inverter_registers == SAMPLE_INVERTER

    @pytest.mark.asyncio
    async def test_cached_common_is_not_shared_between_polls(self):
        """Each PollResult gets its own list copy, so a consumer mutating the
        returned registers cannot corrupt the session cache."""
        plugin = SolarEdgePlugin()
        mock_client = AsyncMock()
        mock_client.connected = True
        mock_client.read_holding_registers = AsyncMock(
            return_value=_make_mock_response(SAMPLE_INVERTER)
        )

        plugin._client = mock_client
        plugin._cached_common = list(SAMPLE_COMMON)

        first = await plugin.poll()
        first.common_registers[0] = 0xDEAD
        second = await plugin.poll()

        assert second.common_registers == SAMPLE_COMMON

    @pytest.mark.asyncio
    async def test_poll_handles_read_error(self):
        """poll() returns PollResult(success=False) on Modbus read error."""
        plugin = SolarEdgePlugin()
        mock_client = AsyncMock()
        mock_client.connected = True

        error_resp = _make_mock_response([], is_error=True)
        mock_client.read_holding_registers = AsyncMock(return_value=error_resp)

        plugin._client = mock_client
        result = await plugin.poll()

        assert result.success is False
        assert result.error is not None
        assert result.common_registers == []
        assert result.inverter_registers == []

    @pytest.mark.asyncio
    async def test_poll_handles_inverter_read_error(self):
        """An Inverter-103 read error fails the poll even though the Common
        cache is populated — a cached identity must never make a failed poll
        look successful (that would keep last_successful_poll_at advancing on
        a wedged inverter)."""
        plugin = SolarEdgePlugin()
        mock_client = AsyncMock()
        mock_client.connected = True

        inverter_err = _make_mock_response([], is_error=True)
        mock_client.read_holding_registers = AsyncMock(return_value=inverter_err)

        plugin._client = mock_client
        plugin._cached_common = list(SAMPLE_COMMON)
        result = await plugin.poll()

        assert result.success is False
        assert "Inverter read error" in result.error
        # Failure path must not leak the cached identity.
        assert result.common_registers == []

    @pytest.mark.asyncio
    async def test_poll_handles_exception(self):
        """poll() returns PollResult(success=False) on ConnectionError without raising."""
        plugin = SolarEdgePlugin()
        mock_client = AsyncMock()
        mock_client.connected = True
        mock_client.read_holding_registers = AsyncMock(
            side_effect=ConnectionError("Connection lost")
        )

        plugin._client = mock_client
        result = await plugin.poll()

        assert result.success is False
        assert "Connection lost" in result.error

    @pytest.mark.asyncio
    async def test_poll_not_connected(self):
        """poll() before connect() returns PollResult(success=False)."""
        plugin = SolarEdgePlugin()
        result = await plugin.poll()

        assert result.success is False
        assert result.error == "Not connected"


class TestStaticOverrides:
    def test_get_static_common_overrides(self):
        """get_static_common_overrides() returns Fronius identity and unit ID 126."""
        plugin = SolarEdgePlugin()
        overrides = plugin.get_static_common_overrides()

        # DID and Length
        assert overrides[0] == 1   # COMMON_DID
        assert overrides[1] == 65  # COMMON_LENGTH

        # Manufacturer "Fronius" at offsets 2-17
        assert 2 in overrides
        assert 17 in overrides

        # C_DeviceAddress at offset 66
        assert overrides[66] == PROXY_UNIT_ID  # 126

    def test_fronius_manufacturer_encoding(self):
        """Manufacturer override encodes 'Fronius' correctly."""
        plugin = SolarEdgePlugin()
        overrides = plugin.get_static_common_overrides()

        # First register of manufacturer: "Fr" = 0x4672
        assert overrides[2] == 0x4672

        # "on" = 0x6F6E
        assert overrides[3] == 0x6F6E


class TestModel120:
    def test_get_model_120_registers(self):
        """get_model_120_registers() returns 28-element list with correct header."""
        plugin = SolarEdgePlugin()
        regs = plugin.get_model_120_registers()

        assert len(regs) == 28
        assert regs[0] == 120   # NAMEPLATE_DID
        assert regs[1] == 26    # NAMEPLATE_LENGTH
        assert regs[2] == 4     # DERTyp = PV
        assert regs[3] == 30000 # WRtg = 30kW

    def test_model_120_nameplate_values(self):
        """Model 120 contains SE30K nameplate ratings."""
        plugin = SolarEdgePlugin()
        regs = plugin.get_model_120_registers()

        assert regs[5] == 30000  # VARtg = 30kVA
        assert regs[12] == 44    # ARtg = 44A


class TestMappingInPlugin:
    def test_mapping_in_plugin(self):
        """Register mapping data is in the plugin, not in proxy core.

        The plugin provides get_model_120_registers() and
        get_static_common_overrides() -- these are the mapping concerns.
        The proxy core should only know about the datablock.
        """
        plugin = SolarEdgePlugin()

        # Model 120 registers are fully defined in the plugin
        model_120 = plugin.get_model_120_registers()
        assert len(model_120) == 28
        assert model_120[0] == 120

        # Static overrides are fully defined in the plugin
        overrides = plugin.get_static_common_overrides()
        assert isinstance(overrides, dict)
        assert len(overrides) > 0

        # These methods are defined on InverterPlugin ABC
        assert hasattr(InverterPlugin, 'get_model_120_registers')
        assert hasattr(InverterPlugin, 'get_static_common_overrides')


class TestConnect:
    @pytest.mark.asyncio
    async def test_connect_creates_client(self):
        """connect() creates AsyncModbusTcpClient and connects."""
        plugin = SolarEdgePlugin()
        with patch(
            "pv_inverter_master.modbus_gateway.AsyncModbusTcpClient"
        ) as MockClient:
            mock_instance = AsyncMock()
            mock_instance.connected = True
            mock_instance.read_holding_registers = AsyncMock(
                return_value=_make_mock_response([0] * 67)
            )
            MockClient.return_value = mock_instance

            await plugin.connect()

            MockClient.assert_called_once_with(
                DEFAULT_HOST, port=DEFAULT_PORT, timeout=2, retries=1,
                reconnect_delay=0,
            )
            mock_instance.connect.assert_awaited_once()
            assert plugin._client is mock_instance

    @pytest.mark.asyncio
    async def test_close_disconnects(self):
        """close() disconnects the client."""
        plugin = SolarEdgePlugin()
        mock_client = MagicMock()
        plugin._client = mock_client

        await plugin.close()

        mock_client.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_waits_for_inflight_transaction(self):
        """close() lets a running gateway transaction FINISH instead of FINing
        the socket mid-transaction (wedge trigger). The client is
        closed strictly AFTER the transaction."""
        plugin = SolarEdgePlugin()
        order: list[str] = []
        mock_client = MagicMock()
        mock_client.connected = True
        mock_client.close = MagicMock(side_effect=lambda: order.append("client_close"))
        plugin._client = mock_client

        async def _slow_txn():
            async with plugin.gateway.transaction():
                await asyncio.sleep(0.05)
                order.append("txn_done")

        task = asyncio.create_task(_slow_txn())
        await asyncio.sleep(0)  # let the transaction start
        await plugin.close()
        await task

        assert order == ["txn_done", "client_close"]
        assert plugin._client is None

    @pytest.mark.asyncio
    async def test_write_after_close_refused_and_reconnect_rearms(self):
        """After close() a write is refused ("Not connected"); connect() on the
        reused plugin (quiesce-close -> reconnect cycle) makes writes work again."""
        plugin = SolarEdgePlugin(dynamic_power_control=False)
        mock_client = AsyncMock()
        mock_client.connected = True
        mock_client.write_registers = AsyncMock(return_value=_make_mock_response([]))
        mock_client.close = MagicMock()
        plugin._client = mock_client

        await plugin.close()
        result = await plugin.write_power_limit(True, 50.0)
        assert result.success is False
        assert result.error == "Not connected"

        with patch(
            "pv_inverter_master.modbus_gateway.AsyncModbusTcpClient"
        ) as MockClient:
            fresh_client = AsyncMock()
            fresh_client.connected = True
            fresh_client.read_holding_registers = AsyncMock(
                return_value=_make_mock_response([0] * 67)
            )
            fresh_client.write_registers = AsyncMock(
                return_value=_make_mock_response([])
            )
            MockClient.return_value = fresh_client
            await plugin.connect()
        result = await plugin.write_power_limit(True, 50.0)
        assert result.success is True

    @pytest.mark.asyncio
    async def test_legacy_commit_runs_inside_the_write_block(self):
        """The legacy commit (61696) is no fire-and-forget task any more: it is
        written in order, after enable + limit, before write_power_limit returns."""
        plugin = SolarEdgePlugin(dynamic_power_control=False)
        mock_client = AsyncMock()
        mock_client.connected = True
        addrs: list[int] = []

        async def _write_registers(addr, values, device_id=None):
            addrs.append(addr)
            return _make_mock_response([])

        mock_client.write_registers = _write_registers
        plugin._client = mock_client

        result = await plugin.write_power_limit(True, 50.0)
        assert result.success is True
        assert addrs == [61762, 61441, 61696]
        assert not hasattr(plugin, "_commit_task")


class TestRegisterReadPolicy:
    """The UI renders this verbatim, so it must describe what poll()/connect()
    actually do — not what they used to do."""

    def test_policy_matches_actual_read_behaviour(self):
        plugin = SolarEdgePlugin()
        policy = plugin.get_register_read_policy()
        blocks = {p["block"]: p for p in policy}

        assert "Common Model" in blocks
        assert "Inverter Model 103" in blocks
        # Common is read once per session; Inverter 103 every cycle. If someone
        # reinstates the per-cycle Common read, this pins the contradiction.
        assert "Verbinden" in blocks["Common Model"]["cadence"]
        assert "Poll-Zyklus" in blocks["Inverter Model 103"]["cadence"]
        assert blocks["Common Model"]["detail"] == f"{COMMON_READ_ADDR}/{COMMON_READ_COUNT}"
        assert blocks["Inverter Model 103"]["detail"] == f"{INVERTER_READ_ADDR}/{INVERTER_READ_COUNT}"

    def test_dpc_block_only_listed_when_enabled(self):
        with_dpc = SolarEdgePlugin(dynamic_power_control=True).get_register_read_policy()
        without = SolarEdgePlugin(dynamic_power_control=False).get_register_read_policy()
        assert any(p["block"] == "Dynamic Power Control" for p in with_dpc)
        assert not any(p["block"] == "Dynamic Power Control" for p in without)

    def test_base_plugin_default_is_empty(self):
        """Plugins that synthesise their blocks report no policy, so the UI
        omits the section rather than showing a misleading one."""
        from pv_inverter_master.plugins.shelly import ShellyPlugin
        assert ShellyPlugin(host="1.2.3.4").get_register_read_policy() == []
