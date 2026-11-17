"""Device-centric routes: device list, per-device snapshot, virtual
inverter snapshot and inverter CRUD.
"""
from __future__ import annotations

import dataclasses

import structlog
from aiohttp import web

from pv_inverter_master.config import (
    Config,
    InverterEntry,
    get_active_inverter,
    save_config,
    validate_inverter_config,
)
from pv_inverter_master.plugin import (
    ThrottleCaps,
    compute_throttle_score,
    get_throttle_caps,
)

from .payloads import _build_device_list, _build_virtual_contributions
from .reconfigure import _reconfigure_active


def _effective_throttle_score(app_ctx, device_id: str, caps: ThrottleCaps | None) -> float:
    """Return the throttle score that reflects the live-measured response
    time (set by Phasen-Detective into the distributor's
    ``DeviceLimitState.measured_response_time_s``) when available,
    falling back to the plugin's statically declared caps.

    The distributor already uses this effective value when deciding
    throttle waterfall order — surfacing it through the API keeps the
    UI honest about what the device actually does versus what the
    plugin's defaults claim.
    """
    if caps is None:
        return 0.0
    distributor = getattr(app_ctx, "distributor", None)
    if distributor is not None:
        states = getattr(distributor, "_device_states", None) or {}
        ds_limit = states.get(device_id)
        if ds_limit is not None and ds_limit.measured_response_time_s is not None:
            return compute_throttle_score(ThrottleCaps(
                mode=caps.mode,
                response_time_s=ds_limit.measured_response_time_s,
                cooldown_s=caps.cooldown_s,
                startup_delay_s=caps.startup_delay_s,
            ))
    return compute_throttle_score(caps)

log = structlog.get_logger()


async def devices_list_handler(request: web.Request) -> web.Response:
    """Return list of all devices: inverters + venus + virtual pseudo-devices."""
    config: Config = request.app["config"]
    app_ctx = request.app["app_ctx"]
    devices = _build_device_list(app_ctx, config)
    return web.json_response({"devices": devices})


async def device_snapshot_handler(request: web.Request) -> web.Response:
    """Return per-device dashboard snapshot."""
    device_id = request.match_info["id"]
    config: Config = request.app["config"]
    app_ctx = request.app["app_ctx"]

    # Find the inverter entry
    entry = None
    for inv in config.inverters:
        if inv.id == device_id:
            entry = inv
            break
    if entry is None:
        return web.json_response({"error": "Device not found"}, status=404)

    ds = app_ctx.devices.get(device_id)
    display_name = entry.name or f"{entry.manufacturer} {entry.model}".strip() or "Inverter"

    # Always include connection info, even without snapshot data
    conn_info = {
        "state": ds.conn_mgr.state.value if ds and ds.conn_mgr else "unknown",
        "poll_success": ds.poll_counter["success"] if ds else 0,
        "poll_total": ds.poll_counter["total"] if ds else 0,
    }

    # 260528-hhj: per-device poll health surface. Compute once and inject
    # into BOTH response branches (no-data + snapshot) so the UI's config
    # status card has a value regardless of whether the collector has a
    # first snapshot yet.
    health = {
        "last_poll_at": ds.last_poll_at if ds else None,
        "last_successful_poll_at": ds.last_successful_poll_at if ds else None,
        "last_poll_error": ds.last_poll_error if ds else None,
    }

    if ds is None or ds.collector is None or ds.collector.last_snapshot is None:
        return web.json_response({
            "error": "No data yet",
            "device_id": device_id,
            "device_type": entry.type,
            "display_name": display_name,
            "enabled": entry.enabled,
            "connection": conn_info,
            "inverter": {},
            "host": entry.host,
            "port": entry.port,
            "unit_id": entry.unit_id,
            "type": entry.type,
            "name": entry.name,
            "rated_power": entry.rated_power,
            "shelly_gen": entry.shelly_gen,
            "gateway_user": entry.gateway_user,
            "gateway_password": entry.gateway_password,
            "throttle_order": entry.throttle_order,
            "throttle_enabled": entry.throttle_enabled,
            "phase": entry.phase,
            "phase_explicit": entry.phase_explicit,
            "phase_detect_supported": __import__("pv_inverter_master.webapp.payloads", fromlist=["_phase_detect_supported"])._phase_detect_supported(config, entry),
            "throttle_mode": c.mode if ds and ds.plugin and (c := get_throttle_caps(ds.plugin)) else "none",
            "throttle_score": _effective_throttle_score(app_ctx, device_id, get_throttle_caps(ds.plugin) if ds and ds.plugin else None),
            "supports_reboot": hasattr(ds.plugin, "reboot") if (ds and ds.plugin) else False,
            # 260528-hhj: per-device poll health (no-data branch).
            **health,
        })

    snapshot = dict(ds.collector.last_snapshot)
    snapshot["device_id"] = device_id
    snapshot["device_type"] = entry.type
    snapshot["display_name"] = display_name
    snapshot["enabled"] = entry.enabled
    snapshot["connection"] = conn_info
    # Include config fields needed by the config page
    snapshot["host"] = entry.host
    snapshot["port"] = entry.port
    snapshot["unit_id"] = entry.unit_id
    snapshot["type"] = entry.type
    snapshot["name"] = entry.name
    snapshot["rated_power"] = entry.rated_power
    snapshot["shelly_gen"] = entry.shelly_gen
    snapshot["gateway_user"] = entry.gateway_user
    snapshot["gateway_password"] = entry.gateway_password
    snapshot["throttle_order"] = entry.throttle_order
    snapshot["throttle_enabled"] = entry.throttle_enabled
    snapshot["phase"] = entry.phase
    snapshot["phase_explicit"] = entry.phase_explicit
    from .payloads import _phase_detect_supported
    snapshot["phase_detect_supported"] = _phase_detect_supported(config, entry)
    snapshot["string_names"] = list(entry.string_names or [])
    from pv_inverter_master.webapp.payloads import strings_to_payload
    from pv_inverter_master.config import inverter_wp_total
    snapshot["strings"] = strings_to_payload(entry.strings)
    snapshot["panel_wp_total"] = inverter_wp_total(entry)
    caps = get_throttle_caps(ds.plugin if ds else None)
    snapshot["throttle_mode"] = caps.mode if caps else "none"
    snapshot["throttle_score"] = _effective_throttle_score(app_ctx, device_id, caps)
    snapshot["supports_reboot"] = hasattr(ds.plugin, "reboot") if (ds and ds.plugin) else False
    # 260528-hhj: per-device poll health (snapshot branch).
    for _hk, _hv in health.items():
        snapshot[_hk] = _hv
    # Surface plugin-specific live caches (per-string DC, SENEC status).
    if ds and ds.plugin:
        if getattr(ds.plugin, "dc_channels", None):
            snapshot["dc_channels"] = ds.plugin.dc_channels
        if getattr(ds.plugin, "senec_status", None):
            snapshot["senec_status"] = ds.plugin.senec_status
    return web.json_response(snapshot)


