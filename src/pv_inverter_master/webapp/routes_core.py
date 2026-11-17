"""Core webapp routes: index page, status, health, dashboard, static
assets and the live-snapshot WebSocket endpoint.
"""
from __future__ import annotations

import importlib.resources as pkg_resources
import os
import time

from aiohttp import web

from pv_inverter_master.config import Config

from .payloads import (
    _build_device_list,
    _build_location_list,
    _build_virtual_contributions,
)


async def index_handler(request: web.Request) -> web.Response:
    """Serve the frontend HTML page."""
    try:
        ref = pkg_resources.files("pv_inverter_master") / "static" / "index.html"
        html = ref.read_text(encoding="utf-8")
        return web.Response(text=html, content_type="text/html")
    except (FileNotFoundError, TypeError, ModuleNotFoundError):
        return web.Response(
            text="<h1>Frontend not deployed</h1>",
            content_type="text/html",
        )


async def status_handler(request: web.Request) -> web.Response:
    """Return connection state and Venus OS status.

    A device only counts as healthy when its connection state is
    "connected" AND it has produced at least one successful poll —
    a bare TCP connect (or one healthy sibling among many devices)
    must not report "connected" for the whole fleet (2026-07-16
    incident: SE30K dead for weeks while /api/status said
    "connected" off the healthy Hoymiles).
    """
    app_ctx = request.app["app_ctx"]

    conn_states = []
    unhealthy: list[str] = []
    healthy_n = 0
    for dev_id, ds in app_ctx.devices.items():
        if ds.conn_mgr is None:
            continue
        state = ds.conn_mgr.state.value
        conn_states.append(state)
        if state == "connected" and ds.last_successful_poll_at is not None:
            healthy_n += 1
        else:
            unhealthy.append(dev_id)

    if not conn_states:
        inverter_state = "no_devices"
    elif healthy_n == len(conn_states):
        inverter_state = "connected"
    elif healthy_n > 0:
        inverter_state = "degraded"
    else:
        # None healthy: surface the first raw state — but a TCP-level
        # "connected" without any successful poll must not read as healthy.
        inverter_state = "connecting" if conn_states[0] == "connected" else conn_states[0]

    venus_connected = app_ctx.venus_mqtt_connected
    if venus_connected is True:
        venus_status = "connected"
    elif venus_connected is False:
        venus_status = "disconnected"
    else:
        venus_status = "not configured"

    return web.json_response({
        "solaredge": inverter_state,
        "venus_os": venus_status,
        "venus_os_detected": app_ctx.venus_os_detected,
        "venus_os_client_ip": app_ctx.venus_os_client_ip,
        "reconfiguring": request.app.get("reconfiguring", False),
        "device_count": len(app_ctx.devices),
        "devices_ok": healthy_n,
        "devices_unhealthy": unhealthy,
    })


#: Startup grace window in seconds. Inside this window the overall status
#: collapses to "starting" instead of "degraded" so the Phase 45-04 updater
#: healthcheck does not misread a cold boot as an update failure
#: (HEALTH-01..02, read in concert with HEALTH-05's 15s stability check).
_HEALTH_STARTUP_GRACE_S = 30.0


