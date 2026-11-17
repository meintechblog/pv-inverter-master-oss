"""Venus Health Dashboard endpoints.

Exposes /api/venus/health — an aggregate read of everything ``venus_reader``
has observed from the Venus OS MQTT broker (system info, detected peers,
limiter state, last throttle command). Pure read of in-memory state +
``state_file.load_state()`` — no MQTT round-trip, no DB.

JSON shape::

    {
      "mqtt": {"configured", "connected", "host", "portal_id", "connected_since_ts"},
      "system": {"serial", "type", "firmware", "hostname"},
      "coupling": {"registered", "device_instance", "product_name",
                   "custom_name", "connection_string", "position",
                   "position_label", "limiter_supported", "limiter_enabled",
                   "max_power_w", "power_limit_w"},
      "peers": {"grid_meter": {...}, "vebus": {...},
                "solarcharger": {...}, "battery": {...}},
      "control": {"last_throttle_command_ts", "last_throttle_pct",
                  "missing_for_active_regulation": [str, ...]}
    }

Always returns 200; the caller dims the UI when ``mqtt.connected`` is false.
"""
from __future__ import annotations

from aiohttp import web

from pv_inverter_master import state_file


# Venus position enum → German label (Settings/Fronius/Inverters/<I>/Position).
_POSITION_LABELS = {
    0: "AC-Eingang",
    1: "AC-Ausgang",
    2: "AC-Genset-Ausgang",
}


def _format_devices(bucket: dict, extras: tuple = ()) -> list[dict]:
    """Flatten a peer bucket into a sorted list of devices for the UI.

    extras is a tuple of extra peer-field names to surface per device
    (e.g. "dc_power_w" for vebus, "soc_pct" for batteries).
    """
    devices: list[dict] = []
    for inst_key, peer in bucket.items():
        if not isinstance(peer, dict):
            continue
        entry = {
            "device_instance": peer.get("device_instance"),
            "product_name": peer.get("product_name"),
        }
        for key in extras:
            if key in peer:
                entry[key] = peer.get(key)
        devices.append(entry)
    devices.sort(key=lambda d: (d.get("device_instance") if d.get("device_instance") is not None else 1 << 30))
    # Promote a "Battery Aggregator" (or anything named *Aggregator*) to the
    # front so the headline reflects the system-total view rather than a
    # single BMS reading. The full list still shows every device.
    for k, dev in enumerate(devices):
        name = (dev.get("product_name") or "").lower()
        if "aggregator" in name and k != 0:
            devices.insert(0, devices.pop(k))
            break
    return devices


def _first_peer(bucket: dict) -> dict | None:
    """Return the first peer dict from a ``peers[<type>]`` mapping, or None."""
    if not bucket:
        return None
    # Deterministic order — sort by key so output is stable across calls.
    first_key = sorted(bucket.keys())[0]
    return bucket.get(first_key)


def _peer_summary(bucket: dict, *, include_power: bool = False, extras: tuple = ()) -> dict:
    """Build a peer-class summary plus a ``devices`` list of every instance.

    The single-device fields (product_name / device_instance / ac_power_w /
    extras) reflect the primary peer (lowest device_instance), which the UI
    uses as the headline. ``devices`` is the full list — used to render
    multi-instance peer classes such as battery aggregator + per-BMS.
    """
    devices = _format_devices(bucket, extras=extras + (("ac_power_w",) if include_power else ()))
    if not devices:
        out = {
            "present": False,
            "count": 0,
            "product_name": None,
            "device_instance": None,
            "devices": [],
        }
        if include_power:
            out["ac_power_w"] = None
        for key in extras:
            out[key] = None
        return out
    primary = devices[0]
    out = {
        "present": True,
        "count": len(devices),
        "product_name": primary.get("product_name"),
        "device_instance": primary.get("device_instance"),
        "devices": devices,
    }
    if include_power:
        out["ac_power_w"] = primary.get("ac_power_w")
    for key in extras:
        out[key] = primary.get(key)
    return out


def _compute_missing(peers: dict, limiter: dict, control_state, vs: dict | None = None) -> list[str]:
    """Derive the ordered list of hints for what is missing for active PV regulation.

    Order matters — the UI renders the list verbatim. The grid_meter hint
    is prepended because without a grid meter the entire ESS/feed-in
    control loop cannot run. The PreventFeedback hint is the most
    actionable one and goes to the very top when active.
    """
    missing: list[str] = []
    vebus_present = bool(peers.get("vebus"))
    battery_present = bool(peers.get("battery"))
    grid_present = bool(peers.get("grid_meter"))
    limiter_enabled = limiter.get("enabled")
    limiter_supported = limiter.get("supported")
    prevent_feedback = bool((vs or {}).get("prevent_feedback"))

    if not vebus_present:
        missing.append("Multiplus per VE.Bus anschließen")
    if vebus_present and not battery_present:
        missing.append("Batterie/MPPT-Verbindung prüfen")
    if vebus_present and limiter_enabled is False:
        missing.append("ESS-Assistent auf Multiplus laden")
    if vebus_present and limiter_supported is False:
        missing.append("Feed-in-Limit oder ESS-Policy aktivieren")
    if not grid_present:
        missing.insert(0, "Grid-Meter (Carlo Gavazzi / Shelly EM) verbinden")
    if prevent_feedback:
        # The PreventFeedback master is so consequential — Venus actively
        # throttles PV to zero feed-in — that it bumps to the absolute top.
        missing.insert(
            0,
            "Venus blockiert Einspeisung: Haken bei Einstellungen → Netzeinspeisung "
            "→ AC-gekoppelte PV → Überschusseinspeisung setzen (PreventFeedback=0)",
        )
    return missing


