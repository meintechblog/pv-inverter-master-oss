"""Power-control routes: manual power limit, per-device clamp and the
Venus OS register / dbus / lock writes.
"""
from __future__ import annotations

import time

import structlog
from aiohttp import web

from pv_inverter_master.control import validate_wmaxlimpct
from pv_inverter_master.modbus_gateway import GatewayError, GatewayHostClaimed, get_gateway
from pv_inverter_master.plugin import get_throttle_caps, locked_write_power_limit

log = structlog.get_logger()


async def power_limit_handler(request: web.Request) -> web.Response:
    """Handle power limit commands from webapp.

    Actions: "set" (with limit_pct), "enable", "disable".
    Returns 409 if Venus OS wrote within last 60s.
    Returns 400 on invalid values.
    Response always includes success and error fields (CTRL-07).
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response(
            {"success": False, "error": "Invalid JSON body"}, status=400,
        )

    action = body.get("action")
    if action not in ("set", "enable", "disable"):
        return web.json_response(
            {"success": False, "error": f"Unknown action: {action}"}, status=400,
        )

    log.info("user_action", action="power_limit", limit_action=action, limit_pct=body.get("limit_pct"))

    app_ctx = request.app["app_ctx"]
    control = app_ctx.control_state
    override_log = app_ctx.override_log

    # No Venus OS priority block -- manual limit is additive (min of webapp + venus wins)

    if action == "set":
        try:
            limit_pct = float(body["limit_pct"])
        except (KeyError, ValueError, TypeError):
            return web.json_response(
                {"success": False, "error": "Missing or invalid limit_pct"}, status=400,
            )

        if limit_pct < 0 or limit_pct > 100:
            return web.json_response(
                {"success": False, "error": f"limit_pct must be 0-100, got {limit_pct}"},
                status=400,
            )

        raw_value = int(round(limit_pct))  # SF=0: 50% -> 50
        error = validate_wmaxlimpct(raw_value)
        if error:
            return web.json_response(
                {"success": False, "error": error}, status=400,
            )

        # Accept locally and trigger distribution immediately
        control.set_from_webapp(raw_value, 1)
        if override_log is not None:
            override_log.append("webapp", "set", limit_pct)
        distributor = getattr(app_ctx, 'distributor', None)
        if distributor is not None:
            await distributor.distribute(control.wmaxlimpct_float, control.is_enabled)
        return web.json_response({"success": True, "error": None})

    elif action == "enable":
        control.update_wmaxlim_ena(1)
        control.last_source = "webapp"
        control.last_change_ts = time.time()
        control.webapp_revert_at = time.monotonic() + 300.0
        if override_log is not None:
            override_log.append("webapp", "enable", control.wmaxlimpct_float)
        distributor = getattr(app_ctx, 'distributor', None)
        if distributor is not None:
            await distributor.distribute(control.wmaxlimpct_float, control.is_enabled)
        return web.json_response({"success": True, "error": None})

    else:  # disable
        control.update_wmaxlim_ena(0)
        control.last_source = "none"
        control.webapp_revert_at = None
        if override_log is not None:
            override_log.append("webapp", "disable", None)
        distributor = getattr(app_ctx, 'distributor', None)
        if distributor is not None:
            await distributor.distribute(control.wmaxlimpct_float, False)
        return web.json_response({"success": True, "error": None})


async def power_clamp_handler(request: web.Request) -> web.Response:
    """Set per-device min/max power limit.

    Body: {"device_id": "abc123", "min_pct": 0, "max_pct": 13}
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response(
            {"success": False, "error": "Invalid JSON body"}, status=400,
        )

    device_id = body.get("device_id")
    if not device_id:
        return web.json_response(
            {"success": False, "error": "device_id required"}, status=400,
        )

    app_ctx = request.app["app_ctx"]
    control = app_ctx.control_state
    min_pct = max(0, min(100, int(body.get("min_pct", 0))))
    max_pct = max(0, min(100, int(body.get("max_pct", 100))))

    control.set_device_clamp(device_id, min_pct, max_pct)

    # Virtual aggregate: clamp applies to the whole Fronius proxy. Update the
    # global clamp_min/max_pct (read by proxy.async_setValues to clip Venus OS
    # writes), re-clamp the current limit, and trigger an immediate
    # distribution so the limit takes effect now — not only on the next
    # Venus OS write.
    if device_id == "virtual":
        control.clamp_min_pct = min_pct
        control.clamp_max_pct = max_pct
        # The user's max dropdown is THE active limit, not just an upper
        # bound on Venus OS writes. When they pick "Max" (100/0), release
        # the throttle entirely — distributor sends 100% to all devices
        # and the SolarEdge ramps back up. Otherwise the chosen ceiling is
        # the setpoint.
        full_release = (min_pct == 0 and max_pct >= 100)
        if full_release:
            control.update_wmaxlim_ena(0)
            # Keep wmaxlimpct_raw at 100 so the readback is sane.
            control.update_wmaxlimpct(100)
        else:
            control.update_wmaxlimpct(max_pct)
            control.update_wmaxlim_ena(1)
        # Tag the source so persisted state files record it correctly
        # and the boot-time restore paths re-apply it.
        control.last_source = "webapp"
        control.last_change_ts = time.time()
        control.save_ui_state()
        control.save_last_limit()
        if app_ctx.override_log is not None:
            app_ctx.override_log.append(
                "webapp", "release" if full_release else "clamp",
                control.wmaxlimpct_float,
                detail=f"min={min_pct}% max={max_pct}%",
            )
        distributor = getattr(app_ctx, "distributor", None)
        if distributor is not None:
            await distributor.distribute(
                control.wmaxlimpct_float, control.is_enabled,
            )
        return web.json_response({"success": True})

    # Find the plugin for this device
    plugin = None
    ds = app_ctx.devices.get(device_id)
    if ds and ds.plugin:
        plugin = ds.plugin

    if plugin is not None:
        caps = get_throttle_caps(plugin)
        if caps and caps.mode == "binary":
            # Binary device: switch on/off based on max_pct
            await plugin.switch(max_pct > 0)
        elif max_pct < 100:
            # Route through the choke point so the write holds the device's
            # poll lock. getattr-guard: a ds without the
            # field (old test doubles) degrades to an unlocked call.
            await locked_write_power_limit(
                plugin, True, max_pct,
                force=True, poll_lock=getattr(ds, "poll_lock", None),
            )
        else:
            await locked_write_power_limit(
                plugin, True, 100.0,
                force=True, poll_lock=getattr(ds, "poll_lock", None),
            )

    return web.json_response({"success": True})


