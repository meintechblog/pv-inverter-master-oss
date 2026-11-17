"""Regression: a micro inverter going quiet must report 0 W SLEEPING.

Bug (2026-05-30): when a Hoymiles HMS / OpenDTU micro goes to sleep its
plugin returns an empty PollResult. ``collect_from_raw`` then produced a
snapshot with no ``ac_power_w`` at all, so the dashboard froze on the last
daytime value (Cabin stuck at ~207 W all night) and InfluxDB recorded
no zero. The collector now synthesizes an explicit 0 W SLEEPING frame once
the device has reported real data, preserving the energy total.
"""
from __future__ import annotations

from pv_inverter_master.dashboard import DashboardCollector


def _real_frame(ac_power_w: int, energy_wh: int) -> list[int]:
    """Minimal SunSpec Model 103 register frame with SF=0 fields."""
    regs = [0] * 52
    regs[0] = 103
    regs[1] = 50
    regs[14] = ac_power_w        # AC power, SF at 15 = 0
    regs[24] = (energy_wh >> 16) & 0xFFFF
    regs[25] = energy_wh & 0xFFFF  # acc32 energy, SF at 26 = 0
    regs[31] = ac_power_w        # DC power ~ AC for the test, SF at 32 = 0
    regs[38] = 4                 # status MPPT
    return regs


def test_empty_after_real_data_yields_sleeping_zero():
    c = DashboardCollector()
    # Daytime poll: real production.
    snap = c.collect_from_raw(common_registers=[], inverter_registers=_real_frame(206, 5000))
    assert snap["inverter"]["ac_power_w"] == 206

    # Night: plugin returns empty registers (asleep / radio down).
    snap = c.collect_from_raw(common_registers=[], inverter_registers=[])
    inv = snap["inverter"]
    assert inv["ac_power_w"] == 0.0, "asleep device must report an explicit 0 W, not freeze on 206"
    assert inv["dc_power_w"] == 0.0
    assert inv["status"] == "SLEEPING"
    assert inv["energy_total_wh"] == 5000, "lifetime energy must be preserved while asleep"


def test_empty_before_any_real_data_stays_empty():
    c = DashboardCollector()
    # No real frame ever seen — do not invent a fake zero at boot.
    snap = c.collect_from_raw(common_registers=[], inverter_registers=[])
    assert "ac_power_w" not in snap["inverter"]
