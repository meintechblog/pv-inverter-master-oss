"""Payload builders for the webapp.

Pure functions that turn the live AppContext + Config into the JSON
structures pushed over the WebSocket and returned by the REST handlers:
device list, virtual-inverter contributions, Standort list.
"""
from __future__ import annotations

import time
from typing import Any

from pv_inverter_master.config import (
    Config,
    InverterEntry,
    inverter_counts_for_aggregate,
    inverter_wp_total,
)
from pv_inverter_master.plugin import compute_throttle_score, get_throttle_caps


def _phase_detect_supported(config: Config, inv: InverterEntry) -> bool:
    """True iff Phasen-Detective makes sense for this inverter.

    The detection algorithm watches the grid meter for per-phase Δ when
    the inverter is throttled. That only works when the inverter is
    physically connected to the same grid meter as the Venus we monitor.
    ``virtual_inverter.location_ids`` lists the Standorte that flow into
    the Venus aggregate — anything outside that list is at a different
    physical site (different grid meter, or no Venus integration) and a
    probe there would only stir up Multiplus rebalancing noise.
    Single-phase inverter types only (three-phase devices distribute
    themselves and don't need phase assignment).
    """
    if inv.type not in ("hoymiles", "opendtu", "shelly"):
        return False
    loc_ids = config.virtual_inverter.location_ids or []
    # Empty list = "all locations count" (matches the aggregation rule
    # in inverter_counts_for_aggregate). Otherwise membership-test.
    if not loc_ids:
        return True
    return inv.location_id in loc_ids


def strings_to_payload(strings: list) -> list[dict]:
    """Serialize an InverterEntry.strings list (StringSpec dataclasses)
    into plain dicts for JSON output. Tolerates raw-dict items too."""
    out: list[dict] = []
    for s in strings or []:
        if isinstance(s, dict):
            out.append({
                "name": str(s.get("name", "") or ""),
                "panels": [
                    {
                        "count": int(p.get("count", 0) or 0),
                        "wp_each": int(p.get("wp_each", 0) or 0),
                        "brand": str(p.get("brand", "") or ""),
                        "model": str(p.get("model", "") or ""),
                    }
                    for p in (s.get("panels") or [])
                    if isinstance(p, dict)
                ],
            })
            continue
        out.append({
            "name": getattr(s, "name", ""),
            "panels": [
                {
                    "count": int(getattr(p, "count", 0) or 0),
                    "wp_each": int(getattr(p, "wp_each", 0) or 0),
                    "brand": getattr(p, "brand", ""),
                    "model": getattr(p, "model", ""),
                }
                for p in getattr(s, "panels", []) or []
            ],
        })
    return out