def _derive_health_payload(app_ctx, uptime_s: float, config) -> dict:
    """Pure helper: build the rich /api/health response dict.

    Implements HEALTH-01..03. Factored out of :func:`health_handler` so that
    unit tests can exercise every derivation path without spinning up an
    aiohttp Application. Does no IO — only reads fields off ``app_ctx`` and
    ``config``. Safe to call from any thread.

    Args:
        app_ctx: The live :class:`AppContext` instance.
        uptime_s: Seconds since webapp ``start_time``. Caller computes this
            via ``time.monotonic() - app["start_time"]`` so the helper stays
            deterministic under tests.
        config: A config-like object with a ``venus.host`` attribute. Used
            to decide ``venus_os == "disabled"`` when the host is empty.

    Returns:
        A plain dict with exactly the 8 keys mandated by HEALTH-01:
        ``{status, version, commit, uptime_seconds, webapp, modbus_server,
        devices, venus_os}``.
    """
    # --- per-component derivation ------------------------------------------
    webapp_s = "ok"  # by definition: if this code runs, the webapp is up

    cache = app_ctx.cache
    if cache is None:
        modbus_s = "failed"
    elif cache.is_stale or getattr(app_ctx, "distribution_error", False) is True:
        # A failed power-limit distribution is a regulation fault: surface it.
        modbus_s = "degraded"
    else:
        modbus_s = "ok"

    devices_s: dict[str, str] = {}
    for dev_id, ds in app_ctx.devices.items():
        if ds.poll_counter.get("success", 0) > 0:
            devices_s[dev_id] = "ok"
        else:
            devices_s[dev_id] = "starting"

    # venus_os is warn-only per HEALTH-03 — even when "degraded" it never
    # flips overall status. "disabled" when host is empty (operator opted out).
    venus_host = getattr(getattr(config, "venus", None), "host", "") or ""
    if venus_host == "":
        venus_s = "disabled"
    elif app_ctx.venus_mqtt_connected:
        venus_s = "ok"
    else:
        venus_s = "degraded"

    # --- overall status derivation (HEALTH-02) ------------------------------
    required_ok = (
        webapp_s == "ok"
        and modbus_s == "ok"
        and any(s == "ok" for s in devices_s.values())
    )
    in_grace = uptime_s < _HEALTH_STARTUP_GRACE_S

    if required_ok:
        overall = "ok"
    elif in_grace:
        overall = "starting"
        # Remap flickering component states during startup so the updater
        # doesn't see a transient "degraded" modbus_server before the first
        # successful poll lands.
        if modbus_s in ("degraded", "failed"):
            modbus_s = "starting"
        devices_s = {
            k: ("ok" if v == "ok" else "starting") for k, v in devices_s.items()
        }
    else:
        overall = "degraded"

    return {
        "status": overall,
        "version": app_ctx.current_version or "unknown",
        "commit": app_ctx.current_commit or "unknown",
        "uptime_seconds": round(uptime_s, 1),
        "webapp": webapp_s,
        "modbus_server": modbus_s,
        "devices": devices_s,
        "venus_os": venus_s,
    }


async def health_handler(request: web.Request) -> web.Response:
    """Return the rich component-level health payload.

    Implements HEALTH-01..03 for Phase 45's in-app updater (Plan 45-04 polls
    this endpoint after restart to decide update success / rollback). The
    schema is::

        {
          "status": "ok" | "starting" | "degraded",
          "version": "8.0.0",
          "commit": "abc123d",
          "uptime_seconds": 42.1,
          "webapp": "ok",
          "modbus_server": "ok" | "starting" | "degraded" | "failed",
          "devices": {"<device_id>": "ok" | "starting" | "degraded" | "failed"},
          "venus_os": "ok" | "degraded" | "disabled"
        }

    HEALTH-04 (``/run/pv-inverter-master/healthy`` flag) is not written here —
    ``__main__._healthy_flag_watcher`` (Phase 43) owns that side-effect and
    runs once on the first successful poll.

    The handler itself is a thin wrapper around :func:`_derive_health_payload`
    to keep the hot path pure (no subprocess, no file IO, no DB).
    """
    app_ctx = request.app["app_ctx"]
    config = request.app["config"]
    uptime = time.monotonic() - request.app["start_time"]
    payload = _derive_health_payload(app_ctx, uptime, config)
    return web.json_response(payload)



#: Static-file content types resolved by extension. Serving by extension
#: (rather than an explicit filename whitelist) lets new assets — the
#: vendored Leaflet bundle, map.js, the software_*.js pages — be served
#: without touching this map.
_STATIC_CONTENT_TYPES = {
    ".html": "text/html",
    ".css": "text/css",
    ".js": "application/javascript",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
}


async def dashboard_handler(request: web.Request) -> web.Response:
    """Return the latest decoded dashboard snapshot as JSON.

    Prefers the first configured device (unchanged legacy behavior while
    it is healthy) but falls back to the first device that HAS a snapshot:
    a single dead first device must not 503 the endpoint while the rest of
    the fleet is fine (2026-06-11 incident: SE30K wedge -> dashboard down
    despite 10 healthy devices). The response carries ``device_id`` so
    consumers can tell whose snapshot they received; ``inverter_name`` /
    ``inverter_serial`` inside the snapshot already disambiguate too.
    503 now means: NO device has produced any data yet.
    """
    app_ctx = request.app["app_ctx"]
    # TODO Phase 24: aggregated virtual dashboard
    for dev_id, ds in app_ctx.devices.items():
        collector = ds.collector
        if collector is not None and collector.last_snapshot is not None:
            return web.json_response(
                {**collector.last_snapshot, "device_id": dev_id},
            )
    return web.json_response({"error": "no data"}, status=503)