async def virtual_snapshot_handler(request: web.Request) -> web.Response:
    """Return aggregated virtual inverter snapshot with per-device contributions."""
    config: Config = request.app["config"]
    app_ctx = request.app["app_ctx"]

    total_power_w, total_rated_w, contributions = _build_virtual_contributions(app_ctx, config)

    # Include control limits from the virtual clamp stored in ControlState.
    # (VirtualInverterConfig only carries the display name — the actual clamp
    # lives in control_state.device_clamps["virtual"], populated by
    # /api/power-clamp.)
    cmin, cmax = app_ctx.control_state.get_device_clamp("virtual")
    control = {
        "clamp_min_pct": cmin,
        "clamp_max_pct": cmax,
    }

    return web.json_response({
        "total_power_w": total_power_w,
        "total_rated_w": total_rated_w,
        "rated_power_w": total_rated_w,
        "virtual_name": config.virtual_inverter.name,
        "contributions": contributions,
        "control": control,
    })


async def inverters_list_handler(request: web.Request) -> web.Response:
    """Return all inverter entries with active flag."""
    config: Config = request.app["config"]
    active = get_active_inverter(config)
    items = []
    for inv in config.inverters:
        d = dataclasses.asdict(inv)
        d["active"] = (active is not None and inv.id == active.id)
        items.append(d)
    return web.json_response({"inverters": items})