async def venus_health_handler(request: web.Request) -> web.Response:
    """Return the aggregate Venus health snapshot as JSON."""
    config = request.app["config"]
    app_ctx = request.app["app_ctx"]
    vs = app_ctx.venus_settings or {}
    peers_state = vs.get("peers", {}) or {}
    system = vs.get("system", {}) or {}
    limiter = vs.get("limiter", {}) or {}

    # MQTT block
    mqtt_block = {
        "configured": bool(config.venus.host),
        "connected": bool(app_ctx.venus_mqtt_connected),
        "host": config.venus.host or None,
        "portal_id": config.venus.portal_id or None,
        "connected_since_ts": app_ctx.venus_connected_since_ts,
    }

    # ATS (Auto-Transfer-Switch) state — surfaced here so the UI can
    # show a single-line status banner alongside the Venus connection.
    # None until the first MQTT publish lands.
    from pv_inverter_master.ats_mqtt import ats_label
    ats_block = {
        "state": app_ctx.ats_state,
        "label": ats_label(app_ctx.ats_state),
        "ts": app_ctx.ats_state_ts or None,
    }

    # System block (pass-through of whatever was observed)
    system_block = {
        "serial": system.get("serial"),
        "type": system.get("type"),
        "firmware": system.get("firmware"),
        "hostname": system.get("hostname"),
    }

    # Coupling block — the proxy's own pvinverter registration on Venus.
    # Source live values from the always-published pvinverter dbus paths first
    # (Position, Ac/MaxPower, Ac/PowerLimit), and fall back to the Fronius
    # Settings topics only when the live path hasn't arrived yet. The Settings
    # tree requires an R/-keepalive nudge to publish — the live pvinverter
    # paths are pushed by Venus continuously.
    pv_bucket = peers_state.get("pvinverter") or {}
    pv_peer = _first_peer(pv_bucket)
    pv = pv_peer or {}
    position_val = pv.get("position")
    if position_val is None:
        position_val = limiter.get("position")
    position_label = _POSITION_LABELS.get(position_val) if position_val is not None else None
    max_power_w = pv.get("max_power_w") or getattr(config.virtual_inverter, "rated_w", None)
    power_limit_w = pv.get("power_limit_w")
    if power_limit_w is None:
        power_limit_w = vs.get("pv_limit_w")
    custom_name = pv.get("custom_name") or limiter.get("custom_name")
    coupling_block = {
        "registered": bool(pv_bucket),
        "device_instance": pv.get("device_instance"),
        "product_name": pv.get("product_name"),
        "custom_name": custom_name,
        "connection_string": pv.get("connection_string"),
        "position": position_val,
        "position_label": position_label,
        "limiter_supported": bool(limiter.get("supported")) if limiter.get("supported") is not None else None,
        "limiter_enabled": bool(limiter.get("enabled")) if limiter.get("enabled") is not None else None,
        "max_power_w": max_power_w,
        "power_limit_w": power_limit_w,
    }

    # Peers block.
    # pvinverter is OUR proxy as seen from the Venus side — surfacing it here
    # gives the user a positive integration-confirmation in the same card as
    # the rest of the Venus-side device inventory. Power is included so the
    # user can sanity-check that Venus's view matches the proxy's own output.
    # Each summary also carries a `devices` list so multi-instance peer
    # classes (battery aggregator + per-BMS) render every member.
    peers_block = {
        "grid_meter": _peer_summary(peers_state.get("grid_meter") or {}, include_power=True),
        "pvinverter": _peer_summary(peers_state.get("pvinverter") or {}, include_power=True),
        "vebus": _peer_summary(
            peers_state.get("vebus") or {},
            extras=("dc_power_w", "ac_out_power_w"),
        ),
        "solarcharger": _peer_summary(
            peers_state.get("solarcharger") or {},
            extras=("dc_power_w",),
        ),
        "battery": _peer_summary(
            peers_state.get("battery") or {},
            extras=("soc_pct", "dc_power_w", "custom_name"),
        ),
    }

    # Control block
    persisted = state_file.load_state()
    prevent_feedback = bool(vs.get("prevent_feedback"))
    max_feed_in_w_raw = vs.get("max_feed_in_w")
    # Venus uses -1 to mean "unlimited"; surface that as None for the frontend.
    max_feed_in_w = (
        None if max_feed_in_w_raw is None or int(max_feed_in_w_raw) < 0
        else int(max_feed_in_w_raw)
    )
    control_block = {
        "last_throttle_command_ts": persisted.power_limit_set_at,
        "last_throttle_pct": persisted.power_limit_pct,
        "feed_in_allowed": not prevent_feedback,
        "max_feed_in_w": max_feed_in_w,
        "missing_for_active_regulation": _compute_missing(peers_state, limiter, persisted, vs),
    }

    return web.json_response({
        "mqtt": mqtt_block,
        "system": system_block,
        "coupling": coupling_block,
        "peers": peers_block,
        "control": control_block,
        "ats": ats_block,
    })
