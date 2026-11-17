"""InfluxDB 2.x time-series logger: queue-based, batched line-protocol writes.

Consumes device + virtual telemetry from ``ctx.influx_queue`` and writes it
to InfluxDB via the ``/api/v2/write`` HTTP API. Independent of the MQTT
publisher — both consume their own queue, fed in parallel by the snapshot
broadcaster.

Line-protocol schema
--------------------
    inverter,device_id=<id>,device=<name>,type=<type> <numeric fields> <ts_ns>
    aggregate,name=<virtual name> total_power_w=...,device_count=...i <ts_ns>

The ``inverter`` field set is taken verbatim from ``mqtt_payloads.device_payload``
so InfluxDB and MQTT always log exactly the same values.
"""
from __future__ import annotations

import asyncio
import math
import time

import aiohttp
import structlog

from pv_inverter_master.mqtt_payloads import device_payload

log = structlog.get_logger(component="influx_writer")

# device_payload keys that are integer-typed in line protocol.
_INT_FIELDS = {"energy_total_wh", "daily_energy_wh", "status_code"}
# device_payload keys that are not numeric line-protocol fields.
_SKIP_FIELDS = {"name", "ts", "status"}

_MAX_BACKOFF_S = 30.0
_WRITE_TIMEOUT_S = 10.0


def _esc_tag(value: object) -> str:
    """Escape a line-protocol tag value (space, comma, equals, backslash)."""
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace(" ", "\\ ")
        .replace(",", "\\,")
        .replace("=", "\\=")
    )


