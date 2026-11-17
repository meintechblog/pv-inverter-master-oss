"""Tests for the SolarEdge Enhanced Dynamic Power Control watchdog path.

Covers the connect()-time init (word-order probe, grid-code gate, ensure +
read-back) and the dynamic write path (single 0xF322 transaction), plus
fallback-to-legacy behavior on every init failure mode.
"""
from __future__ import annotations

import asyncio
import struct

import pytest

from pv_inverter_master.plugins.solaredge import (
    SolarEdgePlugin,
    DPC_ENABLE_REG,
    DPC_MAX_ACTIVE_POWER_REG,
    DPC_ACTIVE_POWER_LIMIT_REG,
    DPC_COMMAND_TIMEOUT_REG,
    DPC_FALLBACK_PCT_REG,
    DPC_DYNAMIC_LIMIT_REG,
    DPC_VERIFY_EVERY_N_WRITES,
    ADV_PWR_CONTROL_EN_REG,
    REACTIVE_PWR_CONFIG_REG,
    LEGACY_LIMIT_REG,
    _f32_to_regs,
    _regs_to_f32,
    _u32_to_regs,
    _regs_to_u32,
)


class _Resp:
    def __init__(self, registers=None, error=False):
        self.registers = registers or []
        self._error = error

    def isError(self):
        return self._error


class FakeClient:
    """Register-store fake: reads/writes keyed by absolute address."""

    def __init__(self, store: dict[int, int], *, fail_reads=(), fail_writes=(),
                 dead_writes=()):
        self.connected = True
        self.store = dict(store)
        self.fail_reads = set(fail_reads)
        self.fail_writes = set(fail_writes)
        # Writes that ACK but do not change the store (read-back mismatch).
        self.dead_writes = set(dead_writes)
        self.writes: list[tuple[int, list[int]]] = []

    def close(self):
        self.connected = False

    async def read_holding_registers(self, address, count, device_id):
        if address in self.fail_reads:
            return _Resp(error=True)
        return _Resp([self.store.get(address + i, 0) for i in range(count)])

    async def write_registers(self, address, values, device_id):
        self.writes.append((address, list(values)))
        if address in self.fail_writes:
            return _Resp(error=True)
        if address not in self.dead_writes:
            for i, v in enumerate(values):
                self.store[address + i] = v
        return _Resp([])


def _seed(store: dict[int, int], address: int, regs: list[int]) -> None:
    for i, v in enumerate(regs):
        store[address + i] = v


def _base_store(high_word_first: bool = True, max_power: float = 30000.0,
                adv: int = 1, react: int = 4) -> dict[int, int]:
    store: dict[int, int] = {}
    _seed(store, DPC_MAX_ACTIVE_POWER_REG, _f32_to_regs(max_power, high_word_first))
    _seed(store, ADV_PWR_CONTROL_EN_REG, _u32_to_regs(adv, high_word_first))
    _seed(store, REACTIVE_PWR_CONFIG_REG, _u32_to_regs(react, high_word_first))
    store[DPC_ENABLE_REG] = 0
    store[LEGACY_LIMIT_REG] = 100
    return store


async def _init(plugin: SolarEdgePlugin) -> bool:
    """Run the DPC init the way connect() does: inside a gateway transaction."""
    async with plugin.gateway.transaction() as t:
        return await plugin._init_dynamic_power_control(t)


def _plugin(client: FakeClient, **kwargs) -> SolarEdgePlugin:
    plugin = SolarEdgePlugin(**kwargs)
    plugin._client = client
    return plugin


class TestWordHelpers:
    def test_f32_roundtrip_both_orders(self):
        for hwf in (True, False):
            regs = _f32_to_regs(30000.0, hwf)
            assert _regs_to_f32(regs, hwf) == pytest.approx(30000.0)

    def test_u32_roundtrip_both_orders(self):
        for hwf in (True, False):
            assert _regs_to_u32(_u32_to_regs(120, hwf), hwf) == 120

    def test_word_orders_differ(self):
        assert _f32_to_regs(30000.0, True) == list(reversed(_f32_to_regs(30000.0, False)))


