"""Device + Standort hot-reload helpers.

``_reconfigure_active`` drives the DeviceRegistry on config changes;
``_reload_location_mqtt`` (re)starts the Standort-coordinate MQTT
subscriber. Both broadcast the refreshed lists to connected clients.
"""
from __future__ import annotations

import asyncio
import json

import structlog
from aiohttp import web

from pv_inverter_master.config import Config, get_active_inverter

from .broadcasts import broadcast_device_list, broadcast_location_list

log = structlog.get_logger()


async def _reconfigure_active(app: web.Application, config: Config, device_id: str = "", action: str = "") -> None:
    """Reconfigure devices via DeviceRegistry.

    Args:
        app: The aiohttp Application.
        config: Current Config object.
        device_id: Specific device to act on (for targeted start/stop).
        action: "start", "stop", "disable" -- what to do with device_id.
    """
    app_ctx = app["app_ctx"]
    registry = app_ctx.device_registry
    app["reconfiguring"] = True
    try:
        if registry is None:
            # Fallback: no registry available (should not happen in normal operation)
            log.warning("no_device_registry", msg="DeviceRegistry not available for reconfigure")
            return

        if action == "start" and device_id:
            await registry.start_device(device_id)
            app_ctx.polling_paused = False
        elif action == "stop" and device_id:
            await registry.stop_device(device_id)
        elif action == "disable" and device_id:
            await registry.disable_device(device_id)
        else:
            # Generic reconfigure: check if any active inverter exists
            active = get_active_inverter(config)
            if not active:
                # Stop all devices
                await registry.stop_all()
                app_ctx.polling_paused = True
                # Broadcast no_inverter event to connected clients
                clients = app.get("ws_clients")
                if clients:
                    payload = json.dumps({"type": "no_inverter"})
                    for ws in set(clients):
                        try:
                            await ws.send_str(payload)
                        except (ConnectionError, RuntimeError, ConnectionResetError):
                            clients.discard(ws)
                log.warning("no_active_inverter", msg="All inverters disabled or removed")

        # Check if we have active devices
        if registry.get_active_count() == 0 and not get_active_inverter(config):
            app_ctx.polling_paused = True
        else:
            app_ctx.polling_paused = False

        # Broadcast updated device list after any CRUD change
        await broadcast_device_list(app)
        await broadcast_location_list(app)
    finally:
        app["reconfiguring"] = False

async def _reload_location_mqtt(app: web.Application, config: Config) -> None:
    """Start/stop/restart the Standort-coordinate MQTT subscriber.

    The subscriber only runs when at least one Standort has
    ``mqtt_coords`` enabled. Any running task is cancelled and recreated
    so a changed broker host or topic prefix is picked up.
    """
    app_ctx = app["app_ctx"]
    needs = any(loc.mqtt_coords for loc in config.locations)

    task = getattr(app_ctx, "location_mqtt_task", None)
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        app_ctx.location_mqtt_task = None

    if needs:
        from pv_inverter_master.location_mqtt import location_mqtt_loop
        app_ctx.location_mqtt_task = asyncio.create_task(
            location_mqtt_loop(app_ctx, config.mqtt_publish)
        )
        log.info("location_mqtt_task_started", broker=config.mqtt_publish.host)


