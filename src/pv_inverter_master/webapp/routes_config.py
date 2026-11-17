"""Configuration routes: read, save (with hot-reload), test-connect,
YAML export and import.
"""
from __future__ import annotations

import asyncio
import dataclasses
import os
import re
import tempfile
from datetime import datetime

import structlog
import yaml
from aiohttp import web

from pv_inverter_master.config import (
    Config,
    InverterEntry,
    _generate_id,
    get_active_inverter,
    save_config,
    validate_inverter_config,
    validate_venus_config,
)

# Inverter types the import handler will accept.
_VALID_INVERTER_TYPES = {"solaredge", "opendtu", "shelly", "sungrow", "hoymiles", "senec"}
from pv_inverter_master.influx_writer import influx_write_loop
from pv_inverter_master.modbus_gateway import GatewayError, probe_read
from pv_inverter_master.mqtt_publisher import mqtt_publish_loop
from pv_inverter_master.venus_reader import venus_mqtt_loop

from .broadcasts import broadcast_device_list
from .reconfigure import _reconfigure_active, _reload_location_mqtt

log = structlog.get_logger()


async def config_get_handler(request: web.Request) -> web.Response:
    """Return current inverter list and Venus OS configuration."""
    config: Config = request.app["config"]
    active = get_active_inverter(config)
    items = []
    for inv in config.inverters:
        d = dataclasses.asdict(inv)
        d["active"] = (active is not None and inv.id == active.id)
        items.append(d)
    return web.json_response({
        "inverters": items,
        "venus": {
            "name": config.venus.name,
            "host": config.venus.host,
            "port": config.venus.port,
            "portal_id": config.venus.portal_id,
        },
        "mqtt_publish": {
            "enabled": config.mqtt_publish.enabled,
            "host": config.mqtt_publish.host,
            "port": config.mqtt_publish.port,
            "topic_prefix": config.mqtt_publish.topic_prefix,
            "interval_s": config.mqtt_publish.interval_s,
        },
        "influx": {
            "enabled": config.influx.enabled,
            "url": config.influx.url,
            "token": config.influx.token,
            "org": config.influx.org,
            "bucket": config.influx.bucket,
            "interval_s": config.influx.interval_s,
        },
        "virtual_inverter": {
            "name": config.virtual_inverter.name,
            "location_ids": config.virtual_inverter.location_ids,
        },
    })


