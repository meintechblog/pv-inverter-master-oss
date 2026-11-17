"""Locations / Standorte routes: CRUD, subnet reachability check,
geocoding (Nominatim) and Standort image upload/serve/delete.
"""
from __future__ import annotations

import asyncio
import dataclasses
import io
import os
import re
import tempfile
import zipfile
from datetime import datetime
from typing import Any

import aiohttp
import structlog
import yaml
from aiohttp import web

from pv_inverter_master.config import (
    Config,
    InverterEntry,
    Location,
    _generate_id,
    get_location,
    save_config,
    validate_inverter_config,
)

from .broadcasts import broadcast_device_list, broadcast_location_list
from .payloads import _build_location_list
from .reconfigure import _reconfigure_active, _reload_location_mqtt

# Inverter types accepted by the Standort import (mirrors routes_config).
_LOC_VALID_INVERTER_TYPES = {"solaredge", "opendtu", "shelly", "sungrow", "hoymiles", "senec"}

log = structlog.get_logger()


def _parse_coord(value: Any) -> float | None:
    """Parse a latitude/longitude value to float, or None when empty/invalid."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_fallback_days(value: Any) -> int:
    """Clamp the MQTT fallback window to 0 (infinite) .. 30 days."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 7
    return max(0, min(30, n))


#: User-Agent for outbound geocoding requests (Nominatim usage policy
#: requires an identifying UA on every request).
_GEOCODE_USER_AGENT = "pv-inverter-master/8.0 (github.com/meintechblog/pv-inverter-master-oss)"