def _fmt_float(value: object) -> str | None:
    """Format a float line-protocol field, or None to skip it."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return repr(f)


def _fmt_int(value: object) -> str | None:
    """Format an integer line-protocol field (``i`` suffix), or None to skip."""
    try:
        return f"{int(round(float(value)))}i"
    except (TypeError, ValueError):
        return None


def device_line(
    device_id: str,
    device_name: str,
    device_type: str,
    snapshot: dict,
    ts_ns: int,
    poll_gap_s: object = None,
) -> str | None:
    """Build an ``inverter`` line-protocol record from a device snapshot.

    Returns None when the snapshot carries no usable numeric field.

    ``poll_gap_s`` (optional) is the monotonic distance to the PREVIOUS
    successful poll. It rides the queue message rather than the snapshot so it
    never enters the MQTT payload — see broadcast_device_snapshot. Absent/None
    on a device's first success, and never on its own enough to emit a line.
    """
    payload = device_payload(snapshot or {}, device_name=device_name or "")
    fields: list[str] = []
    for key, value in payload.items():
        if key in _SKIP_FIELDS or value is None:
            continue
        formatted = _fmt_int(value) if key in _INT_FIELDS else _fmt_float(value)
        if formatted is not None:
            fields.append(f"{key}={formatted}")

    status = payload.get("status")
    if isinstance(status, str) and status:
        fields.append(f'status="{status}"')

    if not fields:
        return None

    # Appended after the emptiness check on purpose: a gap value alone is not
    # a reason to write a point for a device that reported nothing usable.
    if poll_gap_s is not None:
        gap = _fmt_float(poll_gap_s)
        # Negative is impossible on a monotonic clock; guard anyway so a bad
        # value cannot poison the series the floor calibration will read.
        if gap is not None and float(poll_gap_s) >= 0:
            fields.append(f"poll_gap_s={gap}")

    tags = (
        f"device_id={_esc_tag(device_id)}"
        f",device={_esc_tag(device_name or device_id)}"
        f",type={_esc_tag(device_type or 'unknown')}"
    )
    return f"inverter,{tags} {','.join(fields)} {ts_ns}"


def virtual_line(virtual_data: dict, ts_ns: int) -> str | None:
    """Build an ``aggregate`` line-protocol record from the virtual snapshot."""
    total = _fmt_float(virtual_data.get("total_power_w"))
    if total is None:
        return None
    fields = [f"total_power_w={total}"]
    rated = _fmt_float(virtual_data.get("total_rated_w"))
    if rated is not None:
        fields.append(f"total_rated_w={rated}")
    contribs = virtual_data.get("contributions") or []
    fields.append(f"device_count={len(contribs)}i")
    name = _esc_tag(virtual_data.get("virtual_name") or "virtual")
    return f"aggregate,name={name} {','.join(fields)} {ts_ns}"


def location_line(location_name: str, lat: object, lon: object, ts_ns: int) -> str | None:
    """Build a ``location`` line-protocol record for a Standort position.

    Schema: ``location,name=<standort> lat=...,lon=... <ts_ns>`` — so the
    caravan's track is queryable over time.
    """
    flat = _fmt_float(lat)
    flon = _fmt_float(lon)
    if flat is None or flon is None:
        return None
    name = _esc_tag(location_name or "location")
    return f"location,name={name} lat={flat},lon={flon} {ts_ns}"


def _msg_to_line(msg: dict) -> str | None:
    """Convert a queue message into a single line-protocol record."""
    mtype = msg.get("type")
    if mtype == "device":
        snapshot = msg.get("snapshot") or {}
        snap_ts = snapshot.get("ts")
        ts_ns = int(float(snap_ts) * 1e9) if snap_ts else int(time.time() * 1e9)
        return device_line(
            msg.get("device_id", ""),
            msg.get("device_name", ""),
            msg.get("device_type", ""),
            snapshot,
            ts_ns,
            msg.get("poll_gap_s"),
        )
    if mtype == "virtual":
        return virtual_line(msg.get("virtual_data") or {}, int(time.time() * 1e9))
    if mtype == "location":
        return location_line(
            msg.get("location_name", ""),
            msg.get("lat"),
            msg.get("lon"),
            int(time.time() * 1e9),
        )
    return None


async def influx_write_loop(ctx, config) -> None:
    """Background task: batch telemetry from ``ctx.influx_queue`` into InfluxDB.

    Args:
        ctx: AppContext with influx_queue, influx_* stats, shutdown_event.
        config: InfluxConfig with url, token, org, bucket, interval_s.
    """
    queue = ctx.influx_queue
    write_url = f"{config.url.rstrip('/')}/api/v2/write"
    params = {"org": config.org, "bucket": config.bucket, "precision": "ns"}
    headers = {
        "Authorization": f"Token {config.token}",
        "Content-Type": "text/plain; charset=utf-8",
    }
    backoff = 1.0

    log.info("influx_writer_started", url=config.url, bucket=config.bucket)
    try:
        async with aiohttp.ClientSession() as session:
            while not ctx.shutdown_event.is_set():
                lines = await _collect_batch(queue, config.interval_s, ctx)
                if not lines:
                    continue

                body = "\n".join(lines).encode("utf-8")
                try:
                    async with session.post(
                        write_url, params=params, headers=headers, data=body,
                        timeout=aiohttp.ClientTimeout(total=_WRITE_TIMEOUT_S),
                    ) as resp:
                        if resp.status in (200, 204):
                            ctx.influx_connected = True
                            ctx.influx_points += len(lines)
                            ctx.influx_bytes += len(body)
                            ctx.influx_writes += 1
                            ctx.influx_last_ts = time.time()
                            ctx.influx_last_error = ""
                            backoff = 1.0
                        else:
                            text = (await resp.text())[:200]
                            ctx.influx_connected = False
                            ctx.influx_errors += 1
                            ctx.influx_last_error = f"HTTP {resp.status}: {text}"
                            log.warning(
                                "influx_write_rejected",
                                status=resp.status, error=text,
                            )
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    ctx.influx_connected = False
                    ctx.influx_errors += 1
                    ctx.influx_last_error = str(e)[:200]
                    log.warning("influx_write_failed", error=str(e), backoff=backoff)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, _MAX_BACKOFF_S)
    except asyncio.CancelledError:
        pass
    finally:
        ctx.influx_connected = False
        log.info("influx_writer_stopped")


async def _collect_batch(queue, interval_s: float, ctx) -> list[str]:
    """Drain the queue for one interval, returning encoded line-protocol records."""
    lines: list[str] = []
    deadline = time.monotonic() + interval_s
    while not ctx.shutdown_event.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            msg = await asyncio.wait_for(queue.get(), timeout=remaining)
        except asyncio.TimeoutError:
            break
        line = _msg_to_line(msg)
        if line:
            lines.append(line)
        while not queue.empty():
            try:
                line = _msg_to_line(queue.get_nowait())
            except asyncio.QueueEmpty:
                break
            if line:
                lines.append(line)
    return lines
