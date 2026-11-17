"""Tests for the InfluxDB line-protocol encoder.

Covers the pure transformation functions that turn device + virtual
telemetry into InfluxDB line protocol:

- INFLUX-01: device_line emits a well-formed `inverter` record with the
             device_payload field set, integer fields suffixed `i`,
             string status quoted.
- INFLUX-02: tag values with spaces/commas are escaped.
- INFLUX-03: None fields are skipped; an all-empty snapshot yields None.
- INFLUX-04: virtual_line emits an `aggregate` record.
- INFLUX-05: _msg_to_line dispatches device vs virtual and uses the
             snapshot ts when present.
"""
from __future__ import annotations

from pv_inverter_master.influx_writer import (
    _msg_to_line,
    device_line,
    virtual_line,
)


def _snapshot(**inv):
    return {"ts": 1700000000.0, "inverter": inv}


# ---------------------------------------------------------------------------
# INFLUX-01: device_line basic encoding
# ---------------------------------------------------------------------------

def test_device_line_encodes_inverter_record():
    snap = _snapshot(
        ac_power_w=104.3,
        dc_power_w=108.0,
        temperature_sink_c=17.1,
        energy_total_wh=148801,
        daily_energy_wh=259,
        status="MPPT",
        status_code=4,
    )
    line = device_line("dev1", "Cottage", "hoymiles", snap, 1700000000000000000)

    assert line.startswith("inverter,")
    head, _, ts = line.rpartition(" ")
    assert ts == "1700000000000000000"
    # tags
    assert "device_id=dev1" in head
    assert "device=Cottage" in head
    assert "type=hoymiles" in head
    # float field
    assert "ac_power_w=104.3" in head
    # integer fields carry the `i` suffix
    assert "energy_total_wh=148801i" in head
    assert "daily_energy_wh=259i" in head
    assert "status_code=4i" in head
    # string field is quoted
    assert 'status="MPPT"' in head
    # temperature_sink_c is renamed to temperature_c by device_payload
    assert "temperature_c=17.1" in head


# ---------------------------------------------------------------------------
# INFLUX-02: tag escaping
# ---------------------------------------------------------------------------

def test_device_line_escapes_spaces_in_tags():
    line = device_line(
        "abc", "HMS-800W-2T Garage", "hoymiles",
        _snapshot(ac_power_w=50.0), 123,
    )
    # space in the device name must be backslash-escaped in the tag set
    assert "device=HMS-800W-2T\\ Garage" in line
    # ...but the field section (after the first unescaped space) is intact
    assert "ac_power_w=50.0" in line


# ---------------------------------------------------------------------------
# INFLUX-03: None handling
# ---------------------------------------------------------------------------

def test_device_line_skips_none_fields():
    line = device_line("d", "n", "t", _snapshot(ac_power_w=10.0, dc_power_w=None), 1)
    assert "ac_power_w=10.0" in line
    assert "dc_power_w" not in line


def test_device_line_returns_none_for_empty_snapshot():
    assert device_line("d", "n", "t", {"inverter": {}}, 1) is None
    assert device_line("d", "n", "t", {}, 1) is None


# ---------------------------------------------------------------------------
# INFLUX-04: virtual_line
# ---------------------------------------------------------------------------

def test_virtual_line_encodes_aggregate_record():
    vdata = {
        "total_power_w": 4162.0,
        "total_rated_w": 30800,
        "virtual_name": "Fronius Proxy",
        "contributions": [{"device_id": "a"}, {"device_id": "b"}, {"device_id": "c"}],
    }
    line = virtual_line(vdata, 1700000000000000000)
    assert line.startswith("aggregate,")
    assert "name=Fronius\\ Proxy" in line
    assert "total_power_w=4162.0" in line
    assert "total_rated_w=30800.0" in line
    assert "device_count=3i" in line
    assert line.endswith(" 1700000000000000000")


def test_virtual_line_returns_none_without_total_power():
    assert virtual_line({"contributions": []}, 1) is None


# ---------------------------------------------------------------------------
# INFLUX-05: _msg_to_line dispatch
# ---------------------------------------------------------------------------

def test_msg_to_line_device_uses_snapshot_ts():
    msg = {
        "type": "device",
        "device_id": "d1",
        "device_name": "Cottage",
        "device_type": "hoymiles",
        "snapshot": _snapshot(ac_power_w=99.0),
    }
    line = _msg_to_line(msg)
    # snapshot ts 1700000000.0 s -> ns
    assert line.endswith(" 1700000000000000000")
    assert "ac_power_w=99.0" in line


def test_msg_to_line_virtual():
    msg = {"type": "virtual", "virtual_data": {"total_power_w": 1234.0, "contributions": []}}
    line = _msg_to_line(msg)
    assert line.startswith("aggregate,")
    assert "total_power_w=1234.0" in line


def test_msg_to_line_unknown_type_returns_none():
    assert _msg_to_line({"type": "bogus"}) is None


# ---------------------------------------------------------------------------
# poll_gap_s: telemetry for the staleness-floor calibration
# ---------------------------------------------------------------------------


def _snap() -> dict:
    return _snapshot(ac_power_w=1234.0)


def test_poll_gap_absent_when_not_supplied():
    """Backward compatible: no gap argument = exactly the previous line."""
    line = device_line("d1", "Dev", "solaredge", _snap(), 1_000_000)
    assert "poll_gap_s" not in line


def test_poll_gap_emitted_when_supplied():
    line = device_line("d1", "Dev", "solaredge", _snap(), 1_000_000, 17.5)
    assert "poll_gap_s=17.5" in line


def test_poll_gap_zero_is_emitted_not_dropped():
    """0.0 is a real measurement (two successes in the same tick), not absence."""
    line = device_line("d1", "Dev", "solaredge", _snap(), 1_000_000, 0.0)
    assert "poll_gap_s=0.0" in line


def test_poll_gap_none_on_first_success_is_skipped():
    line = device_line("d1", "Dev", "solaredge", _snap(), 1_000_000, None)
    assert "poll_gap_s" not in line


def test_poll_gap_rejects_negative_and_nonnumeric():
    """A monotonic clock cannot go backwards — refuse to poison the series."""
    for bad in (-1.0, "kaputt", float("nan"), float("inf")):
        line = device_line("d1", "Dev", "solaredge", _snap(), 1_000_000, bad)
        assert "poll_gap_s" not in line, bad


def test_poll_gap_alone_does_not_create_a_point():
    """An empty snapshot stays empty — a gap is not a device reading."""
    assert device_line("d1", "Dev", "solaredge", {}, 1_000_000, 42.0) is None


def test_msg_to_line_forwards_the_gap():
    line = _msg_to_line({
        "type": "device", "device_id": "d1", "device_name": "Dev",
        "device_type": "solaredge", "snapshot": _snap(), "poll_gap_s": 3.25,
    })
    assert "poll_gap_s=3.25" in line