async def static_handler(request: web.Request) -> web.Response:
    """Serve static assets from the package via importlib.resources.

    The ``{filename}`` route segment cannot contain a slash, so directory
    traversal is structurally impossible; a leading dot or stray
    separator is still rejected defensively.
    """
    filename = request.match_info["filename"]
    if "/" in filename or "\\" in filename or filename.startswith("."):
        raise web.HTTPNotFound()
    content_type = _STATIC_CONTENT_TYPES.get(os.path.splitext(filename)[1].lower())
    if content_type is None:
        raise web.HTTPNotFound()
    try:
        ref = pkg_resources.files("pv_inverter_master") / "static" / filename
        if content_type.startswith("text/") or content_type == "application/javascript":
            return web.Response(text=ref.read_text(encoding="utf-8"), content_type=content_type)
        return web.Response(body=ref.read_bytes(), content_type=content_type)
    except (FileNotFoundError, TypeError, ModuleNotFoundError, OSError):
        raise web.HTTPNotFound()


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    """Handle WebSocket connections at /ws.

    On connect: send latest snapshot + downsampled history.
    Then keep connection open for broadcast pushes from poll loop.
    """
    ws = web.WebSocketResponse(heartbeat=30.0)
    await ws.prepare(request)
    request.app["ws_clients"].add(ws)

    try:
        ws_app_ctx = request.app["app_ctx"]
        config: Config = request.app["config"]

        # Send per-device snapshots for all active devices
        sent_any = False
        first_collector = None
        for dev_id, ds in ws_app_ctx.devices.items():
            if ds.collector and ds.collector.last_snapshot:
                if first_collector is None:
                    first_collector = ds.collector
                snap = ds.collector.last_snapshot
                # Override with per-device clamp values
                if ws_app_ctx.control_state and "control" in snap:
                    dmin, dmax = ws_app_ctx.control_state.get_device_clamp(dev_id)
                    snap["control"]["clamp_min_pct"] = dmin
                    snap["control"]["clamp_max_pct"] = dmax
                await ws.send_json({
                    "type": "device_snapshot",
                    "device_id": dev_id,
                    "data": snap,
                })
                sent_any = True

        # Also send legacy snapshot for backward compat (first device)
        if first_collector is not None and first_collector.last_snapshot is not None:
            await ws.send_json({"type": "snapshot", "data": first_collector.last_snapshot})
        elif ws_app_ctx.polling_paused:
            await ws.send_json({"type": "no_inverter"})

        # Send virtual snapshot
        if sent_any:
            total_power_w, total_rated_w, contributions = _build_virtual_contributions(ws_app_ctx, config)
            await ws.send_json({"type": "virtual_snapshot", "data": {
                "total_power_w": total_power_w,
                "total_rated_w": total_rated_w,
                "virtual_name": config.virtual_inverter.name,
                "contributions": contributions,
            }})

        # Send downsampled history for sparklines (first device only)
        collector = first_collector
        if collector is not None:
            mono_offset = time.time() - time.monotonic()
            history: dict[str, list[list[float]]] = {}
            for buf_key, buf in collector._buffers.items():
                samples = buf.get_all()
                if not samples:
                    continue
                # Downsample with step of 3 (5 min / 3 = ~100 points)
                downsampled = samples[::3]
                history[buf_key] = [
                    [s.timestamp + mono_offset, s.value] for s in downsampled
                ]
            if history:
                await ws.send_json({"type": "history", "data": history})

        # Send initial device list so client knows connection states (MQTT etc.)
        devices = _build_device_list(ws_app_ctx, config)
        await ws.send_json({"type": "device_list", "data": {"devices": devices}})

        # Send initial Standort list (powers the Locations page + map)
        await ws.send_json({"type": "location_list", "data": {
            "locations": _build_location_list(ws_app_ctx, config),
        }})

        # Phase 44: send initial available_update state so fresh clients know
        # the current version + any pending release info (CHECK-01/05/06)
        await ws.send_json({
            "type": "available_update",
            "data": {
                "current_version": ws_app_ctx.current_version,
                "current_commit": ws_app_ctx.current_commit,
                "available_update": ws_app_ctx.available_update,
                "last_check_at": ws_app_ctx.update_last_check_at,
                "last_check_failed_at": ws_app_ctx.update_last_check_failed_at,
            },
        })

        # Keep connection alive; read loop for future commands (Phase 7)
        async for msg in ws:
            if msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                break
    finally:
        request.app["ws_clients"].discard(ws)

    return ws