class TestInit:
    @pytest.mark.asyncio
    async def test_arms_watchdog_high_word_first(self):
        client = FakeClient(_base_store(high_word_first=True))
        plugin = _plugin(client)
        assert await _init(plugin) is True
        assert plugin._high_word_first is True
        written = dict(client.writes)
        assert written[DPC_ACTIVE_POWER_LIMIT_REG] == _f32_to_regs(30000.0, True)
        assert written[DPC_COMMAND_TIMEOUT_REG] == _u32_to_regs(120, True)
        assert written[DPC_FALLBACK_PCT_REG] == _f32_to_regs(100.0, True)
        # Enable written LAST
        assert client.writes[-1] == (DPC_ENABLE_REG, [1])

    @pytest.mark.asyncio
    async def test_detects_low_word_first(self):
        client = FakeClient(_base_store(high_word_first=False))
        plugin = _plugin(client)
        assert await _init(plugin) is True
        assert plugin._high_word_first is False
        written = dict(client.writes)
        assert written[DPC_COMMAND_TIMEOUT_REG] == _u32_to_regs(120, False)

    @pytest.mark.asyncio
    async def test_idempotent_when_already_armed(self):
        store = _base_store()
        _seed(store, DPC_ACTIVE_POWER_LIMIT_REG, _f32_to_regs(30000.0, True))
        _seed(store, DPC_COMMAND_TIMEOUT_REG, _u32_to_regs(120, True))
        _seed(store, DPC_FALLBACK_PCT_REG, _f32_to_regs(100.0, True))
        store[DPC_ENABLE_REG] = 1
        client = FakeClient(store)
        plugin = _plugin(client)
        assert await _init(plugin) is True
        assert client.writes == []  # nothing to do, zero write load

    @pytest.mark.asyncio
    async def test_gate_blocks_wrong_reactive_config(self):
        client = FakeClient(_base_store(react=0))
        plugin = _plugin(client)
        assert await _init(plugin) is False
        # Grid-relevant registers must never be written autonomously.
        addresses = [a for a, _ in client.writes]
        assert ADV_PWR_CONTROL_EN_REG not in addresses
        assert REACTIVE_PWR_CONFIG_REG not in addresses
        assert DPC_ENABLE_REG not in addresses

    @pytest.mark.asyncio
    async def test_gate_blocks_disabled_adv_control(self):
        client = FakeClient(_base_store(adv=0))
        plugin = _plugin(client)
        assert await _init(plugin) is False

    @pytest.mark.asyncio
    async def test_implausible_probe_falls_back(self):
        store = _base_store()
        _seed(store, DPC_MAX_ACTIVE_POWER_REG, [0, 0])
        client = FakeClient(store)
        plugin = _plugin(client)
        assert await _init(plugin) is False
        assert client.writes == []

    @pytest.mark.asyncio
    async def test_unreadable_probe_falls_back(self):
        client = FakeClient(_base_store(), fail_reads=[DPC_MAX_ACTIVE_POWER_REG])
        plugin = _plugin(client)
        assert await _init(plugin) is False

    @pytest.mark.asyncio
    async def test_readback_mismatch_falls_back(self):
        client = FakeClient(
            _base_store(), dead_writes=[DPC_COMMAND_TIMEOUT_REG],
        )
        plugin = _plugin(client)
        assert await _init(plugin) is False

    @pytest.mark.asyncio
    async def test_setting_write_error_falls_back(self):
        client = FakeClient(_base_store(), fail_writes=[DPC_FALLBACK_PCT_REG])
        plugin = _plugin(client)
        assert await _init(plugin) is False
        # Enable must not have been attempted after a failed setting write.
        assert DPC_ENABLE_REG not in [a for a, _ in client.writes]

    @pytest.mark.asyncio
    async def test_normalizes_legacy_latch_residue(self):
        """A residual sub-100% value in 61441 (the incident register) is
        cleared during init so it cannot survive underneath the dynamic
        path."""
        store = _base_store()
        store[LEGACY_LIMIT_REG] = 5  # the latched 5% from the incident
        client = FakeClient(store)
        plugin = _plugin(client)
        assert await _init(plugin) is True
        assert (LEGACY_LIMIT_REG, [100]) in client.writes
        assert client.store[LEGACY_LIMIT_REG] == 100

    @pytest.mark.asyncio
    async def test_gate_accepts_alternate_word_order(self):
        """F1xx gate registers may use a different word order than the
        probed F3xx block — values 1/4 decode unambiguously, accept both."""
        store = _base_store(high_word_first=True)
        _seed(store, ADV_PWR_CONTROL_EN_REG, _u32_to_regs(1, False))
        _seed(store, REACTIVE_PWR_CONFIG_REG, _u32_to_regs(4, False))
        client = FakeClient(store)
        plugin = _plugin(client)
        assert await _init(plugin) is True

    @pytest.mark.asyncio
    async def test_custom_timeout_and_fallback(self):
        client = FakeClient(_base_store())
        plugin = _plugin(client, dpc_command_timeout_s=300, dpc_fallback_pct=60.0)
        assert await _init(plugin) is True
        written = dict(client.writes)
        assert written[DPC_COMMAND_TIMEOUT_REG] == _u32_to_regs(300, True)
        assert _regs_to_f32(written[DPC_FALLBACK_PCT_REG], True) == pytest.approx(60.0)