async def inverters_add_handler(request: web.Request) -> web.Response:
    """Add a new inverter entry."""
    try:
        body = await request.json()
        host = body["host"]
        port = body.get("port", 1502)
        unit_id = body.get("unit_id", 1)
    except (KeyError, ValueError, TypeError) as e:
        return web.json_response({"error": f"Invalid request: {e}"}, status=400)

    error = validate_inverter_config(host, port, unit_id)
    if error:
        return web.json_response({"error": error}, status=400)

    dev_type = body.get("type", "solaredge")
    entry = InverterEntry(
        host=host, port=port, unit_id=unit_id,
        enabled=body.get("enabled", True),
        manufacturer=body.get("manufacturer", ""),
        model=body.get("model", ""),
        serial=body.get("serial", ""),
        firmware_version=body.get("firmware_version", ""),
        name=body.get("name", ""),
        info=body.get("info", ""),
        poll_interval=body.get("poll_interval", 0.0),
        type=dev_type,
        gateway_host=body.get("gateway_host", host if dev_type == "opendtu" else ""),
        gateway_user=body.get("gateway_user", ""),
        gateway_password=body.get("gateway_password", ""),
        shelly_gen=body.get("shelly_gen", ""),
        shelly_channel=body.get("shelly_channel", 0),
        rated_power=body.get("rated_power", 0),
        panel_power_wp=body.get("panel_power_wp", 0),
        location_id=body.get("location_id", ""),
        throttle_enabled=body.get(
            "throttle_enabled",
            # All proportional-control types throttle by default; binary
            # and monitor-only types stay off until explicitly opted in.
            dev_type not in ("shelly", "sungrow", "senec"),
        ),
        aggregate=body.get("aggregate", True),
    )
    config: Config = request.app["config"]
    config.inverters.append(entry)
    save_config(request.app["config_path"], config)
    log.info("user_action", action="inverter_added", device_id=entry.id, host=host, port=port, unit_id=unit_id, name=entry.name, type=entry.type)

    # Start device immediately if enabled (per locked decision)
    if entry.enabled:
        await _reconfigure_active(request.app, config, device_id=entry.id, action="start")

    d = dataclasses.asdict(entry)
    active = get_active_inverter(config)
    d["active"] = (active is not None and entry.id == active.id)
    return web.json_response(d, status=201)


async def inverters_update_handler(request: web.Request) -> web.Response:
    """Update an existing inverter entry by id."""
    inv_id = request.match_info["id"]
    config: Config = request.app["config"]
    entry = None
    for inv in config.inverters:
        if inv.id == inv_id:
            entry = inv
            break
    if entry is None:
        return web.json_response({"error": "Inverter not found"}, status=404)

    try:
        body = await request.json()
    except (ValueError, TypeError) as e:
        return web.json_response({"error": f"Invalid request: {e}"}, status=400)

    # Snapshot connection-relevant fields so a metadata-only edit
    # (name, Standort, throttle, aggregate, ...) doesn't needlessly
    # restart the device poll loop.
    # serial is connection-relevant for OpenDTU — it selects which inverter
    # on the gateway is read, so a change must recreate the plugin.
    _CONN_FIELDS = ("host", "port", "unit_id", "serial", "gateway_host", "gateway_user", "gateway_password")
    conn_before = tuple(getattr(entry, f) for f in _CONN_FIELDS)

    was_enabled = entry.enabled
    poll_before = entry.poll_interval
    for field_name in ("host", "port", "unit_id", "enabled", "manufacturer", "model", "serial", "firmware_version", "rated_power", "panel_power_wp", "name", "info", "poll_interval", "gateway_host", "gateway_user", "gateway_password", "throttle_order", "throttle_enabled", "aggregate", "location_id", "shelly_channel"):
        if field_name in body:
            setattr(entry, field_name, body[field_name])

    # Phase: the dropdown sends an integer 0/1/2/3.
    #   0 → "L1 (Standard)" sentinel (default L1, no explicit assignment)
    #   1/2/3 → user explicitly picked this phase
    # The sentinel exists so a user who previously assigned (say) L2 can
    # revert to the default by selecting "L1 (Standard)" — without it
    # there's no way to un-flip phase_explicit from the UI.
    if "phase" in body:
        try:
            phase_val = int(body["phase"])
        except (TypeError, ValueError):
            return web.json_response({"error": "phase must be 0/1/2/3"}, status=400)
        if phase_val == 0:
            entry.phase = 1
            entry.phase_explicit = False
        elif phase_val in (1, 2, 3):
            entry.phase = phase_val
            entry.phase_explicit = True
        else:
            return web.json_response({"error": "phase must be 0/1/2/3"}, status=400)

    # Per-DC-string group names (display metadata — no poll restart).
    if "string_names" in body:
        raw = body["string_names"]
        if not isinstance(raw, list):
            return web.json_response(
                {"error": "string_names must be a list"}, status=400,
            )
        entry.string_names = [str(x).strip()[:64] for x in raw[:64]]
        # Mirror into strings[i].name for indices that already exist so the
        # new structured editor stays in sync with the dashboard quick-rename.
        from pv_inverter_master.config import StringSpec
        for i, nm in enumerate(entry.string_names):
            if i < len(entry.strings):
                entry.strings[i].name = nm
            elif nm:
                entry.strings.append(StringSpec(name=nm, panels=[]))

    # Panel inventory per string (count × Wp + optional brand/model).
    # Replace the whole strings list when present; clamped to 32 strings
    # × 32 panel groups so a runaway payload can't blow up the config.
    if "strings" in body:
        from pv_inverter_master.config import coerce_strings_in_place
        raw = body["strings"]
        if not isinstance(raw, list):
            return web.json_response(
                {"error": "strings must be a list"}, status=400,
            )
        cleaned: list[dict] = []
        for s in raw[:32]:
            if not isinstance(s, dict):
                continue
            panels: list[dict] = []
            for p in (s.get("panels") or [])[:32]:
                if not isinstance(p, dict):
                    continue
                panels.append({
                    "count": max(0, int(p.get("count", 0) or 0)),
                    "wp_each": max(0, int(p.get("wp_each", 0) or 0)),
                    "brand": str(p.get("brand", "") or "").strip()[:64],
                    "model": str(p.get("model", "") or "").strip()[:64],
                })
            cleaned.append({
                "name": str(s.get("name", "") or "").strip()[:64],
                "panels": panels,
            })
        entry.strings = cleaned
        coerce_strings_in_place(entry)
        # Keep the legacy panel_power_wp + string_names fields in sync
        # so back-compat consumers (Standort view, older API readers)
        # still see consistent values. Use a direct sum (not the
        # fallback-to-legacy inverter_wp_total) so clearing strings
        # actually clears the Wp total.
        entry.panel_power_wp = sum(s.wp_total for s in entry.strings)
        entry.string_names = [s.name for s in entry.strings]

    conn_changed = conn_before != tuple(getattr(entry, f) for f in _CONN_FIELDS)
    # A changed poll interval is only picked up when the poll loop restarts.
    poll_changed = entry.poll_interval != poll_before

    error = validate_inverter_config(entry.host, entry.port, entry.unit_id)
    if error:
        return web.json_response({"error": error}, status=400)

    # Determine what changed for logging
    changes = {k: body[k] for k in body if k in ("host", "port", "unit_id", "enabled", "name", "rated_power", "throttle_order", "throttle_enabled", "aggregate")}
    if was_enabled and not entry.enabled:
        log.info("user_action", action="inverter_disabled", device_id=inv_id, name=entry.name or entry.model)
    elif not was_enabled and entry.enabled:
        log.info("user_action", action="inverter_enabled", device_id=inv_id, name=entry.name or entry.model)
    else:
        log.info("user_action", action="inverter_updated", device_id=inv_id, changes=changes)

    save_config(request.app["config_path"], config)

    # Handle enable/disable transitions via DeviceRegistry
    if was_enabled and not entry.enabled:
        await _reconfigure_active(request.app, config, device_id=inv_id, action="disable")
    elif not was_enabled and entry.enabled:
        await _reconfigure_active(request.app, config, device_id=inv_id, action="start")
    elif was_enabled and entry.enabled and (conn_changed or poll_changed):
        # Connection settings or poll interval changed -- restart device
        await _reconfigure_active(request.app, config, device_id=inv_id, action="stop")
        await _reconfigure_active(request.app, config, device_id=inv_id, action="start")
    else:
        # Metadata-only change (name, Standort, throttle, aggregate) --
        # no poll-loop restart; just refresh the sidebar + Standort list.
        await _reconfigure_active(request.app, config)

    d = dataclasses.asdict(entry)
    active = get_active_inverter(config)
    d["active"] = (active is not None and entry.id == active.id)
    return web.json_response(d)