async def reverse_geocode(lat: float, lon: float) -> dict | None:
    """Resolve coordinates to a town + country via Nominatim (reverse).

    Returns ``{"city": str|None, "country": str|None}`` where ``country``
    is ``None`` for German locations — only a foreign country needs
    spelling out in the UI. Returns ``None`` on any failure.
    """
    params = {
        "format": "jsonv2",
        "lat": f"{lat:.6f}",
        "lon": f"{lon:.6f}",
        "zoom": "12",
        "addressdetails": "1",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://nominatim.openstreetmap.org/reverse",
                params=params,
                headers={"User-Agent": _GEOCODE_USER_AGENT},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
    except Exception as e:
        log.warning("reverse_geocode_failed", error=str(e))
        return None

    addr = data.get("address") or {}
    city = (
        addr.get("city")
        or addr.get("town")
        or addr.get("village")
        or addr.get("municipality")
        or addr.get("hamlet")
        or addr.get("suburb")
        or addr.get("county")
    )
    country_code = (addr.get("country_code") or "").lower()
    country = addr.get("country") if country_code != "de" else None
    if not city and not country:
        return None
    return {"city": city, "country": country}


def _location_image_dir(app: web.Application) -> str:
    """Return (and create) the directory holding uploaded Standort images.

    Lives next to the config file so it is writable on both the LXC
    deploy (/etc/pv-inverter-master/) and local dev (config/).
    """
    base = os.path.dirname(os.path.abspath(app["config_path"]))
    img_dir = os.path.join(base, "location-images")
    os.makedirs(img_dir, exist_ok=True)
    return img_dir


def _location_to_dict(loc: Location, config: Config) -> dict:
    """Serialise a Location for an HTTP response, with the MQTT topic hint."""
    d = dataclasses.asdict(loc)
    d["mqtt_topic"] = f"{config.mqtt_publish.topic_prefix}/location/{loc.id}/position"
    return d


async def locations_list_handler(request: web.Request) -> web.Response:
    """GET /api/locations — all Standorte with merged coords + assigned inverters."""
    config: Config = request.app["config"]
    app_ctx = request.app["app_ctx"]
    return web.json_response({"locations": _build_location_list(app_ctx, config)})


async def locations_add_handler(request: web.Request) -> web.Response:
    """POST /api/locations — create a new Standort."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    name = (body.get("name") or "").strip()
    if not name:
        return web.json_response({"error": "name required"}, status=400)

    loc = Location(
        name=name,
        lat=_parse_coord(body.get("lat")),
        lon=_parse_coord(body.get("lon")),
        street=(body.get("street") or "").strip(),
        postal_code=(body.get("postal_code") or "").strip(),
        city=(body.get("city") or "").strip(),
        country=(body.get("country") or "").strip(),
        subnet=(body.get("subnet") or "").strip(),
        mqtt_coords=bool(body.get("mqtt_coords", False)),
        mqtt_fallback_days=_parse_fallback_days(body.get("mqtt_fallback_days", 7)),
    )
    config: Config = request.app["config"]
    config.locations.append(loc)
    save_config(request.app["config_path"], config)
    log.info("user_action", action="location_added", location_id=loc.id, name=name)

    await _reload_location_mqtt(request.app, config)
    await broadcast_location_list(request.app)
    return web.json_response(_location_to_dict(loc, config), status=201)


async def locations_update_handler(request: web.Request) -> web.Response:
    """PUT /api/locations/{id} — update an existing Standort."""
    loc_id = request.match_info["id"]
    config: Config = request.app["config"]
    loc = get_location(config, loc_id)
    if loc is None:
        return web.json_response({"error": "Location not found"}, status=404)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    if "name" in body:
        new_name = (body["name"] or "").strip()
        if not new_name:
            return web.json_response({"error": "name cannot be empty"}, status=400)
        loc.name = new_name
    for field_name in ("street", "postal_code", "city", "country", "subnet"):
        if field_name in body:
            setattr(loc, field_name, (body[field_name] or "").strip())
    if "lat" in body:
        loc.lat = _parse_coord(body["lat"])
    if "lon" in body:
        loc.lon = _parse_coord(body["lon"])
    if "mqtt_coords" in body:
        loc.mqtt_coords = bool(body["mqtt_coords"])
    if "mqtt_fallback_days" in body:
        loc.mqtt_fallback_days = _parse_fallback_days(body["mqtt_fallback_days"])

    save_config(request.app["config_path"], config)
    log.info("user_action", action="location_updated", location_id=loc_id)

    await _reload_location_mqtt(request.app, config)
    await broadcast_location_list(request.app)
    return web.json_response(_location_to_dict(loc, config))


async def locations_delete_handler(request: web.Request) -> web.Response:
    """DELETE /api/locations/{id} — remove a Standort.

    Unassigns any inverters pointing at it and drops it from the proxy's
    aggregate location scope so no dangling references survive.
    """
    loc_id = request.match_info["id"]
    config: Config = request.app["config"]
    loc = get_location(config, loc_id)
    if loc is None:
        return web.json_response({"error": "Location not found"}, status=404)

    # Best-effort: delete the uploaded image file.
    if loc.image_filename:
        try:
            os.unlink(os.path.join(_location_image_dir(request.app), loc.image_filename))
        except OSError:
            pass

    config.locations = [item for item in config.locations if item.id != loc_id]
    for inv in config.inverters:
        if inv.location_id == loc_id:
            inv.location_id = ""
    config.virtual_inverter.location_ids = [
        x for x in config.virtual_inverter.location_ids if x != loc_id
    ]
    request.app["app_ctx"].location_positions.pop(loc_id, None)
    save_config(request.app["config_path"], config)
    log.info("user_action", action="location_deleted", location_id=loc_id)

    await _reload_location_mqtt(request.app, config)
    await broadcast_location_list(request.app)
    await broadcast_device_list(request.app)
    return web.json_response({"success": True})


async def location_subnet_check_handler(request: web.Request) -> web.Response:
    """POST /api/locations/check-subnet — quick reachability sweep.

    Validates the subnet string and TCP-probes every host on the common
    device ports so the user can confirm a Standort's network is reachable
    before relying on it for discovery. An empty subnet checks the
    proxy's own local subnet.
    """
    from pv_inverter_master.scanner import (
        _probe_port,
        detect_subnet,
        parse_subnet_override,
    )

    try:
        body = await request.json()
    except Exception:
        body = {}
    subnet_raw = (body.get("subnet") or "").strip()

    try:
        subnet = parse_subnet_override(subnet_raw) if subnet_raw else detect_subnet()
    except ValueError as e:
        return web.json_response({"ok": False, "error": f"Ungültiges Subnetz: {e}"})
    except Exception as e:
        return web.json_response(
            {"ok": False, "error": f"Subnetz konnte nicht ermittelt werden: {e}"}
        )

    hosts = [str(ip) for ip in subnet.hosts()]
    if len(hosts) > 512:
        return web.json_response({
            "ok": False,
            "error": f"Subnetz zu groß ({len(hosts)} Hosts) — bitte /23 oder kleiner prüfen.",
        })

    # Ports across all discovery types: HTTP, HTTPS/SENEC, Modbus, Hoymiles.
    ports = (80, 443, 502, 1502, 10081)
    sem = asyncio.Semaphore(100)

    async def _probe(ip: str, port: int) -> tuple[str, bool]:
        async with sem:
            return ip, await _probe_port(ip, port, 0.4)

    swept = await asyncio.gather(
        *(_probe(ip, port) for ip in hosts for port in ports)
    )
    alive = {ip for ip, is_open in swept if is_open}
    log.info(
        "user_action", action="subnet_check",
        subnet=str(subnet), scanned=len(hosts), alive=len(alive),
    )
    return web.json_response({
        "ok": True,
        "subnet": str(subnet),
        "hosts_scanned": len(hosts),
        "hosts_alive": len(alive),
    })


async def geocode_handler(request: web.Request) -> web.Response:
    """POST /api/geocode — resolve an address to coordinates via Nominatim.

    Server-side so the browser is not subject to CORS and a proper
    identifying User-Agent is sent (Nominatim usage policy).
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"success": False, "error": "Invalid JSON"}, status=400)

    street = (body.get("street") or "").strip()
    postal = (body.get("postal_code") or "").strip()
    city = (body.get("city") or "").strip()
    country = (body.get("country") or "").strip()
    if not any([street, postal, city, country]):
        return web.json_response(
            {"success": False, "error": "Adresse unvollständig"}, status=400,
        )

    params = {"format": "jsonv2", "limit": "1"}
    if street:
        params["street"] = street
    if city:
        params["city"] = city
    if postal:
        params["postalcode"] = postal
    if country:
        params["country"] = country

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://nominatim.openstreetmap.org/search",
                params=params,
                headers={"User-Agent": _GEOCODE_USER_AGENT},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return web.json_response(
                        {"success": False, "error": f"Nominatim HTTP {resp.status}"},
                    )
                results = await resp.json()
    except Exception as e:
        log.warning("geocode_failed", error=str(e))
        return web.json_response(
            {"success": False, "error": f"Geocoding fehlgeschlagen: {e}"},
        )

    if not results:
        return web.json_response(
            {"success": False, "error": "Keine Koordinaten für diese Adresse gefunden"},
        )

    top = results[0]
    log.info("user_action", action="geocode", city=city, found=top.get("display_name", ""))
    return web.json_response({
        "success": True,
        "lat": float(top["lat"]),
        "lon": float(top["lon"]),
        "display_name": top.get("display_name", ""),
    })