async def config_save_handler(request: web.Request) -> web.Response:
    """Save inverter and Venus OS settings, trigger hot-reload as needed.

    Accepts two inverter formats:
    - Old: {"inverter": {"host": ..., "port": ..., "unit_id": ...}} -- updates active entry
    - New: {"inverters": [...]} -- replaces entire inverter list
    """
    try:
        body = await request.json()
    except (ValueError, TypeError) as e:
        return web.json_response(
            {"success": False, "error": f"Invalid request: {e}"},
            status=400,
        )

    config: Config = request.app["config"]

    # --- Handle inverter(s) ---
    if "inverters" in body:
        # New multi-inverter format: replace entire list.
        # CRITICAL: preserve every InverterEntry field — older revisions of this
        # handler whitelisted only ~9 fields and silently wiped type, location_id,
        # rated_power, throttle_*, panel_power_wp, strings, gateway_*, shelly_*,
        # name, info, poll_interval and aggregate on every config save.
        raw_list = body["inverters"]
        existing_by_id = {e.id: e for e in config.inverters}
        new_entries = []
        for raw in raw_list:
            host = raw.get("host", "")
            port = raw.get("port", 1502)
            unit_id = raw.get("unit_id", 1)
            error = validate_inverter_config(host, port, unit_id)
            if error:
                return web.json_response(
                    {"success": False, "error": error},
                    status=400,
                )

            raw_id = raw.get("id") or ""
            existing = existing_by_id.get(raw_id) if raw_id else None
            if existing is None:
                entry = InverterEntry(host=host, port=port, unit_id=unit_id)
                if raw_id:
                    entry.id = raw_id
            else:
                entry = existing
                entry.host = host
                entry.port = port
                entry.unit_id = unit_id

            # Generic field-by-field merge across the whole dataclass schema.
            # Only fields present in `raw` are applied; everything else
            # keeps the existing value.
            for fname in InverterEntry.__dataclass_fields__:
                if fname in ("host", "port", "unit_id", "id"):
                    continue
                if fname in raw and raw[fname] is not None:
                    setattr(entry, fname, raw[fname])

            new_entries.append(entry)
        config.inverters = new_entries
    elif "inverter" in body:
        # Old single-inverter format: update the active entry
        inv = body["inverter"]
        try:
            host = inv["host"]
            port = inv["port"]
            unit_id = inv["unit_id"]
        except (KeyError, TypeError) as e:
            return web.json_response(
                {"success": False, "error": f"Invalid request: {e}"},
                status=400,
            )
        error = validate_inverter_config(host, port, unit_id)
        if error:
            return web.json_response(
                {"success": False, "error": error},
                status=400,
            )
        active = get_active_inverter(config)
        if active:
            active.host = host
            active.port = port
            active.unit_id = unit_id
        else:
            # No active inverter, create one
            config.inverters.append(InverterEntry(host=host, port=port, unit_id=unit_id))

    # --- Parse venus config ---
    # Defensive parse: only fields explicitly present in the POST body are
    # candidates for update. A POST missing the venus section keeps the
    # current venus.host/port/portal_id intact — earlier revisions of this
    # handler silently wiped them whenever the body lacked a venus object
    # (e.g. when the webapp's Standort-Selector saved virtual_inverter only).
    venus_body_raw = body.get("venus")
    venus_has_body = isinstance(venus_body_raw, dict)
    venus_body = venus_body_raw if venus_has_body else {}

    venus_name = venus_body.get("name", config.venus.name) if venus_has_body else config.venus.name
    venus_host = venus_body.get("host", config.venus.host) if venus_has_body else config.venus.host
    venus_port = venus_body.get("port", config.venus.port) if venus_has_body else config.venus.port
    venus_portal_id = venus_body.get("portal_id", config.venus.portal_id) if venus_has_body else config.venus.portal_id

    # Only validate when caller is actually setting venus values (otherwise an
    # unrelated POST would fail validation against the current state).
    if venus_has_body:
        error = validate_venus_config(venus_host, venus_port)
        if error:
            return web.json_response(
                {"success": False, "error": error},
                status=400,
            )

    # --- Parse mqtt_publish config ---
    mqtt_pub_body = body.get("mqtt_publish", {})

    try:
        app_ctx = request.app["app_ctx"]

        # Detect venus config change
        old_venus = (config.venus.host, config.venus.port, config.venus.portal_id)
        new_venus = (venus_host, venus_port, venus_portal_id)
        venus_changed = old_venus != new_venus

        # Detect mqtt_publish config change
        old_mqtt_pub = (config.mqtt_publish.enabled, config.mqtt_publish.host,
                        config.mqtt_publish.port, config.mqtt_publish.topic_prefix,
                        config.mqtt_publish.interval_s, config.mqtt_publish.client_id)

        # Update mqtt_publish config fields
        if mqtt_pub_body:
            for k, v in mqtt_pub_body.items():
                if k in config.mqtt_publish.__dataclass_fields__:
                    setattr(config.mqtt_publish, k, v)

        new_mqtt_pub = (config.mqtt_publish.enabled, config.mqtt_publish.host,
                        config.mqtt_publish.port, config.mqtt_publish.topic_prefix,
                        config.mqtt_publish.interval_s, config.mqtt_publish.client_id)
        mqtt_publish_changed = old_mqtt_pub != new_mqtt_pub

        # Detect + apply influx config change
        influx_body = body.get("influx", {})
        old_influx = (config.influx.enabled, config.influx.url, config.influx.token,
                      config.influx.org, config.influx.bucket, config.influx.interval_s)
        if influx_body:
            for k, v in influx_body.items():
                if k in config.influx.__dataclass_fields__:
                    setattr(config.influx, k, v)
        new_influx = (config.influx.enabled, config.influx.url, config.influx.token,
                      config.influx.org, config.influx.bucket, config.influx.interval_s)
        influx_changed = old_influx != new_influx

        # Update venus config
        config.venus.name = venus_name
        config.venus.host = venus_host
        config.venus.port = venus_port
        config.venus.portal_id = venus_portal_id

        # Apply virtual_inverter config (name + aggregate Standort scope)
        vi_body = body.get("virtual_inverter")
        vi_changed = False
        if isinstance(vi_body, dict):
            old_vi = (config.virtual_inverter.name, tuple(config.virtual_inverter.location_ids))
            if isinstance(vi_body.get("name"), str):
                config.virtual_inverter.name = vi_body["name"]
            if isinstance(vi_body.get("location_ids"), list):
                config.virtual_inverter.location_ids = [str(x) for x in vi_body["location_ids"]]
            vi_changed = old_vi != (
                config.virtual_inverter.name,
                tuple(config.virtual_inverter.location_ids),
            )

        save_config(request.app["config_path"], config)
        log.info("user_action", action="config_saved", venus_changed=venus_changed, venus_host=venus_host)

        # Hot-reload active inverter
        await _reconfigure_active(request.app, config)

        # Hot-reload Venus MQTT if config changed
        if venus_changed:
            # Cancel old venus task if running
            old_task = app_ctx.venus_task
            if old_task is not None and not old_task.done():
                old_task.cancel()

            if venus_host:
                # Start new venus MQTT loop
                app_ctx.venus_task = asyncio.create_task(
                    venus_mqtt_loop(app_ctx, venus_host, venus_port, venus_portal_id)
                )
            else:
                # Venus disabled -- clear state
                app_ctx.venus_mqtt_connected = False
                app_ctx.venus_task = None

        # Hot-reload MQTT publisher if config changed
        if mqtt_publish_changed:
            old_task = app_ctx.mqtt_pub_task
            if old_task is not None and not old_task.done():
                old_task.cancel()
                try:
                    await old_task
                except asyncio.CancelledError:
                    pass

            if config.mqtt_publish.enabled:
                app_ctx.mqtt_pub_queue = asyncio.Queue(maxsize=100)
                app_ctx.mqtt_pub_task = asyncio.create_task(
                    mqtt_publish_loop(
                        app_ctx, config.mqtt_publish,
                        inverters=config.inverters,
                        virtual_name=config.virtual_inverter.name,
                    )
                )
                log.info("user_action", action="mqtt_publisher_reloaded", host=config.mqtt_publish.host)
            else:
                app_ctx.mqtt_pub_connected = False
                app_ctx.mqtt_pub_task = None
                app_ctx.mqtt_pub_queue = None
                log.info("user_action", action="mqtt_publisher_disabled")

            # The Standort coordinate subscriber rides the same broker —
            # restart it so a changed host/port/prefix is picked up.
            await _reload_location_mqtt(request.app, config)

        # Hot-reload InfluxDB logger if config changed
        if influx_changed:
            old_task = app_ctx.influx_task
            if old_task is not None and not old_task.done():
                old_task.cancel()
                try:
                    await old_task
                except asyncio.CancelledError:
                    pass

            if config.influx.enabled:
                app_ctx.influx_queue = asyncio.Queue(maxsize=400)
                app_ctx.influx_task = asyncio.create_task(
                    influx_write_loop(app_ctx, config.influx)
                )
                log.info("user_action", action="influx_writer_reloaded", url=config.influx.url)
            else:
                app_ctx.influx_connected = False
                app_ctx.influx_task = None
                app_ctx.influx_queue = None
                log.info("user_action", action="influx_writer_disabled")

        # An aggregate-scope change retunes which inverters feed the virtual
        # Fronius identity — push a fresh device list so the UI reflects it.
        if vi_changed:
            await broadcast_device_list(request.app)

        return web.json_response({"success": True})
    except Exception as e:
        request.app["reconfiguring"] = False
        return web.json_response(
            {"success": False, "error": str(e)},
            status=500,
        )


