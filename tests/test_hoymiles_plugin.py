"""Tests for the Hoymiles HMS WLAN plugin.

Covers the contract that matters for waterfall integration and the
two quirks we explicitly defended against:

- HMS-01: Implements the full InverterPlugin ABC.
- HMS-02: Treats TCP timeout / None response as success-but-empty so
          night-time silence doesn't trip the registry into failed state.
- HMS-03: Parses RealDataNew protobuf-like response into SunSpec
          Model 103 + Common Model + per-channel dc_channels cache.
- HMS-04: Auto-adopts serial from `device_serial_number` on first poll
          when none was configured by the user.
- HMS-05: Power-limit dead-time + min-delta guard suppresses EEPROM
          wear from waterfall re-sends, but `force=True` bypasses
          dead-time only.
- HMS-06: throttle_capabilities advertises proportional with the long
          (60 s) cooldown that reflects EEPROM-write protection.
- HMS-07: Plugin factory routes type="hoymiles" to HoymilesPlugin.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from pv_inverter_master.plugin import InverterPlugin, WriteResult
from pv_inverter_master.plugins.hoymiles import (
    DEAD_TIME_S,
    MIN_LIMIT_DELTA_PCT,
    NIGHT_MODE_AFTER_POLLS,
    HoymilesPlugin,
    discover_hoymiles_devices,
)
from pv_inverter_master.sunspec_models import _int16_as_uint16


def _fake_real_data(
    *,
    ac_power_dW: int = 5234,        # 523.4 W
    ac_voltage_dV: int = 2301,      # 230.1 V
    ac_current_cA: int = 227,       # 2.27 A
    ac_freq_cHz: int = 5001,        # 50.01 Hz
    temperature_dC: int = 351,      # 35.1 °C
    power_limit_raw: int = 100,     # 100 %
    serial: str = "112345678900",
    panels: int = 2,
) -> SimpleNamespace:
    pv = []
    for i in range(panels):
        pv.append(SimpleNamespace(
            port_number=i + 1,
            voltage=321,            # 32.1 V (×10)
            current=452,            # 4.52 A (×100)
            power=1452,             # 145.2 W (×10)
            energy_total=1234567,   # Wh
            energy_daily=2345,      # Wh
            error_code=0,
        ))
    sgs = [SimpleNamespace(
        voltage=ac_voltage_dV,
        frequency=ac_freq_cHz,
        current=ac_current_cA,
        active_power=ac_power_dW,
        reactive_power=0,
        power_factor=1000,
        temperature=temperature_dC,
        warning_number=0,
        link_status=1,
        power_limit=power_limit_raw,
        # Numeric sub-inverter id — distinct from the alphanumeric DTU
        # serial string (HMS serials like "4143A0110000" are not
        # decimal, so this must not be int(serial)).
        serial_number=11111111111111,
        firmware_version=10043,
    )]
    return SimpleNamespace(
        device_serial_number=serial,
        timestamp=1716000000,
        firmware_version=10043,
        dtu_power=ac_power_dW,
        dtu_daily_energy=panels * 2345,
        ap=1, cp=1,
        pv_data=pv,
        sgs_data=sgs,
        tgs_data=[],
    )


# ---------------------------------------------------------------------------
# HMS-01: ABC compliance
# ---------------------------------------------------------------------------

def test_plugin_implements_inverter_plugin_abc():
    p = HoymilesPlugin(host="203.0.113.1", name="HMS-800")
    assert isinstance(p, InverterPlugin)


# ---------------------------------------------------------------------------
# HMS-02: Night-time silence is not a failure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_poll_timeout_returns_success_empty():
    """TCP timeout (inverter asleep) → success=True, empty regs, no error."""
    p = HoymilesPlugin(host="203.0.113.1")
    await p.connect()
    p._dtu.async_get_real_data_new = AsyncMock(side_effect=asyncio.TimeoutError())
    result = await p.poll()
    assert result.success is True
    assert result.error is None
    assert result.common_registers == []
    assert result.inverter_registers == []
    assert p.hoymiles_status["reachable"] is False


@pytest.mark.asyncio
async def test_poll_none_response_returns_success_empty():
    """CRC fail / silent device → success=True, empty regs (registry stays healthy)."""
    p = HoymilesPlugin(host="203.0.113.1")
    await p.connect()
    p._dtu.async_get_real_data_new = AsyncMock(return_value=None)
    result = await p.poll()
    assert result.success is True
    assert p.hoymiles_status["reachable"] is False


@pytest.mark.asyncio
async def test_poll_oserror_returns_success_empty():
    """OSError (host unreachable) is treated the same as a timeout."""
    p = HoymilesPlugin(host="203.0.113.1")
    await p.connect()
    p._dtu.async_get_real_data_new = AsyncMock(side_effect=OSError("no route"))
    result = await p.poll()
    assert result.success is True


# ---------------------------------------------------------------------------
# HMS-03: Real data is decoded into the right register positions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_poll_decodes_real_data_into_model_103():
    p = HoymilesPlugin(host="203.0.113.1")
    await p.connect()
    fake = _fake_real_data()
    p._dtu.async_get_real_data_new = AsyncMock(return_value=fake)
    # Skip the app-info refresh that piggybacks on the same code path.
    p._dtu.async_app_information_data = AsyncMock(return_value=None)
    p._app_info_ts = 1e9  # already-refreshed sentinel

    result = await p.poll()
    assert result.success is True
    assert len(result.common_registers) == 67
    assert len(result.inverter_registers) == 52

    regs = result.inverter_registers
    assert regs[0] == 103   # DID
    assert regs[1] == 50    # length
    # AC Voltage 230.1 V → 2301 (×10, SF=-1)
    assert regs[10] == 2301
    assert regs[13] == _int16_as_uint16(-1)
    # AC Power 523.4 W → rounded to 523 W (SF=0)
    assert regs[14] == 523
    assert regs[15] == 0
    # AC Frequency 50.01 Hz → 5001 (×100, SF=-2)
    assert regs[16] == 5001
    assert regs[17] == _int16_as_uint16(-2)
    # Temperature 35.1 °C → 351 (×10, SF=-1)
    assert regs[33] == 351
    assert regs[37] == _int16_as_uint16(-1)
    # Status code: producing → 4 (MPPT)
    assert regs[38] == 4

    # DC channel cache: two PVs, each 145.2 W / 32.1 V / 4.52 A
    assert len(p.dc_channels) == 2
    assert p.dc_channels[0]["power_w"] == 145.2
    assert p.dc_channels[0]["voltage_v"] == 32.1
    assert p.dc_channels[0]["current_a"] == 4.52
    assert p.dc_channels[0]["yield_day_wh"] == 2345

    # hoymiles_status cache
    assert p.hoymiles_status["reachable"] is True
    assert p.hoymiles_status["producing"] is True
    assert p.hoymiles_status["limit_pct"] == 100


# ---------------------------------------------------------------------------
# HMS-04: Auto-adopt serial on first poll
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_poll_auto_adopts_serial_when_unset():
    p = HoymilesPlugin(host="203.0.113.1")  # no serial in config
    await p.connect()
    fake = _fake_real_data(serial="118800001234")
    p._dtu.async_get_real_data_new = AsyncMock(return_value=fake)
    p._dtu.async_app_information_data = AsyncMock(return_value=None)
    p._app_info_ts = 1e9

    await p.poll()
    assert p.serial == "118800001234"


@pytest.mark.asyncio
async def test_poll_does_not_overwrite_existing_serial():
    p = HoymilesPlugin(host="203.0.113.1", serial="999999999999")
    await p.connect()
    fake = _fake_real_data(serial="118800001234")
    p._dtu.async_get_real_data_new = AsyncMock(return_value=fake)
    p._dtu.async_app_information_data = AsyncMock(return_value=None)
    p._app_info_ts = 1e9
    await p.poll()
    assert p.serial == "999999999999"


# ---------------------------------------------------------------------------
# HMS-05: EEPROM-wear protection on power limit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_power_limit_first_call_writes():
    p = HoymilesPlugin(host="203.0.113.1")
    await p.connect()
    p._dtu.async_set_power_limit = AsyncMock(return_value=SimpleNamespace())
    r = await p.write_power_limit(enable=True, limit_pct=50)
    assert r.success
    p._dtu.async_set_power_limit.assert_awaited_once_with(50)


@pytest.mark.asyncio
async def test_power_limit_dead_time_suppresses_resend():
    p = HoymilesPlugin(host="203.0.113.1")
    await p.connect()
    p._dtu.async_set_power_limit = AsyncMock(return_value=SimpleNamespace())
    await p.write_power_limit(enable=True, limit_pct=50)
    # Within dead-time, even with a large delta, second call must NOT hit the DTU
    r = await p.write_power_limit(enable=True, limit_pct=10)
    assert r.success
    assert p._dtu.async_set_power_limit.await_count == 1


@pytest.mark.asyncio
async def test_power_limit_delta_guard_suppresses_tiny_nudges():
    p = HoymilesPlugin(host="203.0.113.1")
    await p.connect()
    p._dtu.async_set_power_limit = AsyncMock(return_value=SimpleNamespace())
    await p.write_power_limit(enable=True, limit_pct=50)
    # Trick the dead-time clock so only the delta guard is left
    p._last_limit_ts -= DEAD_TIME_S + 1.0
    # 1 % delta < MIN_LIMIT_DELTA_PCT (2 %) → suppressed
    r = await p.write_power_limit(enable=True, limit_pct=51)
    assert r.success
    assert p._dtu.async_set_power_limit.await_count == 1
    # 3 % delta > threshold → write
    r = await p.write_power_limit(enable=True, limit_pct=53)
    assert r.success
    assert p._dtu.async_set_power_limit.await_count == 2


@pytest.mark.asyncio
async def test_power_limit_force_bypasses_dead_time():
    p = HoymilesPlugin(host="203.0.113.1")
    await p.connect()
    p._dtu.async_set_power_limit = AsyncMock(return_value=SimpleNamespace())
    await p.write_power_limit(enable=True, limit_pct=50)
    r = await p.write_power_limit(enable=True, limit_pct=10, force=True)
    assert r.success
    assert p._dtu.async_set_power_limit.await_count == 2


@pytest.mark.asyncio
async def test_power_limit_disable_sets_full_power():
    p = HoymilesPlugin(host="203.0.113.1")
    await p.connect()
    p._dtu.async_set_power_limit = AsyncMock(return_value=SimpleNamespace())
    await p.write_power_limit(enable=False, limit_pct=20)
    p._dtu.async_set_power_limit.assert_awaited_once_with(100)


# ---------------------------------------------------------------------------
# HMS-06: Throttle capabilities
# ---------------------------------------------------------------------------

def test_throttle_capabilities_proportional_with_long_cooldown():
    """HMS WLAN: proportional but EEPROM-wear-limited → score 5.0.

    Verified against a live regulation test: the limit is a true
    proportional one, but every write hits EEPROM so the declared
    cooldown is long. The resulting score (5.0) matches OpenDTU's
    score (also 5.0 since OpenDTU is just a different gateway for
    the same HMS micros) and sits above binary Shelly (2.8).
    """
    from pv_inverter_master.plugin import compute_throttle_score
    caps = HoymilesPlugin(host="203.0.113.1").throttle_capabilities
    assert caps.mode == "proportional"
    assert caps.cooldown_s == 300.0  # EEPROM-wear: 5 min between writes
    assert compute_throttle_score(caps) == 5.0


def test_min_poll_interval_protects_rate_limited_dtu():
    """The HMS DTU must not be polled faster than the documented floor.

    Fast polling overwhelms the built-in WiFi DTU; the registry honours
    this floor regardless of the configured/global poll interval.
    """
    from pv_inverter_master.plugins.hoymiles import MIN_POLL_INTERVAL_S
    # No firmware known yet → safe baseline.
    p = HoymilesPlugin(host="203.0.113.1")
    assert p.min_poll_interval == MIN_POLL_INTERVAL_S
    # Empirical floor: 15s freezes within minutes; 20s passed a 6h
    # afternoon soak but failed across a full day cycle on FW 267
    # (2026-05-24); 30s is rock-solid across multiple day/night cycles.
    assert p.min_poll_interval >= 30.0


def test_min_poll_interval_tightens_for_new_firmware():
    """FW >= 4097 tolerates the tighter 20 s floor (Kathi-class units)."""
    from pv_inverter_master.plugins.hoymiles import (
        MIN_POLL_INTERVAL_FAST_S, FW_TOLERATES_FAST_POLL,
    )
    p = HoymilesPlugin(host="203.0.113.1", firmware_version=str(FW_TOLERATES_FAST_POLL))
    assert p.min_poll_interval == MIN_POLL_INTERVAL_FAST_S
    # And a clearly-newer one stays on the tighter floor.
    p2 = HoymilesPlugin(host="203.0.113.1", firmware_version="9999")
    assert p2.min_poll_interval == MIN_POLL_INTERVAL_FAST_S


def test_min_poll_interval_stays_safe_for_old_firmware():
    """FW 267 (the units that froze across the day cycle) stays at 30 s."""
    from pv_inverter_master.plugins.hoymiles import MIN_POLL_INTERVAL_S
    p = HoymilesPlugin(host="203.0.113.1", firmware_version="267")
    assert p.min_poll_interval == MIN_POLL_INTERVAL_S


def test_min_poll_interval_safe_for_garbage_firmware_value():
    """Empty / non-numeric firmware falls back to the safe baseline."""
    from pv_inverter_master.plugins.hoymiles import MIN_POLL_INTERVAL_S
    for fw in ("", "unknown", "v4.0.97"):
        p = HoymilesPlugin(host="203.0.113.1", firmware_version=fw)
        assert p.min_poll_interval == MIN_POLL_INTERVAL_S, fw


@pytest.mark.asyncio
async def test_poll_adopts_firmware_from_app_info():
    """First app-info refresh writes the FW onto the plugin attribute so
    the registry can persist it to entry.firmware_version."""
    p = HoymilesPlugin(host="203.0.113.1")
    assert p.firmware_version == ""
    await p.connect()
    fake = _fake_real_data()
    dtu_info = SimpleNamespace(dtu_sw_version=4097, signal_strength=70)
    app_info = SimpleNamespace(dtu_info=dtu_info)
    p._dtu.async_get_real_data_new = AsyncMock(return_value=fake)
    p._dtu.async_app_information_data = AsyncMock(return_value=app_info)
    p._app_info_ts = 0.0  # force the refresh path
    await p.poll()
    assert p.firmware_version == "4097"
    assert p.hoymiles_status["firmware_version"] == "4097"


# ---------------------------------------------------------------------------
# HMS-08: Night-mode poll backoff
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_night_mode_entered_after_consecutive_timeouts():
    """After a few unreachable polls the plugin flips into night mode."""
    p = HoymilesPlugin(host="203.0.113.1")
    await p.connect()
    p._dtu.async_get_real_data_new = AsyncMock(side_effect=asyncio.TimeoutError())
    for _ in range(NIGHT_MODE_AFTER_POLLS):
        assert p._night_mode is False
        await p.poll()
    assert p._night_mode is True


@pytest.mark.asyncio
async def test_night_mode_entered_after_consecutive_none():
    """An asleep HMS answers with None frames — that must reach night mode.

    Regression: a powered-down HMS DTU keeps its TCP port open and
    returns None rather than timing out. The None path used to reset the
    counter, so night mode never engaged for these inverters.
    """
    p = HoymilesPlugin(host="203.0.113.1")
    await p.connect()
    p._dtu.async_get_real_data_new = AsyncMock(return_value=None)
    for _ in range(NIGHT_MODE_AFTER_POLLS):
        await p.poll()
    assert p._night_mode is True


@pytest.mark.asyncio
async def test_night_mode_skips_query_between_probes():
    """In night mode, a poll within the probe interval does zero I/O."""
    p = HoymilesPlugin(host="203.0.113.1")
    await p.connect()
    p._night_mode = True
    p._last_night_probe = time.monotonic()  # just probed
    real = AsyncMock(return_value=None)
    p._dtu.async_get_real_data_new = real

    result = await p.poll()
    assert result.success is True
    real.assert_not_called()  # no protobuf query at all


@pytest.mark.asyncio
async def test_night_mode_probe_stays_asleep_on_no_data():
    """Once the probe interval elapses, a None probe keeps us asleep."""
    p = HoymilesPlugin(host="203.0.113.1")
    await p.connect()
    p._night_mode = True
    p._last_night_probe = 0.0  # probe interval long elapsed
    real = AsyncMock(return_value=None)
    p._dtu.async_get_real_data_new = real

    result = await p.poll()
    assert result.success is True
    assert p._night_mode is True
    real.assert_called_once()  # one real probe poll was spent


@pytest.mark.asyncio
async def test_night_mode_wakes_on_real_data():
    """A probe poll that returns production data ends night mode."""
    p = HoymilesPlugin(host="203.0.113.1")
    await p.connect()
    p._night_mode = True
    p._last_night_probe = 0.0
    p._dtu.async_get_real_data_new = AsyncMock(return_value=_fake_real_data())
    p._dtu.async_app_information_data = AsyncMock(return_value=None)
    p._app_info_ts = 1e9

    result = await p.poll()
    assert result.success is True
    assert p._night_mode is False
    assert p._consecutive_unreachable == 0
    assert p.hoymiles_status["producing"] is True


@pytest.mark.asyncio
async def test_successful_poll_clears_unreachable_counter():
    """A good poll resets the night-mode counter so blips don't accrue."""
    p = HoymilesPlugin(host="203.0.113.1")
    await p.connect()
    p._consecutive_unreachable = NIGHT_MODE_AFTER_POLLS - 1  # one short
    p._dtu.async_get_real_data_new = AsyncMock(return_value=_fake_real_data())
    p._dtu.async_app_information_data = AsyncMock(return_value=None)
    p._app_info_ts = 1e9

    await p.poll()
    assert p._consecutive_unreachable == 0
    assert p._night_mode is False