async def venus_write_handler(request: web.Request) -> web.Response:
    """Write a single register to Venus OS Modbus TCP.

    Body: {"register": 2706, "value": 100}
    Only allows known safe registers (ESS settings).
    """
    ALLOWED_REGISTERS = {2704, 2706, 2707, 2708}  # MaxDischargePower, MaxFeedIn, OvervoltageFeedIn, PreventFeedback

    try:
        body = await request.json()
    except Exception:
        return web.json_response(
            {"success": False, "error": "Invalid JSON body"}, status=400,
        )

    register = body.get("register")
    value = body.get("value")

    if register not in ALLOWED_REGISTERS:
        return web.json_response(
            {"success": False, "error": f"Register {register} not writable"}, status=400,
        )

    venus_cfg = request.app["config"].venus
    if not venus_cfg.host:
        return web.json_response(
            {"success": False, "error": "Venus OS not configured"}, status=503,
        )

    try:
        # Handle signed int16
        raw = int(value)
        if raw < 0:
            raw = raw + 65536

        # Through the host's gateway like every other Modbus access: one lock per
        # host (parallel Venus requests queue), QUIESCE honoured, and refused when
        # venus.host is (an alias of) a configured inverter - the Venus write must
        # never become a second session to a single-session inverter.
        try:
            gateway = get_gateway(venus_cfg.host)
            async with gateway.probe_transaction(
                502, timeout=3.0, retries=0, refuse_claimed=True,
            ) as t:
                result = await t.write_register(register, raw, 100)
        except GatewayHostClaimed:
            return web.json_response(
                {"success": False,
                 "error": "venus.host is a configured inverter - write refused"},
                status=409,
            )
        except GatewayError as e:
            return web.json_response(
                {"success": False, "error": f"Cannot reach Venus OS: {e}"}, status=502,
            )

        if result.isError():
            return web.json_response(
                {"success": False, "error": f"Write failed: {result}"}, status=500,
            )

        return web.json_response({"success": True})
    except Exception as e:
        return web.json_response(
            {"success": False, "error": str(e)}, status=500,
        )


