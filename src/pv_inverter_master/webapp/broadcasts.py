"""WebSocket broadcast helpers for the webapp.

Each function pushes one message type to every connected client and
silently prunes dead sockets. Telemetry queues (MQTT publisher, InfluxDB
logger) are fed from the snapshot broadcasts.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json

from aiohttp import web

from pv_inverter_master.config import Config

from .payloads import (
    _build_device_list,
    _build_location_list,
    _build_virtual_contributions,
)


async def broadcast_to_clients(app: web.Application, snapshot: dict) -> None:
    """Push a snapshot to all connected WebSocket clients.

    Dead/disconnected clients are silently removed.
    """
    clients = app.get("ws_clients")
    if not clients:
        return

    # Attach Venus OS ESS settings if available
    bc_app_ctx = app.get("app_ctx")
    venus_settings = bc_app_ctx.venus_settings if bc_app_ctx is not None else None
    if venus_settings:
        snapshot["venus_settings"] = venus_settings

    payload = json.dumps({"type": "snapshot", "data": snapshot})
    for ws in set(clients):
        try:
            await ws.send_str(payload)
        except (ConnectionError, RuntimeError, ConnectionResetError):
            clients.discard(ws)


async def broadcast_device_snapshot(app: web.Application, device_id: str, snapshot: dict) -> None:
    """Feed per-device telemetry queues and push to any WebSocket clients.

    InfluxDB + MQTT logging must NOT depend on a browser being connected:
    the telemetry enqueue runs unconditionally. Only the WebSocket fan-out
    is skipped when no clients are attached. (Earlier this whole function
    returned early on an empty client set, so closing the dashboard tab
    silently stopped all time-series logging for every device.)
    """
    bc_ctx = app.get("app_ctx")
    venus_settings = bc_ctx.venus_settings if bc_ctx else None
    if venus_settings:
        snapshot["venus_settings"] = venus_settings
    # Override clamp values with per-device clamp if available
    if bc_ctx and bc_ctx.control_state and "control" in snapshot:
        dmin, dmax = bc_ctx.control_state.get_device_clamp(device_id)
        snapshot["control"]["clamp_min_pct"] = dmin
        snapshot["control"]["clamp_max_pct"] = dmax
    # Inject cached OpenDTU / Hoymiles data for instant display.
    # Both plugins expose a `*_status` dict and a `dc_channels` list with
    # the same shape, so the duck-typed lookups stay uniform.
    if bc_ctx:
        ds = bc_ctx.devices.get(device_id)
        if ds and ds.plugin:
            if hasattr(ds.plugin, "opendtu_status") and ds.plugin.opendtu_status:
                snapshot["opendtu_status"] = ds.plugin.opendtu_status
            if hasattr(ds.plugin, "hoymiles_status") and ds.plugin.hoymiles_status:
                snapshot["hoymiles_status"] = ds.plugin.hoymiles_status
            if hasattr(ds.plugin, "senec_status") and ds.plugin.senec_status:
                snapshot["senec_status"] = ds.plugin.senec_status
            if hasattr(ds.plugin, "dc_channels") and ds.plugin.dc_channels:
                snapshot["dc_channels"] = ds.plugin.dc_channels

    clients = app.get("ws_clients")
    if clients:
        payload = json.dumps({"type": "device_snapshot", "device_id": device_id, "data": snapshot})
        for ws in set(clients):
            try:
                await ws.send_str(payload)
            except (ConnectionError, RuntimeError, ConnectionResetError):
                clients.discard(ws)

    # Telemetry queues — MQTT publisher (Phase 26) + InfluxDB logger.
    # Resolve device name + type from config once, feed both queues.
    if bc_ctx and (bc_ctx.mqtt_pub_queue is not None or bc_ctx.influx_queue is not None):
        _cfg = app.get("config")
        _dev_name = ""
        _dev_type = ""
        if _cfg:
            for _ie in getattr(_cfg, "inverters", []):
                if _ie.id == device_id:
                    _dev_name = _ie.name or f"{_ie.manufacturer} {_ie.model}".strip()
                    _dev_type = _ie.type
                    break
        if bc_ctx.mqtt_pub_queue is not None:
            try:
                bc_ctx.mqtt_pub_queue.put_nowait({
                    "type": "device",
                    "device_id": device_id,
                    "device_name": _dev_name or device_id,
                    "snapshot": snapshot,
                })
            except asyncio.QueueFull:
                pass  # publisher behind — stale telemetry has no value
        if bc_ctx.influx_queue is not None:
            # poll_gap_s rides the queue message rather than the snapshot so it
            # reaches InfluxDB WITHOUT entering the MQTT payload / HA discovery
            # (device_payload is shared with the publisher). It is diagnostic
            # telemetry, not a device reading.
            _ds = bc_ctx.devices.get(device_id)
            try:
                bc_ctx.influx_queue.put_nowait({
                    "type": "device",
                    "device_id": device_id,
                    "device_name": _dev_name or device_id,
                    "device_type": _dev_type,
                    "snapshot": snapshot,
                    "poll_gap_s": getattr(_ds, "last_poll_gap_s", None) if _ds else None,
                })
            except asyncio.QueueFull:
                pass


async def broadcast_virtual_snapshot(app: web.Application) -> None:
    """Feed the aggregate telemetry queues and push to any WebSocket clients.

    Like ``broadcast_device_snapshot``, the InfluxDB/MQTT enqueue runs
    regardless of whether a browser is connected — only the WebSocket
    fan-out is gated on the client set.
    """
    app_ctx = app.get("app_ctx")
    config: Config = app.get("config")
    if app_ctx is None or config is None:
        return

    total_power_w, total_rated_w, contributions = _build_virtual_contributions(app_ctx, config)

    clients = app.get("ws_clients")
    if clients:
        payload = json.dumps({"type": "virtual_snapshot", "data": {
            "total_power_w": total_power_w,
            "total_rated_w": total_rated_w,
            "virtual_name": config.virtual_inverter.name,
            "contributions": contributions,
        }})
        for ws in set(clients):
            try:
                await ws.send_str(payload)
            except (ConnectionError, RuntimeError, ConnectionResetError):
                clients.discard(ws)

    # Telemetry queues — MQTT publisher (Phase 26) + InfluxDB logger
    if app_ctx:
        _virtual_data = {
            "total_power_w": total_power_w,
            "total_rated_w": total_rated_w,
            "virtual_name": config.virtual_inverter.name,
            "contributions": contributions,
        }
        if app_ctx.mqtt_pub_queue is not None:
            try:
                app_ctx.mqtt_pub_queue.put_nowait({
                    "type": "virtual",
                    "virtual_data": _virtual_data,
                })
            except asyncio.QueueFull:
                pass
        if app_ctx.influx_queue is not None:
            try:
                app_ctx.influx_queue.put_nowait({
                    "type": "virtual",
                    "virtual_data": _virtual_data,
                })
            except asyncio.QueueFull:
                pass


async def broadcast_device_list(app: web.Application) -> None:
    """Broadcast updated device list to all WebSocket clients."""
    clients = app.get("ws_clients")
    if not clients:
        return
    app_ctx = app.get("app_ctx")
    config: Config = app.get("config")
    if app_ctx is None or config is None:
        return

    devices = _build_device_list(app_ctx, config)
    payload = json.dumps({"type": "device_list", "data": {
        "devices": devices,
    }})
    for ws in set(clients):
        try:
            await ws.send_str(payload)
        except (ConnectionError, RuntimeError, ConnectionResetError):
            clients.discard(ws)


async def broadcast_location_list(app: web.Application) -> None:
    """Broadcast the Standort list to all WebSocket clients."""
    clients = app.get("ws_clients")
    if not clients:
        return
    app_ctx = app.get("app_ctx")
    config: Config = app.get("config")
    if app_ctx is None or config is None:
        return
    payload = json.dumps({"type": "location_list", "data": {
        "locations": _build_location_list(app_ctx, config),
    }})
    for ws in set(clients):
        try:
            await ws.send_str(payload)
        except (ConnectionError, RuntimeError, ConnectionResetError):
            clients.discard(ws)


async def broadcast_location_position(app: web.Application, location_id: str,
                                      lat: float, lon: float, ts: float) -> None:
    """Broadcast a single live Standort position update (caravan tracking)."""
    clients = app.get("ws_clients")
    if not clients:
        return
    payload = json.dumps({"type": "location_position", "data": {
        "location_id": location_id, "lat": lat, "lon": lon, "ts": ts,
    }})
    for ws in set(clients):
        try:
            await ws.send_str(payload)
        except (ConnectionError, RuntimeError, ConnectionResetError):
            clients.discard(ws)


async def broadcast_available_update(app: web.Application) -> None:
    """Push updated available_update + version info to all WS clients.

    Called by the scheduler callback whenever AppContext.available_update or
    the last_check_{at,failed_at} fields change. Mirrors broadcast_device_list
    to reuse its pruning pattern for dead clients.
    """
    clients = app.get("ws_clients")
    if not clients:
        return
    app_ctx = app.get("app_ctx")
    if app_ctx is None:
        return
    payload = json.dumps({
        "type": "available_update",
        "data": {
            "current_version": app_ctx.current_version,
            "current_commit": app_ctx.current_commit,
            "available_update": app_ctx.available_update,
            "last_check_at": app_ctx.update_last_check_at,
            "last_check_failed_at": app_ctx.update_last_check_failed_at,
        },
    })
    for ws in set(clients):
        try:
            await ws.send_str(payload)
        except (ConnectionError, RuntimeError, ConnectionResetError):
            clients.discard(ws)


async def _broadcast_scan_progress(app: web.Application, phase: str, current: int, total: int) -> None:
    """Broadcast scan progress to all WS clients."""
    clients = app.get("ws_clients")
    if not clients:
        return
    payload = json.dumps({
        "type": "scan_progress",
        "data": {"phase": phase, "current": current, "total": total}
    })
    for ws in set(clients):
        try:
            await ws.send_str(payload)
        except (ConnectionError, RuntimeError):
            clients.discard(ws)


async def _broadcast_scan_complete(app: web.Application, devices: list) -> None:
    """Broadcast scan results to all WS clients."""
    clients = app.get("ws_clients")
    if not clients:
        return
    payload = json.dumps({
        "type": "scan_complete",
        "data": {
            "devices": [{**dataclasses.asdict(d), "supported": d.supported} for d in devices],
            "count": len(devices)
        }
    })
    for ws in set(clients):
        try:
            await ws.send_str(payload)
        except (ConnectionError, RuntimeError):
            clients.discard(ws)


async def _broadcast_scan_error(app: web.Application, error: str) -> None:
    """Broadcast scan error to all WS clients."""
    clients = app.get("ws_clients")
    if not clients:
        return
    payload = json.dumps({"type": "scan_error", "data": {"error": error}})
    for ws in set(clients):
        try:
            await ws.send_str(payload)
        except (ConnectionError, RuntimeError):
            clients.discard(ws)