def _build_virtual_contributions(
    app_ctx: Any, config: Config,
) -> tuple[float, float, list[dict]]:
    """Aggregate power and per-device contributions for the virtual inverter.

    Returns (total_power_w, total_rated_w, contributions).
    """
    total_power_w = 0
    total_rated_w = 0
    contributions: list[dict] = []
    distributor = (
        getattr(app_ctx, "device_registry", None)
        and app_ctx.device_registry.distributor
    )
    device_limits = distributor.get_device_limits() if distributor is not None else {}

    for inv in config.inverters:
        if not inverter_counts_for_aggregate(config, inv):
            continue
        ds = app_ctx.devices.get(inv.id)
        power_w = 0
        rated_w = inv.rated_power
        if ds and ds.collector and ds.collector.last_snapshot:
            snap_inv = ds.collector.last_snapshot.get("inverter", {})
            power_w = snap_inv.get("ac_power_w", 0)
            # Use snapshot rated_power_w if config has 0
            if not rated_w:
                rated_w = ds.collector.last_snapshot.get("rated_power_w", 0)
        total_power_w += power_w
        total_rated_w += rated_w
        display_name = inv.name or f"{inv.manufacturer} {inv.model}".strip() or "Inverter"
        contrib: dict = {
            "device_id": inv.id,
            "name": display_name,
            "power_w": power_w,
            "throttle_order": inv.throttle_order,
            "throttle_enabled": inv.throttle_enabled,
            "current_limit_pct": device_limits.get(inv.id, 100.0),
        }

        plugin = ds.plugin if ds else None
        caps = get_throttle_caps(plugin)
        throttle_score = compute_throttle_score(caps) if caps else 0.0
        throttle_mode = caps.mode if caps else "none"

        # Build score breakdown for UI transparency
        if caps and caps.mode != "none":
            base = 7.0 if caps.mode == "proportional" else 3.0
            response_bonus = round(max(0.0, 3.0 * (1.0 - caps.response_time_s / 10.0)), 2)
            cooldown_penalty = round(min(2.0, caps.cooldown_s / 150.0), 2)
            startup_penalty = round(min(1.0, caps.startup_delay_s / 30.0), 2)
            score_breakdown = {
                "base": base,
                "response_time_s": caps.response_time_s,
                "response_bonus": response_bonus,
                "cooldown_s": caps.cooldown_s,
                "cooldown_penalty": cooldown_penalty,
                "startup_delay_s": caps.startup_delay_s,
                "startup_penalty": startup_penalty,
            }
        else:
            score_breakdown = None

        display = distributor.get_device_display_state(inv.id) if distributor else None
        if display:
            contrib["throttle_score"] = throttle_score
            contrib["throttle_mode"] = throttle_mode
            contrib["score_breakdown"] = score_breakdown
            contrib["measured_response_time_s"] = display["measured_response_time_s"]
            contrib["relay_on"] = display["relay_on"]
            contrib["throttle_state"] = display["throttle_state"]
        else:
            contrib["throttle_score"] = throttle_score
            contrib["throttle_mode"] = throttle_mode
            contrib["score_breakdown"] = score_breakdown
            contrib["measured_response_time_s"] = None
            contrib["relay_on"] = True
            contrib["throttle_state"] = "active" if inv.throttle_enabled else "disabled"
        contributions.append(contrib)

    return total_power_w, total_rated_w, contributions


