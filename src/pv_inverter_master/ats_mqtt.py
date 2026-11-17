"""MQTT subscriber for the central Auto-Transfer-Switch (ATS) state.

An installation may have a single transfer switch that flips the entire
house topology between two modes:

  * ``1`` — Netzparallel: house is on AC.In (grid-parallel). All
    inverters effectively feed in alongside the grid; the grid meter
    sees the net result.
  * ``2`` — ESS (AC.Out, Ersatzstrom): house runs from Multiplus AC.Out
    in island mode. Grid is disconnected; all inverters feed into the
    Multiplus's AC.Out bus.
  * ``-1`` — Error / unknown / undefined state.

Loxone publishes the live state to a fixed MQTT topic; we cache it on
the AppContext so Phasen-Detective can record what topology was in
play when a detection ran (and warn if the topology has flipped since
the last verification), and the UI can surface the current mode in a
status banner.
"""
from __future__ import annotations

import asyncio
import json
import os
import time

import aiomqtt
import structlog

log = structlog.get_logger(component="ats_mqtt")

# ATS topic — override with the PV_INVERTER_ATS_TOPIC environment variable
# if your installation publishes the transfer-switch state elsewhere.
ATS_TOPIC = os.environ.get("PV_INVERTER_ATS_TOPIC", "home/ats/state")

# Allowed states. Anything else lands as None to flag a parse error
# without lying about the topology.
_STATE_GRID = 1     # Netzparallel
_STATE_ISLAND = 2   # ESS / AC.Out / Inselbetrieb
_STATE_ERROR = -1   # Loxone reports an error
_VALID_STATES = {_STATE_GRID, _STATE_ISLAND, _STATE_ERROR}


def ats_label(state: int | None) -> str:
    """Human-readable label for the API + UI."""
    if state == _STATE_GRID:
        return "Netzparallel (AC.In)"
    if state == _STATE_ISLAND:
        return "Inselbetrieb (AC.Out)"
    if state == _STATE_ERROR:
        return "Fehler"
    return "unbekannt"


async def ats_mqtt_loop(ctx, mqtt_config) -> None:
    """Background task: subscribe to the ATS state topic.

    Uses the same broker as the location subscriber and MQTT publisher
    (``mqtt.example`` by default) so this doesn't add a second
    broker dependency. Reconnects with exponential backoff on errors.
    """
    backoff = 1.0
    max_backoff = 30.0

    while not ctx.shutdown_event.is_set():
        try:
            async with aiomqtt.Client(
                hostname=mqtt_config.host,
                port=mqtt_config.port,
                identifier=f"{mqtt_config.client_id}-atssub",
                keepalive=30,
            ) as client:
                await client.subscribe(ATS_TOPIC, qos=1)
                backoff = 1.0
                log.info("ats_mqtt_connected", host=mqtt_config.host, topic=ATS_TOPIC)
                async for message in client.messages:
                    if ctx.shutdown_event.is_set():
                        break
                    _handle_message(ctx, message.payload)
        except aiomqtt.MqttError as e:
            log.warning("ats_mqtt_disconnected", error=str(e), backoff=backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)
        except asyncio.CancelledError:
            break

    log.info("ats_mqtt_stopped")


def _handle_message(ctx, payload: object) -> None:
    """Parse a single ATS state message and stash it on the AppContext.

    Loxone's MQTT bridge wraps every value in a metadata envelope:
    ``{"value": 1, "name": "Hausversorgung", "type": "InfoOnlyAnalog",
       "uuid": "...", "room": "Zentral"}``. We parse the envelope first
    and fall back to a plain integer payload so the parser also works
    for any other bridge that publishes a raw value.
    """
    try:
        raw = payload.decode("utf-8") if isinstance(payload, (bytes, bytearray)) else str(payload)
    except (UnicodeDecodeError, AttributeError):
        log.warning("ats_mqtt_bad_payload", payload=str(payload)[:120])
        return

    raw = raw.strip()
    value: int | None = None
    if raw.startswith("{"):
        try:
            envelope = json.loads(raw)
            value = int(envelope.get("value"))
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    if value is None:
        try:
            value = int(raw)
        except ValueError:
            log.warning("ats_mqtt_bad_payload", payload=raw[:120])
            return

    if value not in _VALID_STATES:
        log.warning("ats_mqtt_unknown_state", value=value)
        return

    prev = ctx.ats_state
    ctx.ats_state = value
    ctx.ats_state_ts = time.time()
    if prev != value:
        log.info(
            "ats_state_changed",
            prev=prev,
            new=value,
            label=ats_label(value),
        )