def _mqtt_write_venus(host: str, port: int, portal_id: str, path: str, value) -> bool:
    """Write a dbus value to Venus OS via MQTT (for values Modbus can't set)."""
    import socket, struct, json as _json
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect((host, port))
        # MQTT CONNECT
        cid = b"pv-proxy"
        payload = struct.pack("!H", 4) + b"MQTT" + bytes([4, 2, 0, 60])
        payload += struct.pack("!H", len(cid)) + cid
        s.send(bytes([0x10, len(payload)]) + payload)
        connack = s.recv(4)  # CONNACK
        if len(connack) < 4 or connack[3] != 0:
            s.close()
            return False
        # MQTT PUBLISH
        topic = f"W/{portal_id}/settings/0{path}".encode()
        msg = _json.dumps({"value": value}).encode()
        rem = 2 + len(topic) + len(msg)
        hdr = bytearray([0x30])
        while rem > 0:
            b = rem % 128
            rem //= 128
            if rem > 0:
                b |= 0x80
            hdr.append(b)
        s.send(bytes(hdr) + struct.pack("!H", len(topic)) + topic + msg)
        time.sleep(0.5)
        s.send(bytes([0xE0, 0x00]))  # DISCONNECT
        s.close()
        return True
    except Exception:
        return False


async def venus_dbus_handler(request: web.Request) -> web.Response:
    """Write a dbus value to Venus OS via MQTT (for values Modbus can't handle)."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"success": False, "error": "Invalid JSON"}, status=400)

    path = body.get("path")
    value = body.get("value")
    ALLOWED_PATHS = {
        "/Settings/CGwacs/MaxDischargePower",
        "/Settings/CGwacs/MaxFeedInPower",
        "/Settings/CGwacs/PreventFeedback",
        "/Settings/CGwacs/OvervoltageFeedIn",
    }
    if path not in ALLOWED_PATHS:
        return web.json_response({"success": False, "error": f"Path not allowed"}, status=400)

    venus_cfg = request.app["config"].venus
    if not venus_cfg.host or not venus_cfg.portal_id:
        return web.json_response(
            {"success": False, "error": "Venus OS MQTT not configured"}, status=503,
        )
    ok = _mqtt_write_venus(venus_cfg.host, venus_cfg.port, venus_cfg.portal_id, path, value)
    return web.json_response({"success": ok, "error": None if ok else "MQTT write failed"})


async def venus_lock_handler(request: web.Request) -> web.Response:
    """Handle Venus OS lock toggle commands.

    Actions: "lock" (lock for 15 min), "unlock" (unlock immediately).
    Returns 400 on invalid JSON or unknown action.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response(
            {"success": False, "error": "Invalid JSON body"}, status=400,
        )

    action = body.get("action")
    if action not in ("lock", "unlock"):
        return web.json_response(
            {"success": False, "error": f"Unknown action: {action}"}, status=400,
        )

    log.info("user_action", action="venus_control", control_action=action)

    app_ctx = request.app["app_ctx"]
    control = app_ctx.control_state
    override_log = app_ctx.override_log

    if action == "lock":
        permanent = body.get("permanent", False)
        control.lock(duration_s=0 if permanent else 900.0)
        # Reset limit to 100% so inverter runs at full power while locked
        control.update_wmaxlimpct(100)
        control.update_wmaxlim_ena(0)
        control.last_source = "none"
        control.save_ui_state()
        if override_log is not None:
            override_log.append("webapp", "lock", None, "Venus OS writes blocked, limit reset to 100%")
    else:
        control.unlock()
        control.save_ui_state()
        if override_log is not None:
            override_log.append("webapp", "unlock", None, "Venus OS writes unblocked")

    return web.json_response({"success": True})