def _build_device_list(app_ctx: Any, config: Config) -> list[dict]:
    """Build the device list payload used by WS clients for sidebar + status dots."""
    devices = []
    for inv in config.inverters:
        ds = app_ctx.devices.get(inv.id)
        conn_state = "unknown"
        if ds and ds.conn_mgr:
            conn_state = ds.conn_mgr.state.value
        power_w = 0
        if ds and ds.collector and ds.collector.last_snapshot:
            snap_inv = ds.collector.last_snapshot.get("inverter", {})
            power_w = snap_inv.get("ac_power_w", 0)
        display_name = inv.name or f"{inv.manufacturer} {inv.model}".strip() or "Inverter"
        dev_entry: dict = {
            "id": inv.id,
            "name": display_name,
            "info": inv.info,
            "type": inv.type,
            "enabled": inv.enabled,
            "host": inv.host,
            "port": inv.port,
            "unit_id": inv.unit_id,
            "connection_state": conn_state,
            "power_w": power_w,
            "rated_power": inv.rated_power,
            "throttle_order": inv.throttle_order,
            "throttle_enabled": inv.throttle_enabled,
            "aggregate": inv.aggregate,
            "panel_power_wp": inv.panel_power_wp,
            "panel_wp_total": inverter_wp_total(inv),
            "strings": strings_to_payload(inv.strings),
            "location_id": inv.location_id,
            "poll_interval": inv.poll_interval,
            "phase": inv.phase,
            "phase_explicit": inv.phase_explicit,
            # Phasen-Detective measures grid-meter Δ during throttle, so it
            # only works when the inverter physically shares its grid meter
            # with the Venus this proxy talks to. virtual_inverter.location_ids
            # is the canonical list of "locations that flow into the Venus
            # aggregate" — inverters at any other Standort are physically
            # elsewhere and the grid_meter never sees their throttle. The UI
            # uses this flag to hide the Phasen-Detective panel for those
            # devices instead of letting the user kick off a probe that
            # would yield Multiplus-noise + nothing else.
            "phase_detect_supported": _phase_detect_supported(config, inv),
        }
        if inv.type == "opendtu":
            dev_entry["gateway_host"] = inv.gateway_host
            dev_entry["gateway_user"] = inv.gateway_user
            dev_entry["gateway_password"] = inv.gateway_password
            # The config page's inverter picker needs the configured serial
            # to pre-select it — and to not wipe it on an untouched save.
            dev_entry["serial"] = inv.serial
        if inv.type == "shelly":
            dev_entry["shelly_gen"] = inv.shelly_gen
        caps = get_throttle_caps(ds.plugin if ds else None)
        dev_entry["throttle_mode"] = caps.mode if caps else "none"
        # Reboot capability is duck-typed: any plugin that declares an
        # async ``reboot()`` opts in. Mirrors the snapshot payload's
        # supports_reboot so future sidebar consumers don't have to patch
        # this list separately (same convention as throttle_mode).
        dev_entry["supports_reboot"] = (
            hasattr(ds.plugin, "reboot") if (ds and ds.plugin) else False
        )
        # Prefer the live-measured response time (Phasen-Detective writes
        # it into the distributor's DeviceLimitState) over the plugin's
        # statically declared response_time_s — the distributor already
        # uses the effective value for waterfall ordering, the UI should
        # too.
        distributor = getattr(app_ctx, 'distributor', None)
        measured_response_time_s = None
        if distributor is not None:
            display = distributor.get_device_display_state(inv.id)
            if display and display["measured_response_time_s"] is not None:
                measured_response_time_s = display["measured_response_time_s"]
                dev_entry["measured_response_time_s"] = round(measured_response_time_s, 2)
        if caps is None:
            dev_entry["throttle_score"] = 0.0
        elif measured_response_time_s is not None:
            from pv_inverter_master.plugin import ThrottleCaps
            dev_entry["throttle_score"] = compute_throttle_score(ThrottleCaps(
                mode=caps.mode,
                response_time_s=measured_response_time_s,
                cooldown_s=caps.cooldown_s,
                startup_delay_s=caps.startup_delay_s,
            ))
        else:
            dev_entry["throttle_score"] = compute_throttle_score(caps)
        # 260528-hhj: per-device poll health. Only inverters get these keys —
        # pseudo-devices (venus / virtual / mqtt_pub / influx) below don't
        # have a DeviceState and must stay backward-compatible.
        dev_entry["last_poll_at"] = ds.last_poll_at if ds else None
        dev_entry["last_successful_poll_at"] = ds.last_successful_poll_at if ds else None
        dev_entry["last_poll_error"] = ds.last_poll_error if ds else None
        # Source-stale (OpenDTU radio gap): last successful poll had no fresh
        # data. Lets the anomaly detector tell a transient radio gap apart
        # from a dead inverter. Default False = fail-safe for non-OpenDTU /
        # never-polled rows (consumer treats absent/false as "not stale").
        dev_entry["source_stale"] = bool(getattr(ds, "source_stale", False)) if ds else False
        # Effective poll interval as RESOLVED by the registry (per-device
        # override > gateway > proxy default, then floored by the plugin
        # minimum). `poll_interval` above is the raw config value, where 0.0
        # means "use the default" — surfacing only that made the UI (and any
        # consumer computing a staleness threshold from it) reason about a
        # cadence the device does not actually run at.
        dev_entry["effective_poll_interval"] = getattr(ds, "poll_interval", 0.0) or None if ds else None
        # What the plugin actually reads periodically, reported by the plugin
        # itself so the register view cannot drift from the code. Empty for
        # plugins that synthesise their blocks; best-effort, never fatal.
        try:
            plugin = getattr(ds, "plugin", None) if ds else None
            policy = plugin.get_register_read_policy() if plugin is not None else []
            if policy:
                dev_entry["register_read_policy"] = policy
        except Exception:
            pass
        devices.append(dev_entry)

    venus_conn = "connected" if app_ctx.venus_mqtt_connected else "disconnected"
    devices.append({
        "id": "venus",
        "name": config.venus.name or "Venus OS",
        "type": "venus",
        "enabled": bool(config.venus.host),
        "connection_state": venus_conn,
    })
    virtual_conn = "disconnected"
    webapp = getattr(app_ctx, "webapp", None)
    slave_ctx = webapp.get("slave_ctx") if webapp is not None else None
    if slave_ctx and getattr(slave_ctx, "last_successful_read", 0) > 0:
        age = time.monotonic() - slave_ctx.last_successful_read
        virtual_conn = "connected" if age < 30 else "disconnected"
    virtual_rated_w = sum(
        inv.rated_power for inv in config.inverters
        if inv.enabled and inverter_counts_for_aggregate(config, inv)
    )
    devices.append({
        "id": "virtual",
        "name": config.virtual_inverter.name,
        "type": "virtual",
        "connection_state": virtual_conn,
        "rated_power": virtual_rated_w,
    })
    mqtt_pub_conn = "connected" if app_ctx.mqtt_pub_connected else "disconnected"
    devices.append({
        "id": "mqtt_pub",
        "name": config.mqtt_publish.name or "MQTT Publishing",
        "type": "mqtt_pub",
        "enabled": config.mqtt_publish.enabled,
        "connection_state": mqtt_pub_conn,
        "stats": {
            "messages": app_ctx.mqtt_pub_messages,
            "bytes": app_ctx.mqtt_pub_bytes,
            "skipped": app_ctx.mqtt_pub_skipped,
            "last_ts": app_ctx.mqtt_pub_last_ts,
        },
    })
    # InfluxDB logger: connected only counts once a write has succeeded;
    # disabled when the feature is off.
    if not config.influx.enabled:
        influx_conn = "disabled"
    elif app_ctx.influx_connected:
        influx_conn = "connected"
    else:
        influx_conn = "disconnected"
    devices.append({
        "id": "influx",
        "name": config.influx.name or "InfluxDB",
        "type": "influx",
        "enabled": config.influx.enabled,
        "connection_state": influx_conn,
        "stats": {
            "points": app_ctx.influx_points,
            "bytes": app_ctx.influx_bytes,
            "writes": app_ctx.influx_writes,
            "errors": app_ctx.influx_errors,
            "last_ts": app_ctx.influx_last_ts,
            "last_error": app_ctx.influx_last_error,
        },
    })
    return devices