async def inverters_delete_handler(request: web.Request) -> web.Response:
    """Delete an inverter entry by id."""
    inv_id = request.match_info["id"]
    config: Config = request.app["config"]

    original_len = len(config.inverters)
    config.inverters = [inv for inv in config.inverters if inv.id != inv_id]
    if len(config.inverters) == original_len:
        return web.json_response({"error": "Inverter not found"}, status=404)

    save_config(request.app["config_path"], config)
    log.info("user_action", action="inverter_deleted", device_id=inv_id)

    # Stop the device via DeviceRegistry
    await _reconfigure_active(request.app, config, device_id=inv_id, action="stop")

    return web.json_response({"success": True})


async def inverter_reboot_handler(request: web.Request) -> web.Response:
    """Trigger a plugin-specific reboot (e.g. Sungrow WiNet-S).

    Duck-typed: any plugin that declares an ``async def reboot()`` method
    returning a ``WriteResult`` opts in. Plugins without the method get a
    400 — the frontend should gate the button on the ``supports_reboot``
    flag in the device payload, so this is just a defensive guard.
    """
    device_id = request.match_info["id"]
    app_ctx = request.app["app_ctx"]
    ds = app_ctx.devices.get(device_id)
    if not ds or not ds.plugin:
        return web.json_response(
            {"success": False, "error": "Device not found"}, status=404,
        )
    if not hasattr(ds.plugin, "reboot"):
        return web.json_response(
            {"success": False, "error": "Reboot not supported for this device type"},
            status=400,
        )

    result = await ds.plugin.reboot()
    log.info(
        "user_action",
        action="inverter_reboot",
        device_id=device_id,
        success=result.success,
        error=result.error,
    )
    status_code = 200 if result.success else 500
    return web.json_response(
        {"success": result.success, "error": result.error},
        status=status_code,
    )

