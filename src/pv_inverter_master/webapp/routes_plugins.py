"""Plugin-specific device routes: OpenDTU status/inverters/power,
Shelly relay switching and Hoymiles status/power.
"""
from __future__ import annotations

import aiohttp
import structlog
from aiohttp import web

from pv_inverter_master.config import Config
from pv_inverter_master.plugins.hoymiles import HoymilesPlugin
from pv_inverter_master.plugins.opendtu import OpenDTUPlugin
from pv_inverter_master.plugins.shelly import ShellyPlugin

log = structlog.get_logger()


async def opendtu_status_handler(request: web.Request) -> web.Response:
    """Get OpenDTU inverter status (producing, reachable, limits)."""
    device_id = request.match_info["id"]
    app_ctx = request.app["app_ctx"]
    ds = app_ctx.devices.get(device_id)
    if not ds or not ds.plugin:
        return web.json_response({"error": "Device not found"}, status=404)

    if not isinstance(ds.plugin, OpenDTUPlugin):
        return web.json_response({"error": "Not an OpenDTU device"}, status=400)

    status = await ds.plugin.get_inverter_status()
    return web.json_response(status)


async def opendtu_inverters_handler(request: web.Request) -> web.Response:
    """GET /api/devices/{id}/opendtu/inverters — list the gateway's inverters.

    Lets the config UI offer a serial picker: an OpenDTU gateway can host
    several inverters and the device entry must name exactly one by serial.
    """
    device_id = request.match_info["id"]
    config: Config = request.app["config"]
    entry = next((i for i in config.inverters if i.id == device_id), None)
    if entry is None or entry.type != "opendtu":
        return web.json_response({"ok": False, "error": "Not an OpenDTU device"})

    gw_host = entry.gateway_host or entry.host
    user = entry.gateway_user or "admin"
    password = entry.gateway_password or "openDTU42"
    url = f"http://{gw_host}/api/livedata/status"
    try:
        async with aiohttp.ClientSession(auth=aiohttp.BasicAuth(user, password)) as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                data = await resp.json()
    except Exception as e:
        return web.json_response({"ok": False, "error": f"Gateway nicht erreichbar: {e}"})

    raw = data.get("inverters", []) if isinstance(data, dict) else []
    inverters = [
        {
            "serial": str(i.get("serial", "")),
            "name": str(i.get("name", "") or i.get("serial", "")),
        }
        for i in raw
        if isinstance(i, dict) and i.get("serial")
    ]
    return web.json_response({"ok": True, "inverters": inverters})


async def opendtu_power_handler(request: web.Request) -> web.Response:
    """Send power on/off/restart to an OpenDTU inverter.

    Body: {"action": "on"|"off"|"restart"}
    """
    device_id = request.match_info["id"]
    app_ctx = request.app["app_ctx"]
    ds = app_ctx.devices.get(device_id)
    if not ds or not ds.plugin:
        return web.json_response({"success": False, "error": "Device not found"}, status=404)

    if not isinstance(ds.plugin, OpenDTUPlugin):
        return web.json_response({"success": False, "error": "Not an OpenDTU device"}, status=400)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"success": False, "error": "Invalid JSON"}, status=400)

    action = body.get("action", "")
    if action not in ("on", "off", "restart"):
        return web.json_response({"success": False, "error": "action must be on/off/restart"}, status=400)

    result = await ds.plugin.send_power_command(action)
    log.info("user_action", action=f"opendtu_power_{action}", device_id=device_id)
    return web.json_response({"success": result.success, "error": result.error})


async def shelly_switch_handler(request: web.Request) -> web.Response:
    """Send on/off command to a Shelly device relay.

    Body: {"on": true|false}
    """
    device_id = request.match_info["id"]
    app_ctx = request.app["app_ctx"]
    ds = app_ctx.devices.get(device_id)
    if not ds or not ds.plugin:
        return web.json_response({"success": False, "error": "Device not found"}, status=404)

    if not isinstance(ds.plugin, ShellyPlugin):
        return web.json_response({"success": False, "error": "Not a Shelly device"}, status=400)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"success": False, "error": "Invalid JSON"}, status=400)

    on = body.get("on")
    if on is None or not isinstance(on, bool):
        return web.json_response({"success": False, "error": "'on' must be true or false"}, status=400)

    success = await ds.plugin.switch(on)
    log.info("user_action", action=f"shelly_switch_{'on' if on else 'off'}", device_id=device_id)
    return web.json_response({"success": success})


async def opendtu_test_auth_handler(request: web.Request) -> web.Response:
    """Test OpenDTU gateway connectivity and auth.

    Body: {"host": "198.51.100.100", "user": "admin", "password": "openDTU42"}
    Returns: {"success": true/false, "error": "...", "inverters": [...]}
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"success": False, "error": "Invalid JSON"}, status=400)

    host = body.get("host", "")
    user = body.get("user", "admin")
    password = body.get("password", "openDTU42")

    if not host:
        return web.json_response({"success": False, "error": "Host required"}, status=400)

    try:
        auth = aiohttp.BasicAuth(user, password)
        async with aiohttp.ClientSession(auth=auth) as session:
            async with session.get(f"http://{host}/api/livedata/status", timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 401:
                    return web.json_response({"success": False, "error": "Authentication failed — check username/password"})
                data = await resp.json()
                inverters = []
                for inv in data.get("inverters", []):
                    inverters.append({
                        "serial": inv.get("serial", ""),
                        "name": inv.get("name", ""),
                        "producing": inv.get("producing", False),
                    })
                return web.json_response({"success": True, "inverters": inverters})
    except Exception as e:
        return web.json_response({"success": False, "error": f"Connection failed: {e}"})


async def hoymiles_status_handler(request: web.Request) -> web.Response:
    """Get live Hoymiles inverter status (reachable, producing, limit_pct)."""
    device_id = request.match_info["id"]
    app_ctx = request.app["app_ctx"]
    ds = app_ctx.devices.get(device_id)
    if not ds or not ds.plugin:
        return web.json_response({"error": "Device not found"}, status=404)
    if not isinstance(ds.plugin, HoymilesPlugin):
        return web.json_response({"error": "Not a Hoymiles device"}, status=400)
    return web.json_response(await ds.plugin.get_inverter_status())


async def hoymiles_power_handler(request: web.Request) -> web.Response:
    """Send power on/off/restart to a Hoymiles inverter.

    Body: {"action": "on"|"off"|"restart"}
    """
    device_id = request.match_info["id"]
    app_ctx = request.app["app_ctx"]
    ds = app_ctx.devices.get(device_id)
    if not ds or not ds.plugin:
        return web.json_response({"success": False, "error": "Device not found"}, status=404)
    if not isinstance(ds.plugin, HoymilesPlugin):
        return web.json_response({"success": False, "error": "Not a Hoymiles device"}, status=400)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"success": False, "error": "Invalid JSON"}, status=400)

    action = body.get("action", "")
    if action not in ("on", "off", "restart"):
        return web.json_response({"success": False, "error": "action must be on/off/restart"}, status=400)

    result = await ds.plugin.send_power_command(action)
    log.info("user_action", action=f"hoymiles_power_{action}", device_id=device_id)
    return web.json_response({"success": result.success, "error": result.error})