# A remainder device row below this many watts is treated as noise and
# suppressed, so an idle/unnamed MPPT does not render a phantom 0 W card.
REMAINDER_EPSILON_W = 1.0


def _string_group_rows(inv: InverterEntry, snapshot: dict | None) -> list[dict]:
    """Expand one device into Standort-view rows.

    A device whose DC strings carry group names (``string_names``,
    index-aligned to the snapshot's ``dc_channels``) yields one row per
    named group — the summed power of its strings — plus a remainder row
    that reflects only the ungrouped strings' own DC power. The remainder
    row is omitted when that power is negligible (e.g. an unnamed MPPT
    sitting at 0 W), so a single named group carrying all production no
    longer produces a phantom 0 W device card. A device with no string
    names yields a single plain row.
    """
    base_name = inv.name or f"{inv.manufacturer} {inv.model}".strip() or "Inverter"
    inverter = (snapshot or {}).get("inverter", {}) or {}
    ac_power_w = inverter.get("ac_power_w", 0) or 0
    dc_channels = (snapshot or {}).get("dc_channels", []) or []
    names = inv.string_names or []

    # Group channel indices by their (non-empty) name, first-seen order.
    groups: dict[str, list[int]] = {}
    for i in range(len(dc_channels)):
        nm = str(names[i]).strip() if i < len(names) and names[i] else ""
        if nm:
            groups.setdefault(nm, []).append(i)

    plain_row = {
        "id": inv.id,
        "name": base_name,
        "type": inv.type,
        "enabled": inv.enabled,
        "power_w": ac_power_w,
        "rated_power": inv.rated_power,
        "kind": "device",
    }
    if not groups:
        return [plain_row]

    def _ch_power(idx: int) -> float:
        ch = dc_channels[idx] if idx < len(dc_channels) else {}
        return ch.get("power_w", 0) or 0

    rows: list[dict] = []
    assigned: set[int] = set()
    grouped_power = 0.0
    for gname, idxs in groups.items():
        gp = sum(_ch_power(i) for i in idxs)
        grouped_power += gp
        assigned.update(idxs)
        rows.append({
            "id": inv.id,
            "name": gname,
            "type": inv.type,
            "enabled": inv.enabled,
            "power_w": round(gp),
            "rated_power": 0,
            "kind": "string_group",
            "parent_id": inv.id,
            "parent_name": base_name,
        })

    # Remainder reflects the ungrouped channels' OWN DC power — NOT
    # ``ac_power_w - grouped_power`` (DC > AC due to conversion loss, which
    # used to clamp to 0 and render a phantom card). Omit the remainder row
    # entirely when no ungrouped string is meaningfully producing.
    remainder_idxs = [i for i in range(len(dc_channels)) if i not in assigned]
    remainder_power = sum(_ch_power(i) for i in remainder_idxs)
    if remainder_power > REMAINDER_EPSILON_W:
        plain_row["power_w"] = round(remainder_power)
        rows.append(plain_row)
    return rows