# ---------------------------------------------------------------------------
# HMS-07: Plugin factory wiring
# ---------------------------------------------------------------------------

def test_plugin_factory_returns_hoymiles_for_type():
    from pv_inverter_master.config import InverterEntry
    from pv_inverter_master.plugins import plugin_factory
    entry = InverterEntry(type="hoymiles", host="203.0.113.1", name="Garden-HMS", rated_power=800)
    plugin = plugin_factory(entry)
    assert isinstance(plugin, HoymilesPlugin)
    assert plugin.host == "203.0.113.1"
    assert plugin.name == "Garden-HMS"
    assert plugin._max_power_w == 800


# ---------------------------------------------------------------------------
# Static identity & nameplate
# ---------------------------------------------------------------------------

def test_static_common_overrides_include_hoymiles_manufacturer():
    p = HoymilesPlugin(host="203.0.113.1", name="HMS-800W-2T")
    ov = p.get_static_common_overrides()
    # First 16 manufacturer registers spell "Hoymiles\0\0\0\0\0\0\0\0"
    # ASCII "Ho" = 0x486F
    assert ov[2] == 0x486F
    assert ov[66] == 126  # PROXY_UNIT_ID


def test_model_120_uses_rated_power():
    p = HoymilesPlugin(host="203.0.113.1", rated_power=850)
    regs = p.get_model_120_registers()
    assert regs[0] == 120
    assert regs[1] == 26
    assert regs[2] == 4    # DERTyp = PV
    assert regs[3] == 850  # WRtg