#: Accepted image MIME types mapped to their canonical file extension.
_IMAGE_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

#: Hard cap on uploaded Standort images.
_MAX_IMAGE_BYTES = 3 * 1024 * 1024  # 3 MB


async def location_image_upload_handler(request: web.Request) -> web.Response:
    """POST /api/locations/{id}/image — upload a Standort photo (multipart).

    The stored filename is derived from the Standort id (``<id><ext>``) so
    it can never escape the image directory via path traversal.
    """
    loc_id = request.match_info["id"]
    config: Config = request.app["config"]
    loc = get_location(config, loc_id)
    if loc is None:
        return web.json_response({"error": "Location not found"}, status=404)

    try:
        reader = await request.multipart()
    except Exception:
        return web.json_response({"error": "Expected multipart/form-data"}, status=400)

    field = await reader.next()
    while field is not None and field.name != "image":
        field = await reader.next()
    if field is None:
        return web.json_response({"error": "Missing 'image' field"}, status=400)

    content_type = (field.headers.get("Content-Type", "") or "").split(";")[0].strip().lower()
    ext = _IMAGE_EXTENSIONS.get(content_type)
    if ext is None:
        return web.json_response(
            {"error": f"Nicht unterstützter Bildtyp: {content_type or 'unbekannt'}"},
            status=400,
        )

    data = bytearray()
    while True:
        chunk = await field.read_chunk(65536)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > _MAX_IMAGE_BYTES:
            return web.json_response({"error": "Bild zu groß (max. 3 MB)"}, status=413)
    if not data:
        return web.json_response({"error": "Leere Datei"}, status=400)

    img_dir = _location_image_dir(request.app)
    filename = f"{loc_id}{ext}"
    old_filename = loc.image_filename

    fd, tmp = tempfile.mkstemp(dir=img_dir, suffix=ext)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, os.path.join(img_dir, filename))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    # Drop a stale image left over from a different extension.
    if old_filename and old_filename != filename:
        try:
            os.unlink(os.path.join(img_dir, old_filename))
        except OSError:
            pass

    loc.image_filename = filename
    save_config(request.app["config_path"], config)
    log.info("user_action", action="location_image_uploaded", location_id=loc_id, bytes=len(data))
    await broadcast_location_list(request.app)
    return web.json_response({"success": True, "image_filename": filename})


