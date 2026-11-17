"""MQTT subscriber for live Standort coordinates (caravan tracking).

Subscribes to ``<topic_prefix>/location/+/position`` on the same broker
the MQTT publisher uses and feeds positions into
``AppContext.location_positions``. Every accepted update is broadcast to
WebSocket clients and queued to InfluxDB so the caravan's track is
recorded.

Expected payload (retained, so a fresh connection re-seeds positions)::

    {"lat": 48.137154, "lon": 11.576124}

Only Standorte flagged ``mqtt_coords: true`` honour incoming positions —
a publish to a statically-placed Standort's topic is ignored so the
operator's intent is respected.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import tempfile
import time

import aiomqtt
import structlog

from pv_inverter_master.config import get_location

log = structlog.get_logger(component="location_mqtt")

#: Re-run reverse geocoding only after the Standort has moved this far.
#: Town names change on a km scale, and Nominatim asks for <1 req/s — a
#: caravan would have to move at highway speed to come close to that.
_REGEOCODE_DISTANCE_M = 250.0

#: Standort IDs with a reverse-geocode request currently in flight, so
#: rapid position updates do not spawn duplicate lookups.
_geocoding_in_flight: set[str] = set()


def _distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Rough great-circle distance in metres (equirectangular approx)."""
    x = math.radians(lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    y = math.radians(lat2 - lat1)
    return math.hypot(x, y) * 6371000.0


def _needs_regeocode(prev: dict, lat: float, lon: float) -> bool:
    """True when the live position has no place name yet or has moved far."""
    if not prev or not prev.get("city"):
        return True
    glat, glon = prev.get("geo_lat"), prev.get("geo_lon")
    if glat is None or glon is None:
        return True
    return _distance_m(glat, glon, lat, lon) >= _REGEOCODE_DISTANCE_M


async def _resolve_place(ctx, loc_id: str, lat: float, lon: float) -> None:
    """Reverse-geocode a live position and store the town/country on it."""
    try:
        from pv_inverter_master.webapp import broadcast_location_list, reverse_geocode

        place = await reverse_geocode(lat, lon)
        if place is None:
            return
        cur = ctx.location_positions.get(loc_id)
        if cur is None:
            return
        cur["city"] = place.get("city")
        cur["country"] = place.get("country")
        cur["geo_lat"] = lat
        cur["geo_lon"] = lon
        _save_positions(ctx)
        log.info(
            "location_place_resolved", location_id=loc_id,
            city=place.get("city"), country=place.get("country"),
        )
        # Push a fresh Standort list so the overview reflects the move at
        # once instead of waiting for the next periodic broadcast.
        if ctx.webapp is not None:
            await broadcast_location_list(ctx.webapp)
    except Exception as e:
        log.warning("location_place_failed", location_id=loc_id, error=str(e))
    finally:
        _geocoding_in_flight.discard(loc_id)


def _positions_path(ctx) -> str:
    """Return the on-disk path for the persisted live position cache."""
    base = os.path.dirname(os.path.abspath(ctx.config_path or "config.yaml"))
    return os.path.join(base, "location_positions.json")


def _load_positions(ctx) -> None:
    """Load persisted MQTT positions so the staleness timer survives restarts."""
    try:
        with open(_positions_path(ctx)) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return
    if not isinstance(data, dict):
        return
    for loc_id, v in data.items():
        if isinstance(v, dict) and v.get("lat") is not None and v.get("lon") is not None:
            entry = {"lat": v["lat"], "lon": v["lon"], "ts": v.get("ts")}
            # Carry the last resolved place so it shows immediately on
            # restart and the move-distance throttle keeps working.
            for key in ("city", "country", "geo_lat", "geo_lon"):
                if v.get(key) is not None:
                    entry[key] = v[key]
            ctx.location_positions[loc_id] = entry
    log.info("location_positions_loaded", count=len(ctx.location_positions))


def _save_positions(ctx) -> None:
    """Atomically persist the live position cache (best-effort)."""
    path = _positions_path(ctx)
    try:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(ctx.location_positions, f)
        os.replace(tmp, path)
    except OSError as e:
        log.warning("location_positions_save_failed", error=str(e))


async def location_mqtt_loop(ctx, mqtt_config) -> None:
    """Background task: subscribe to per-Standort position topics.

    Args:
        ctx: AppContext — provides location_positions, influx_queue,
            webapp, config, shutdown_event.
        mqtt_config: MqttPublishConfig — reuses the publish broker's
            host/port/topic_prefix/client_id.
    """
    sub_topic = f"{mqtt_config.topic_prefix}/location/+/position"
    backoff = 1.0
    max_backoff = 30.0

    _load_positions(ctx)

    while not ctx.shutdown_event.is_set():
        try:
            async with aiomqtt.Client(
                hostname=mqtt_config.host,
                port=mqtt_config.port,
                identifier=f"{mqtt_config.client_id}-locsub",
                keepalive=30,
            ) as client:
                await client.subscribe(sub_topic, qos=1)
                backoff = 1.0
                log.info("location_mqtt_connected", host=mqtt_config.host, topic=sub_topic)
                async for message in client.messages:
                    if ctx.shutdown_event.is_set():
                        break
                    await _handle_message(
                        ctx, str(message.topic), message.payload,
                        bool(getattr(message, "retain", False)),
                    )
        except aiomqtt.MqttError as e:
            log.warning("location_mqtt_disconnected", error=str(e), backoff=backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)
        except asyncio.CancelledError:
            break

    log.info("location_mqtt_stopped")


async def _handle_message(ctx, topic: str, payload: object, retain: bool) -> None:
    """Parse one position message and fan it out (state, WS, InfluxDB).

    ``retain`` distinguishes a live publish (the broker delivers it with
    the retain flag cleared) from the broker replaying its stored last
    message when we (re)subscribe. A retained replay keeps the previously
    recorded timestamp so the staleness timer survives proxy restarts; a
    live publish stamps ``time.time()``.
    """
    parts = topic.split("/")
    try:
        loc_id = parts[parts.index("location") + 1]
    except (ValueError, IndexError):
        return

    loc = get_location(ctx.config, loc_id)
    if loc is None:
        log.debug("location_mqtt_unknown_id", location_id=loc_id)
        return
    if not loc.mqtt_coords:
        log.debug("location_mqtt_ignored_static", location_id=loc_id)
        return

    try:
        data = json.loads(payload)
        lat = float(data["lat"])
        lon = float(data["lon"])
    except (json.JSONDecodeError, TypeError, ValueError, KeyError):
        log.warning("location_mqtt_bad_payload", topic=topic, payload=str(payload)[:120])
        return

    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        log.warning("location_mqtt_out_of_range", location_id=loc_id, lat=lat, lon=lon)
        return

    # A retained replay on (re)connect keeps the prior timestamp so the
    # fallback timer is not reset by a proxy restart; a live publish is
    # stamped now.
    prev = ctx.location_positions.get(loc_id) or {}
    if retain:
        ts = prev.get("ts") or time.time()
    else:
        ts = time.time()
    entry = {"lat": lat, "lon": lon, "ts": ts}
    # Carry over the last reverse-geocoded place across this update.
    for key in ("city", "country", "geo_lat", "geo_lon"):
        if prev.get(key) is not None:
            entry[key] = prev[key]
    ctx.location_positions[loc_id] = entry
    _save_positions(ctx)
    log.debug("location_position_update", location_id=loc_id, lat=lat, lon=lon, retained=retain)

    # Live reverse geocoding — resolve the current town/country whenever the
    # Standort has moved far enough that its place name may have changed.
    if _needs_regeocode(prev, lat, lon) and loc_id not in _geocoding_in_flight:
        _geocoding_in_flight.add(loc_id)
        asyncio.create_task(_resolve_place(ctx, loc_id, lat, lon))

    # InfluxDB — record the track. Skip retained replays so an old
    # position is not logged as a fresh point.
    if not retain and ctx.influx_queue is not None:
        try:
            ctx.influx_queue.put_nowait({
                "type": "location",
                "location_name": loc.name or loc_id,
                "lat": lat,
                "lon": lon,
            })
        except asyncio.QueueFull:
            pass

    # WebSocket — move the marker live.
    app = ctx.webapp
    if app is not None:
        from pv_inverter_master.webapp import broadcast_location_position
        await broadcast_location_position(app, loc_id, lat, lon, ts)