class TestDynamicWrite:
    @pytest.mark.asyncio
    async def test_single_transaction_to_dynamic_register(self):
        client = FakeClient(_base_store())
        plugin = _plugin(client)
        plugin._dynamic_ok = True
        plugin._high_word_first = True
        result = await plugin.write_power_limit(True, 42.5)
        assert result.success
        assert client.writes == [(DPC_DYNAMIC_LIMIT_REG, _f32_to_regs(42.5, True))]

    @pytest.mark.asyncio
    async def test_disable_writes_100_percent(self):
        client = FakeClient(_base_store())
        plugin = _plugin(client)
        plugin._dynamic_ok = True
        result = await plugin.write_power_limit(False, 5.0)
        assert result.success
        addr, regs = client.writes[0]
        assert addr == DPC_DYNAMIC_LIMIT_REG
        assert _regs_to_f32(regs, True) == pytest.approx(100.0)

    @pytest.mark.asyncio
    async def test_floor_at_one_percent(self):
        client = FakeClient(_base_store())
        plugin = _plugin(client)
        plugin._dynamic_ok = True
        await plugin.write_power_limit(True, 0.0)
        _, regs = client.writes[0]
        assert _regs_to_f32(regs, True) == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_low_word_first_encoding_used(self):
        client = FakeClient(_base_store(high_word_first=False))
        plugin = _plugin(client)
        plugin._dynamic_ok = True
        plugin._high_word_first = False
        await plugin.write_power_limit(True, 50.0)
        assert client.writes == [(DPC_DYNAMIC_LIMIT_REG, _f32_to_regs(50.0, False))]

    @pytest.mark.asyncio
    async def test_write_error_reported_no_legacy_fallback(self):
        client = FakeClient(_base_store(), fail_writes=[DPC_DYNAMIC_LIMIT_REG])
        plugin = _plugin(client)
        plugin._dynamic_ok = True
        result = await plugin.write_power_limit(True, 50.0)
        assert not result.success
        # Exactly one attempt; never a legacy static write into flaky comms.
        assert [a for a, _ in client.writes] == [DPC_DYNAMIC_LIMIT_REG]

    @pytest.mark.asyncio
    async def test_write_during_init_waits_for_gateway_and_runs_dynamic(self):
        """No write may slip into the pre-armed init window - a legacy static
        write there would be latchable. Structural now: init
        holds the gateway lock, so the write queues behind it and then takes
        the DYNAMIC path (no 61762/61441 legacy writes)."""
        client = FakeClient(_base_store())
        plugin = _plugin(client)
        async with plugin.gateway.transaction() as t:
            plugin._dynamic_ok = await plugin._init_dynamic_power_control(t)
            writer = asyncio.ensure_future(plugin.write_power_limit(True, 5.0))
            await asyncio.sleep(0.01)
            assert not writer.done()  # blocked while the init block holds the lock
        result = await writer
        assert result.success
        assert all(addr not in (61762, 61441) for addr, _ in client.writes[-1:])
        assert client.writes[-1][0] == DPC_DYNAMIC_LIMIT_REG

    @pytest.mark.asyncio
    async def test_periodic_verify_rearms_after_inverter_reset(self):
        """An inverter-side reset clears 0xF300; the Nth write detects it
        and re-initializes inline."""
        client = FakeClient(_base_store())
        plugin = _plugin(client)
        assert await _init(plugin) is True
        plugin._dynamic_ok = True
        client.writes.clear()
        client.store[DPC_ENABLE_REG] = 0  # inverter restarted, block volatile
        plugin._writes_since_verify = DPC_VERIFY_EVERY_N_WRITES - 1
        result = await plugin.write_power_limit(True, 50.0)
        assert result.success
        assert (DPC_ENABLE_REG, [1]) in client.writes  # re-armed
        assert client.writes[-1][0] == DPC_DYNAMIC_LIMIT_REG  # then the limit

    @pytest.mark.asyncio
    async def test_periodic_verify_disarms_loudly_when_reinit_fails(self):
        """If re-arming fails the write must FAIL, not ACK a no-op."""
        client = FakeClient(_base_store())
        plugin = _plugin(client)
        assert await _init(plugin) is True
        plugin._dynamic_ok = True
        client.store[DPC_ENABLE_REG] = 0
        client.fail_reads.add(DPC_MAX_ACTIVE_POWER_REG)  # re-init probe dies
        plugin._writes_since_verify = DPC_VERIFY_EVERY_N_WRITES - 1
        result = await plugin.write_power_limit(True, 50.0)
        assert not result.success
        assert plugin._dynamic_ok is False

    @pytest.mark.asyncio
    async def test_closed_refuses_write(self):
        client = FakeClient(_base_store())
        plugin = _plugin(client)
        plugin._dynamic_ok = True
        await plugin.close()
        client.writes.clear()
        result = await plugin.write_power_limit(True, 50.0)
        assert not result.success
        assert result.error == "Not connected"
        assert client.writes == []

    @pytest.mark.asyncio
    async def test_legacy_path_when_not_armed(self):
        client = FakeClient(_base_store())
        plugin = _plugin(client)
        plugin._dynamic_ok = False
        result = await plugin.write_power_limit(True, 50.0)
        assert result.success
        addresses = [a for a, _ in client.writes]
        assert 61762 in addresses and 61441 in addresses
        assert DPC_DYNAMIC_LIMIT_REG not in addresses
        # Let the fire-and-forget commit task finish cleanly.
        await plugin.close()


class TestConnectIntegration:
    @pytest.mark.asyncio
    async def test_connect_arms_and_flag_off_skips(self, monkeypatch):
        seen = {}

        class _FakeTcp:
            def __init__(self, host, port, timeout, retries, reconnect_delay=0):
                self.connected = True
                self.inner = FakeClient(_base_store())

            async def connect(self):
                return True

            async def read_holding_registers(self, *a, **kw):
                return await self.inner.read_holding_registers(*a, **kw)

            async def write_registers(self, *a, **kw):
                return await self.inner.write_registers(*a, **kw)

        monkeypatch.setattr(
            "pv_inverter_master.modbus_gateway.AsyncModbusTcpClient", _FakeTcp,
        )
        plugin = SolarEdgePlugin()
        await plugin.connect()
        assert plugin._dynamic_ok is True

        plugin_off = SolarEdgePlugin(dynamic_power_control=False)
        await plugin_off.connect()
        assert plugin_off._dynamic_ok is False