async def location_image_get_handler(request: web.Request) -> web.StreamResponse:
    """GET /api/locations/{id}/image — serve the uploaded Standort photo."""
    loc_id = request.match_info["id"]
    config: Config = request.app["config"]
    loc = get_location(config, loc_id)
    if loc is None or not loc.image_filename:
        raise web.HTTPNotFound()
    path = os.path.join(_location_image_dir(request.app), loc.image_filename)
    if not os.path.isfile(path):
        raise web.HTTPNotFound()
    return web.FileResponse(path)


async def location_image_delete_handler(request: web.Request) -> web.Response:
    """DELETE /api/locations/{id}/image — remove the uploaded Standort photo."""
    loc_id = request.match_info["id"]
    config: Config = request.app["config"]
    loc = get_location(config, loc_id)
    if loc is None:
        return web.json_response({"error": "Location not found"}, status=404)
    if loc.image_filename:
        try:
            os.unlink(os.path.join(_location_image_dir(request.app), loc.image_filename))
        except OSError:
            pass
        loc.image_filename = ""
        save_config(request.app["config_path"], config)
        log.info("user_action", action="location_image_deleted", location_id=loc_id)
    await broadcast_location_list(request.app)
    return web.json_response({"success": True})



def _location_export_filename(loc: Location) -> str:
    """Build a filesystem-safe .zip download filename for a Standort."""
    label = re.sub(r"[^A-Za-z0-9_.-]+", "-", loc.name or loc.id).strip("-") or loc.id
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{ts}_standort_{label}.zip"


#: Image extensions the Standort import will restore (derived from the
#: upload whitelist), plus the jpeg alias.
_IMPORT_IMAGE_EXTS = set(_IMAGE_EXTENSIONS.values()) | {".jpeg"}


async def location_export_handler(request: web.Request) -> web.Response:
    """Export a whole Standort as a downloadable ZIP.

    The archive bundles ``standort.yaml`` (the Location plus every
    InverterEntry assigned to it) and the uploaded photo, so a single file
    captures the complete site. Re-importing it (here or on another
    instance) restores the Standort, its image and all its inverters.
    """
    loc_id = request.match_info.get("id", "")
    config: Config = request.app["config"]
    loc = get_location(config, loc_id)
    if loc is None:
        return web.json_response({"error": "location_not_found"}, status=404)

    inverters = [
        dataclasses.asdict(e) for e in config.inverters if e.location_id == loc_id
    ]
    manifest = {
        "_export": "pv-inverter-master-location",
        "version": 1,
        "location": dataclasses.asdict(loc),
        "inverters": inverters,
    }

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("standort.yaml", yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True))
        if loc.image_filename:
            img_path = os.path.join(_location_image_dir(request.app), loc.image_filename)
            if os.path.isfile(img_path):
                with open(img_path, "rb") as f:
                    zf.writestr(loc.image_filename, f.read())

    return web.Response(
        body=buf.getvalue(),
        content_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={_location_export_filename(loc)}"},
    )