async def config_test_handler(request: web.Request) -> web.Response:
    """Test-connect to a Modbus address without saving."""
    try:
        body = await request.json()
        host = body["host"]
        port = body["port"]
        unit_id = body["unit_id"]
    except (KeyError, ValueError) as e:
        return web.json_response(
            {"success": False, "error": f"Invalid request: {e}"},
            status=400,
        )

    error = validate_inverter_config(host, port, unit_id)
    if error:
        return web.json_response({"success": False, "error": error}, status=400)

    # Through the host's gateway, never a client of our own: for a configured
    # (active) host this reuses the plugin's single session and queues on its
    # lock; during QUIESCE it is refused. A second TCP session next to the
    # SE30K's only one is what used to wedge it.
    try:
        result = await probe_read(host, port, unit_id, 40000, 2, timeout=5)
        if result.isError():
            return web.json_response({
                "success": False,
                "error": f"Modbus read error: {result}",
            })
        return web.json_response({"success": True, "error": None})
    except GatewayError as e:
        return web.json_response({"success": False, "error": f"Gateway: {e}"})
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)})


async def config_export_handler(request: web.Request) -> web.Response:
    """Export current config as YAML file download."""
    config_path = request.app["config_path"]
    try:
        with open(config_path) as f:
            yaml_content = f.read()
    except FileNotFoundError:
        return web.json_response({"error": "Config file not found"}, status=404)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{ts}_pv-inverter-master.yaml"
    return web.Response(
        body=yaml_content,
        content_type="application/x-yaml",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


async def config_import_handler(request: web.Request) -> web.Response:
    """Import config from uploaded YAML file. Validates, saves, restarts."""
    try:
        data = await request.read()
        yaml_text = data.decode("utf-8")
        parsed = yaml.safe_load(yaml_text)
        if not isinstance(parsed, dict):
            return web.json_response({"success": False, "error": "Invalid YAML structure"}, status=400)
    except Exception as e:
        return web.json_response({"success": False, "error": f"YAML parse error: {e}"}, status=400)

    # Write to config file
    config_path = request.app["config_path"]
    try:
        dir_name = os.path.dirname(config_path)
        fd, tmp = tempfile.mkstemp(dir=dir_name, suffix=".yaml")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(yaml_text)
            os.replace(tmp, config_path)
        except Exception:
            os.unlink(tmp)
            raise
    except Exception as e:
        return web.json_response({"success": False, "error": f"Write failed: {e}"}, status=500)

    # Reload config
    try:
        from pv_inverter_master.config import load_config
        new_config = load_config(config_path)
        request.app["config"] = new_config
        app_ctx = request.app["app_ctx"]
        app_ctx.config = new_config
        log.info("user_action", action="config_imported")
    except Exception as e:
        return web.json_response({"success": False, "error": f"Reload failed: {e}"}, status=500)

    return web.json_response({"success": True, "message": "Config imported. Restart recommended."})


def _inverter_export_filename(entry: InverterEntry) -> str:
    """Build a filesystem-safe download filename for a single inverter."""
    label = entry.name or entry.model or entry.serial or entry.id
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", label).strip("-") or entry.id
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{ts}_inverter_{safe}.yaml"


async def inverter_export_handler(request: web.Request) -> web.Response:
    """Export a single inverter as a downloadable YAML file.

    Companion to ``inverter_import_handler``: the user can pull one
    configured inverter out of the app onto their computer and feed it
    back (here or on another instance) without touching the rest of the
    config. The whole InverterEntry is serialised, so a re-import restores
    the device exactly — including gateway credentials.
    """
    device_id = request.match_info.get("id", "")
    config: Config = request.app["config"]
    entry = next((e for e in config.inverters if e.id == device_id), None)
    if entry is None:
        return web.json_response({"error": "inverter_not_found"}, status=404)

    payload = {
        "_export": "pv-inverter-master-inverter",
        "version": 1,
        "inverter": dataclasses.asdict(entry),
    }
    yaml_content = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
    return web.Response(
        body=yaml_content,
        content_type="application/x-yaml",
        headers={"Content-Disposition": f"attachment; filename={_inverter_export_filename(entry)}"},
    )


def _extract_inverter_dict(parsed: object) -> dict | None:
    """Pull a single inverter mapping out of a parsed import document.

    Accepts the export wrapper ``{"inverter": {...}}``, a bare inverter
    mapping, or a ``{"inverters": [...]}`` list (first entry) so files
    produced by either the single- or whole-config export both work.
    """
    if not isinstance(parsed, dict):
        return None
    if isinstance(parsed.get("inverter"), dict):
        return parsed["inverter"]
    inverters = parsed.get("inverters")
    if isinstance(inverters, list) and inverters and isinstance(inverters[0], dict):
        return inverters[0]
    if "host" in parsed or "type" in parsed:
        return parsed
    return None


async def inverter_import_handler(request: web.Request) -> web.Response:
    """Import a single inverter from an uploaded YAML/JSON file.

    A new inverter is appended and started immediately; an upload whose id
    matches an existing inverter updates that entry in place and restarts
    it. Only known InverterEntry fields are accepted; everything else is
    ignored. No full restart — just a targeted device (re)start.
    """
    try:
        raw = await request.read()
        # yaml.safe_load parses JSON too, so one path covers both formats.
        parsed = yaml.safe_load(raw.decode("utf-8"))
    except Exception as e:
        return web.json_response({"success": False, "error": f"Parse error: {e}"}, status=400)

    inv = _extract_inverter_dict(parsed)
    if inv is None:
        return web.json_response(
            {"success": False, "error": "No inverter found in file"}, status=400
        )

    itype = inv.get("type", "")
    if itype and itype not in _VALID_INVERTER_TYPES:
        return web.json_response(
            {"success": False, "error": f"Invalid inverter type: {itype}"}, status=400
        )

    host = inv.get("host", "")
    error = validate_inverter_config(host, inv.get("port", 1502), inv.get("unit_id", 1))
    if error:
        return web.json_response({"success": False, "error": error}, status=400)

    # Keep only real dataclass fields with a value — defends against
    # hand-edited or foreign files carrying junk keys.
    fields = set(InverterEntry.__dataclass_fields__)
    clean = {k: v for k, v in inv.items() if k in fields and v is not None}

    config: Config = request.app["config"]
    incoming_id = str(clean.get("id") or "")
    existing = next((e for e in config.inverters if e.id == incoming_id), None) if incoming_id else None

    if existing is not None:
        for k, v in clean.items():
            if k == "id":
                continue
            setattr(existing, k, v)
        entry = existing
        is_new = False
    else:
        entry = InverterEntry(host=host)
        for k, v in clean.items():
            setattr(entry, k, v)
        # Avoid colliding with an existing id (or a missing one).
        if not entry.id or any(e.id == entry.id for e in config.inverters):
            entry.id = _generate_id()
        config.inverters.append(entry)
        is_new = True

    try:
        save_config(request.app["config_path"], config)
        # Targeted (re)start so the imported device goes live without a
        # full-process restart. Stop first for an update so the plugin is
        # rebuilt with the new connection params.
        if not is_new:
            await _reconfigure_active(request.app, config, device_id=entry.id, action="stop")
        await _reconfigure_active(request.app, config, device_id=entry.id, action="start")
        log.info("user_action", action="inverter_imported", device_id=entry.id, is_new=is_new)
    except Exception as e:
        return web.json_response({"success": False, "error": f"Apply failed: {e}"}, status=500)

    return web.json_response({
        "success": True,
        "device_id": entry.id,
        "name": entry.name or entry.model or entry.id,
        "is_new": is_new,
    })