def _build_location_list(app_ctx: Any, config: Config) -> list[dict]:
    """Build the Standort list payload: merged coords + assigned inverters + power.

    Coordinates resolve in priority order: a live MQTT-fed position
    (``app_ctx.location_positions``) wins over the statically configured
    ``lat/lon``. ``position_source`` records which one was used so the UI
    and map can label live vs. fixed sites.
    """
    positions = getattr(app_ctx, "location_positions", {}) or {}
    topic_prefix = config.mqtt_publish.topic_prefix
    out: list[dict] = []
    now = time.time()
    for loc in config.locations:
        live = positions.get(loc.id)
        mqtt_last_ts = live.get("ts") if live else None
        # An MQTT position counts as fresh until it is older than
        # mqtt_fallback_days (0 = never expire). Once stale the Standort
        # falls back to its statically configured address.
        mqtt_fresh = False
        if (
            live
            and live.get("lat") is not None
            and live.get("lon") is not None
            and mqtt_last_ts
        ):
            fb_days = loc.mqtt_fallback_days
            if fb_days <= 0 or (now - mqtt_last_ts) <= fb_days * 86400.0:
                mqtt_fresh = True
        if mqtt_fresh:
            lat, lon = live["lat"], live["lon"]
            pos_source = "mqtt"
            pos_ts = mqtt_last_ts
        elif loc.lat is not None and loc.lon is not None:
            lat, lon = loc.lat, loc.lon
            pos_source = "static"
            pos_ts = None
        else:
            lat = lon = None
            pos_source = "none"
            pos_ts = None
        mqtt_stale = bool(loc.mqtt_coords and mqtt_last_ts and not mqtt_fresh)

        # Devices at this Standort, biggest (rated power) first. Each is
        # expanded into one-or-more rows so named PV-string groups show
        # as their own sub-units.
        loc_devices = sorted(
            (inv for inv in config.inverters if inv.location_id == loc.id),
            key=lambda inv: inv.rated_power,
            reverse=True,
        )
        inverters: list[dict] = []
        total_power_w = 0
        for inv in loc_devices:
            ds = app_ctx.devices.get(inv.id)
            snapshot = None
            if ds and ds.collector and ds.collector.last_snapshot:
                # last_snapshot has the AC totals; per-string dc_channels
                # is a plugin live cache and must be merged in for the
                # string-group expansion to see it.
                snapshot = dict(ds.collector.last_snapshot)
                if ds.plugin and getattr(ds.plugin, "dc_channels", None):
                    snapshot["dc_channels"] = ds.plugin.dc_channels
            total_power_w += (snapshot or {}).get("inverter", {}).get("ac_power_w", 0) or 0
            inverters.extend(_string_group_rows(inv, snapshot))

        out.append({
            "id": loc.id,
            "name": loc.name,
            "lat": lat,
            "lon": lon,
            "config_lat": loc.lat,
            "config_lon": loc.lon,
            "position_source": pos_source,
            "position_ts": pos_ts,
            "live_city": (live or {}).get("city"),
            "live_country": (live or {}).get("country"),
            "street": loc.street,
            "postal_code": loc.postal_code,
            "city": loc.city,
            "country": loc.country,
            "image_filename": loc.image_filename,
            "has_image": bool(loc.image_filename),
            "subnet": loc.subnet,
            "mqtt_coords": loc.mqtt_coords,
            "mqtt_fallback_days": loc.mqtt_fallback_days,
            "mqtt_last_ts": mqtt_last_ts,
            "mqtt_stale": mqtt_stale,
            "mqtt_topic": f"{topic_prefix}/location/{loc.id}/position",
            "inverters": inverters,
            "inverter_count": len(loc_devices),
            "total_power_w": total_power_w,
            "enabled": loc.enabled,
        })
    return out