def _restore_location_image(zf: zipfile.ZipFile, img_name: str, img_dir: str, loc_id: str) -> str:
    """Write the archived Standort image to ``<loc_id><ext>``. Returns filename or ''."""
    member = None
    names = zf.namelist()
    if img_name and img_name in names:
        member = img_name
    else:
        for n in names:
            if n != "standort.yaml" and not n.endswith("/"):
                member = n
                break
    if member is None:
        return ""
    ext = os.path.splitext(member)[1].lower()
    if ext not in _IMPORT_IMAGE_EXTS:
        return ""
    if ext == ".jpeg":
        ext = ".jpg"
    data = zf.read(member)
    if not data or len(data) > _MAX_IMAGE_BYTES:
        return ""
    os.makedirs(img_dir, exist_ok=True)
    filename = f"{loc_id}{ext}"
    fd, tmp = tempfile.mkstemp(dir=img_dir, suffix=ext)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, os.path.join(img_dir, filename))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return filename


async def location_import_handler(request: web.Request) -> web.Response:
    """Import a whole Standort from an uploaded ZIP (raw application/zip body).

    Restores the Location (new, or update-in-place when the id already
    exists), its photo, and every bundled inverter (assigned to this
    Standort and started live). Unparseable archives or a missing
    ``standort.yaml`` → 400. Invalid individual inverters are skipped, not
    fatal.
    """
    try:
        raw = await request.read()
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except Exception as e:
        return web.json_response({"success": False, "error": f"Invalid ZIP: {e}"}, status=400)

    try:
        manifest = yaml.safe_load(zf.read("standort.yaml").decode("utf-8"))
    except KeyError:
        return web.json_response({"success": False, "error": "standort.yaml missing in ZIP"}, status=400)
    except Exception as e:
        return web.json_response({"success": False, "error": f"Manifest parse error: {e}"}, status=400)

    if not isinstance(manifest, dict) or not isinstance(manifest.get("location"), dict):
        return web.json_response({"success": False, "error": "No location in manifest"}, status=400)

    loc_data = manifest["location"]
    inv_list = manifest.get("inverters") or []

    config: Config = request.app["config"]

    # --- Location: known fields only; image_filename handled separately. ---
    loc_fields = set(Location.__dataclass_fields__)
    loc_clean = {k: v for k, v in loc_data.items() if k in loc_fields and v is not None}
    if not (loc_clean.get("name") or "").strip():
        return web.json_response({"success": False, "error": "location name required"}, status=400)
    archived_image = loc_clean.pop("image_filename", "") or ""

    incoming_id = str(loc_clean.get("id") or "")
    existing = get_location(config, incoming_id) if incoming_id else None
    if existing is not None:
        for k, v in loc_clean.items():
            if k == "id":
                continue
            setattr(existing, k, v)
        loc = existing
        loc_is_new = False
    else:
        loc = Location()
        for k, v in loc_clean.items():
            setattr(loc, k, v)
        if not loc.id or get_location(config, loc.id) is not None:
            loc.id = _generate_id()
        config.locations.append(loc)
        loc_is_new = True

    # --- Image: restore to <loc_id><ext>; leave existing untouched if none. ---
    try:
        restored = _restore_location_image(
            zf, archived_image, _location_image_dir(request.app), loc.id
        )
        if restored:
            loc.image_filename = restored
    except Exception as e:
        log.warning("location_import_image_failed", location_id=loc.id, error=str(e))

    # --- Inverters: assign to this Standort, start/restart each live. ---
    inv_fields = set(InverterEntry.__dataclass_fields__)
    applied: list[tuple[str, bool]] = []
    skipped = 0
    for raw_inv in inv_list:
        if not isinstance(raw_inv, dict):
            skipped += 1
            continue
        clean = {k: v for k, v in raw_inv.items() if k in inv_fields and v is not None}
        itype = clean.get("type", "")
        host = clean.get("host", "")
        if (itype and itype not in _LOC_VALID_INVERTER_TYPES) or \
                validate_inverter_config(host, clean.get("port", 1502), clean.get("unit_id", 1)):
            skipped += 1
            continue

        inv_id = str(clean.get("id") or "")
        existing_inv = next((e for e in config.inverters if e.id == inv_id), None) if inv_id else None
        if existing_inv is not None:
            for k, v in clean.items():
                if k == "id":
                    continue
                setattr(existing_inv, k, v)
            existing_inv.location_id = loc.id
            entry = existing_inv
            inv_is_new = False
        else:
            entry = InverterEntry(host=host)
            for k, v in clean.items():
                setattr(entry, k, v)
            if not entry.id or any(e.id == entry.id for e in config.inverters):
                entry.id = _generate_id()
            entry.location_id = loc.id
            config.inverters.append(entry)
            inv_is_new = True
        applied.append((entry.id, inv_is_new))

    try:
        save_config(request.app["config_path"], config)
        await _reload_location_mqtt(request.app, config)
        for device_id, inv_is_new in applied:
            if not inv_is_new:
                await _reconfigure_active(request.app, config, device_id=device_id, action="stop")
            await _reconfigure_active(request.app, config, device_id=device_id, action="start")
        await broadcast_location_list(request.app)
        await broadcast_device_list(request.app)
        log.info(
            "user_action", action="location_imported",
            location_id=loc.id, is_new=loc_is_new,
            inverters=len(applied), skipped=skipped,
        )
    except Exception as e:
        return web.json_response({"success": False, "error": f"Apply failed: {e}"}, status=500)

    return web.json_response({
        "success": True,
        "location_id": loc.id,
        "location_name": loc.name,
        "is_new": loc_is_new,
        "inverters_imported": len(applied),
        "inverters_skipped": skipped,
        "image_restored": bool(loc.image_filename),
    })