def test_model_120_defaults_to_800w_when_unconfigured():
    p = HoymilesPlugin(host="203.0.113.1", rated_power=0)
    regs = p.get_model_120_registers()
    assert regs[3] == 800


# ---------------------------------------------------------------------------
# HMS-08: Subnet auto-discovery
# ---------------------------------------------------------------------------

class _FakeDTU:
    """Stand-in for hoymiles_wifi.dtu.DTU — producing host returns data."""

    def __init__(self, host, **_kw):
        self.host = host

    async def async_get_real_data_new(self):
        if self.host == "198.51.100.242":
            return _fake_real_data(serial="4143A0110000", ac_power_dW=1043, panels=2)
        return None  # open port but silent (asleep / not an HMS)


@pytest.mark.asyncio
async def test_discover_finds_producing_and_sleeping_hosts():
    """Discovery sweeps port 10081, then protobuf-probes each open host.

    One host produces (full data), one is open-but-silent (sleeping).
    Closed hosts are dropped.
    """
    open_ports = {"198.51.100.242", "198.51.100.150"}

    async def fake_probe(ip, port, timeout):
        assert port == 10081
        return ip in open_ports

    with patch("pv_inverter_master.scanner._probe_port", side_effect=fake_probe), \
         patch("pv_inverter_master.scanner.detect_subnet") as det, \
         patch("hoymiles_wifi.dtu.DTU", _FakeDTU):
        from ipaddress import IPv4Network
        det.return_value = IPv4Network("198.51.100.0/24")
        devices = await discover_hoymiles_devices()

    by_host = {d["host"]: d for d in devices}
    assert set(by_host) == open_ports
    prod = by_host["198.51.100.242"]
    assert prod["serial"] == "4143A0110000"
    assert prod["panels"] == 2
    assert prod["producing"] is True
    assert prod["ac_power_w"] == 104  # 1043 raw / 10, rounded
    assert prod["sleeping"] is False
    # Open-but-silent host: reported so the UI can still offer it
    silent = by_host["198.51.100.150"]
    assert silent["sleeping"] is True
    assert silent["serial"] == ""


@pytest.mark.asyncio
async def test_discover_honors_subnet_override_and_scans_all():
    """An explicit subnet is parsed; already-configured IPs are still swept."""
    probed: list[str] = []

    async def fake_probe(ip, port, timeout):
        probed.append(ip)
        return False

    with patch("pv_inverter_master.scanner._probe_port", side_effect=fake_probe):
        devices = await discover_hoymiles_devices(
            subnet_override="198.51.100.0/24",
            skip_ips={"198.51.100.242"},
        )

    assert devices == []
    # Already-configured IPs are no longer filtered out — they are kept
    # in the results flagged already_added so the UI can grey them out.
    assert "198.51.100.242" in probed
    assert "198.51.100.150" in probed
    assert len(probed) == 254                # full /24 host range