async def location_set_enabled_handler(request: web.Request) -> web.Response:
    """POST /api/locations/{id}/enabled — (de)activate a whole Standort.

    Body: ``{"enabled": bool}``. Disabling a Standort cascades to every
    inverter assigned to it: each entry's ``enabled`` flag is flipped and
    its poll loop is stopped (or started again on re-activation), so a
    deactivated site stops talking to all its hardware. Persisted to config
    so the state survives a restart.
    """
    loc_id = request.match_info["id"]
    config: Config = request.app["config"]
    loc = get_location(config, loc_id)
    if loc is None:
        return web.json_response({"error": "Location not found"}, status=404)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    enabled = bool(body.get("enabled", True))
    loc.enabled = enabled

    affected = [inv for inv in config.inverters if inv.location_id == loc_id]
    for inv in affected:
        inv.enabled = enabled

    save_config(request.app["config_path"], config)

    # Apply live, guarding against double start/stop: only start devices
    # not already running, only stop ones that are.
    app_ctx = request.app["app_ctx"]
    registry = getattr(app_ctx, "device_registry", None)
    active = set(registry.get_active_device_ids()) if registry is not None else set()
    action = "start" if enabled else "stop"
    for inv in affected:
        is_active = inv.id in active
        if (enabled and is_active) or (not enabled and not is_active):
            continue
        await _reconfigure_active(request.app, config, device_id=inv.id, action=action)

    await broadcast_location_list(request.app)
    await broadcast_device_list(request.app)
    log.info(
        "user_action", action="location_enabled_toggled",
        location_id=loc_id, enabled=enabled, inverters=len(affected),
    )
    return web.json_response({
        "success": True,
        "location_id": loc_id,
        "enabled": enabled,
        "inverters_affected": len(affected),
    })
