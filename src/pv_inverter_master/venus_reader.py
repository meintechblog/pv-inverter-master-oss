"""Read Venus OS settings via MQTT (real-time, no polling).

Subscribes to Venus OS MQTT topics and updates app_ctx on every change.
Replaces the old Modbus TCP polling approach.
"""
from __future__ import annotations

import asyncio
import json
import socket
import struct
import time

import structlog

logger = structlog.get_logger(component="venus_reader")

def _mqtt_connect(host: str, port: int = 1883, client_id: str = "pv-proxy-sub") -> socket.socket:
    """Connect to MQTT broker and return socket."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(5)
    s.connect((host, port))
    cid = client_id.encode()
    payload = struct.pack("!H", 4) + b"MQTT" + bytes([4, 2, 0, 60])
    payload += struct.pack("!H", len(cid)) + cid
    s.send(bytes([0x10, len(payload)]) + payload)
    connack = s.recv(4)
    if len(connack) < 4 or connack[3] != 0:
        s.close()
        raise ConnectionError(
            f"MQTT CONNACK rejected: rc={connack[3] if len(connack) >= 4 else 'short'}"
        )
    return s


def _mqtt_subscribe(s: socket.socket, topics: list[str]) -> None:
    """Subscribe to MQTT topics."""
    msg_id = 1
    sub = struct.pack("!H", msg_id)
    for t in topics:
        tb = t.encode()
        sub += struct.pack("!H", len(tb)) + tb + bytes([0])
    rem = len(sub)
    hdr = bytearray([0x82])
    while rem > 0:
        b = rem % 128
        rem //= 128
        if rem > 0:
            b |= 0x80
        hdr.append(b)
    s.send(bytes(hdr) + sub)


def _mqtt_publish(s: socket.socket, topic: str, message: str = "") -> None:
    """Publish an MQTT message."""
    tb = topic.encode()
    mb = message.encode()
    rem = 2 + len(tb) + len(mb)
    hdr = bytearray([0x30])
    while rem > 0:
        b = rem % 128
        rem //= 128
        if rem > 0:
            b |= 0x80
        hdr.append(b)
    s.send(bytes(hdr) + struct.pack("!H", len(tb)) + tb + mb)


def _parse_mqtt_messages(data: bytes) -> list[tuple[str, dict]]:
    """Parse MQTT PUBLISH packets from raw data."""
    messages = []
    i = 0
    while i < len(data):
        if data[i] & 0xF0 == 0x30:  # PUBLISH
            mult = 1
            rem_len = 0
            j = i + 1
            while j < len(data):
                byte = data[j]
                rem_len += (byte & 0x7F) * mult
                mult *= 128
                j += 1
                if not (byte & 0x80):
                    break
            hdr_len = j - i
            if j + 2 > len(data):
                break
            topic_len = struct.unpack("!H", data[j : j + 2])[0]
            end_pos = i + hdr_len + rem_len
            if j + 2 + topic_len > len(data) or end_pos > len(data):
                break
            topic = data[j + 2 : j + 2 + topic_len].decode(errors="replace")
            payload_bytes = data[j + 2 + topic_len : end_pos]
            try:
                payload = json.loads(payload_bytes)
                messages.append((topic, payload))
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
            i = end_pos
        else:
            i += 1
    return messages


async def discover_portal_id(host: str, port: int = 1883, timeout: float = 10.0) -> str | None:
    """Auto-discover Venus OS portal ID via MQTT wildcard subscription."""
    loop = asyncio.get_event_loop()
    try:
        def _discover_blocking():
            s = _mqtt_connect(host, port, client_id="pv-proxy-discover")
            _mqtt_subscribe(s, ["N/+/system/0/Serial"])
            s.settimeout(timeout)
            try:
                data = s.recv(8192)
                for topic, payload in _parse_mqtt_messages(data):
                    if "/system/0/Serial" in topic:
                        parts = topic.split("/")
                        if len(parts) >= 2:
                            portal_id = parts[1]
                            logger.info("portal_id_discovered", portal_id=portal_id)
                            return portal_id
            except socket.timeout:
                logger.warning("portal_id_discovery_timeout", host=host, timeout=timeout)
            finally:
                try:
                    s.close()
                except Exception:
                    pass
            return None
        return await asyncio.wait_for(
            loop.run_in_executor(None, _discover_blocking),
            timeout=timeout + 2,
        )
    except (asyncio.TimeoutError, Exception) as e:
        logger.warning("portal_id_discovery_error", error=str(e))
        return None


async def venus_mqtt_loop(app_ctx: object, host: str, port: int, portal_id: str) -> None:
    """Background task: subscribe to Venus OS MQTT and update settings in real-time."""
    if not host:
        logger.info("venus_mqtt_disabled", reason="no host configured")
        app_ctx.venus_mqtt_connected = False
        return

    portal = portal_id

    while True:
        # Auto-discover portal ID if not provided
        if not portal:
            discovered = await discover_portal_id(host, port)
            if discovered:
                portal = discovered
            else:
                logger.warning("portal_id_discovery_failed", host=host)
                app_ctx.venus_mqtt_connected = False
                await asyncio.sleep(30)
                continue

        break

    prefix = f"N/{portal}"

    # Topics to subscribe (wildcards for grid power)
    sub_topics = [
        f"{prefix}/settings/0/Settings/CGwacs/#",
        f"{prefix}/system/0/Ac/Grid/#",
        f"{prefix}/hub4/0/#",
        f"{prefix}/pvinverter/20/Ac/PowerLimit",
        f"{prefix}/vebus/+/State",
        # GON Venus Health Dashboard — extended discovery topics
        f"{prefix}/system/0/+",
        f"{prefix}/pvinverter/+/+",
        f"{prefix}/pvinverter/+/Mgmt/Connection",
        f"{prefix}/pvinverter/+/Ac/Power",
        f"{prefix}/pvinverter/+/Ac/MaxPower",
        f"{prefix}/pvinverter/+/Ac/PowerLimit",
        f"{prefix}/pvinverter/+/Position",
        f"{prefix}/grid/+/+",
        f"{prefix}/grid/+/Ac/Power",
        f"{prefix}/grid/+/Ac/L1/Power",
        f"{prefix}/grid/+/Ac/L2/Power",
        f"{prefix}/grid/+/Ac/L3/Power",
        f"{prefix}/grid/+/Mgmt/Connection",
        f"{prefix}/grid/+/ProductName",
        f"{prefix}/grid/+/DeviceInstance",
        f"{prefix}/vebus/+/Dc/0/Power",
        f"{prefix}/vebus/+/Mgmt/Connection",
        f"{prefix}/vebus/+/ProductName",
        f"{prefix}/vebus/+/DeviceInstance",
        f"{prefix}/solarcharger/+/ProductName",
        f"{prefix}/solarcharger/+/DeviceInstance",
        f"{prefix}/solarcharger/+/Dc/0/Power",
        f"{prefix}/battery/+/ProductName",
        f"{prefix}/battery/+/CustomName",
        f"{prefix}/battery/+/DeviceInstance",
        f"{prefix}/battery/+/Dc/0/Power",
        f"{prefix}/battery/+/Soc",
        f"{prefix}/vebus/+/Dc/0/Power",
        f"{prefix}/vebus/+/Ac/Out/P",
        f"{prefix}/settings/0/Settings/Fronius/#",
    ]

    # R/ topics to request initial values
    request_topics = [
        f"R/{portal}/settings/0/Settings/CGwacs/MaxFeedInPower",
        f"R/{portal}/settings/0/Settings/CGwacs/PreventFeedback",
        f"R/{portal}/settings/0/Settings/CGwacs/OvervoltageFeedIn",
        f"R/{portal}/settings/0/Settings/CGwacs/MaxDischargePower",
        f"R/{portal}/hub4/0/PvPowerLimiterActive",
        f"R/{portal}/system/0/Ac/Grid/L1/Power",
        f"R/{portal}/system/0/Ac/Grid/L2/Power",
        f"R/{portal}/system/0/Ac/Grid/L3/Power",
        f"R/{portal}/pvinverter/20/Ac/PowerLimit",
        # GON Venus Health — initial value requests for system + Fronius inverter settings
        f"R/{portal}/system/0/Serial",
        f"R/{portal}/system/0/SystemType",
        f"R/{portal}/system/0/FirmwareVersion",
        f"R/{portal}/system/0/Hostname",
    ]
    # Fronius inverter settings live under /Settings/Fronius/Inverters/<idx>/...
    # Venus's dbus-fronius daemon uses literal "I" for the auto-registered
    # entry (and "I<serial>" for older ones) — older fleet installs may also
    # have integer indexes. Request all three so we observe whichever the
    # proxy registered under (without it limiter_enabled / position stay null).
    for inv_idx in (0, 1, "I"):
        request_topics.extend([
            f"R/{portal}/settings/0/Settings/Fronius/Inverters/{inv_idx}/EnableLimiter",
            f"R/{portal}/settings/0/Settings/Fronius/Inverters/{inv_idx}/LimiterSupported",
            f"R/{portal}/settings/0/Settings/Fronius/Inverters/{inv_idx}/CustomName",
            f"R/{portal}/settings/0/Settings/Fronius/Inverters/{inv_idx}/Position",
        ])

    # State
    state = {
        "max_feed_in_w": -1,
        "prevent_feedback": False,
        "overvoltage_feed_in": False,
        "max_inverter_w": -1,
        "limiter_active": False,
        "grid_l1_w": 0,
        "grid_l2_w": 0,
        "grid_l3_w": 0,
        "grid_feed_in_w": 0,
        "pv_limit_w": None,
        "ac_setpoint_w": 0,
        "ess_available": False,
        "vebus_last_ts": 0,
        "ts": time.time(),
        "connected_since_ts": None,
        # GON Venus Health Dashboard — extended discovery state
        "system": {
            "serial": None,
            "type": None,
            "firmware": None,
            "hostname": None,
        },
        "peers": {
            "pvinverter": {},
            "vebus": {},
            "grid_meter": {},
            "solarcharger": {},
            "battery": {},
        },
        "limiter": {
            "enabled": None,
            "supported": None,
            "custom_name": None,
            "position": None,
            "inverter_index": None,
        },
    }

    def _peer_instance(topic: str, peer_segment: str) -> str | None:
        """Extract the device-instance segment immediately after a peer-type segment.

        Topics look like ``N/<portal>/pvinverter/20/Ac/PowerLimit`` — the segment
        after ``pvinverter`` is the device instance.
        """
        parts = topic.split("/")
        try:
            idx = parts.index(peer_segment)
        except ValueError:
            return None
        if idx + 1 >= len(parts):
            return None
        return parts[idx + 1]

    def _update_peer(peer_type: str, instance: str, field: str, value) -> None:
        bucket = state["peers"].setdefault(peer_type, {})
        peer = bucket.setdefault(instance, {
            "device_instance": None,
            "product_name": None,
            "connection_string": None,
            "custom_name": None,
            "ac_power_w": None,
            "last_ts": 0.0,
        })
        peer[field] = value
        peer["last_ts"] = time.time()

    def update_from_topic(topic: str, payload: dict) -> None:
        val = payload.get("value")
        if val is None:
            return

        # ----- System info ----------------------------------------------------
        if "/system/0/Serial" in topic:
            state["system"]["serial"] = val
        elif "/system/0/SystemType" in topic:
            state["system"]["type"] = val
        elif "/system/0/FirmwareVersion" in topic:
            state["system"]["firmware"] = val
        elif "/system/0/Hostname" in topic:
            state["system"]["hostname"] = val

        # ----- Fronius inverter settings (limiter block) ----------------------
        # Topic shape: .../Settings/Fronius/Inverters/<idx>/<Field>
        # idx is usually the literal string "I" for the auto-registered entry;
        # older installs sometimes use integer indexes. Keep idx as a string
        # so both forms pass through the lock check.
        if "/Settings/Fronius/Inverters/" in topic:
            parts = topic.split("/")
            try:
                inv_idx = parts[parts.index("Inverters") + 1]
                field = parts[parts.index("Inverters") + 2]
            except (ValueError, IndexError):
                inv_idx = None
                field = None
            if inv_idx and field:
                # Only track the inverter index that actually publishes values.
                if state["limiter"]["inverter_index"] in (None, inv_idx):
                    state["limiter"]["inverter_index"] = inv_idx
                    if field == "EnableLimiter":
                        state["limiter"]["enabled"] = bool(val)
                    elif field == "LimiterSupported":
                        state["limiter"]["supported"] = bool(val)
                    elif field == "CustomName":
                        state["limiter"]["custom_name"] = val
                    elif field == "Position":
                        state["limiter"]["position"] = val

        # ----- pvinverter peer ------------------------------------------------
        if "/pvinverter/" in topic:
            inst = _peer_instance(topic, "pvinverter")
            if inst is not None:
                if topic.endswith("/Mgmt/Connection"):
                    _update_peer("pvinverter", inst, "connection_string", val)
                elif topic.endswith("/ProductName"):
                    _update_peer("pvinverter", inst, "product_name", val)
                elif topic.endswith("/CustomName"):
                    _update_peer("pvinverter", inst, "custom_name", val)
                elif topic.endswith("/DeviceInstance"):
                    _update_peer("pvinverter", inst, "device_instance", val)
                elif topic.endswith("/Ac/Power"):
                    _update_peer("pvinverter", inst, "ac_power_w", val)
                elif topic.endswith("/Ac/MaxPower"):
                    _update_peer("pvinverter", inst, "max_power_w", val)
                elif topic.endswith("/Ac/PowerLimit"):
                    _update_peer("pvinverter", inst, "power_limit_w", val)
                elif topic.endswith("/Position"):
                    _update_peer("pvinverter", inst, "position", val)

        # ----- grid meter peer ------------------------------------------------
        if "/grid/" in topic and "/system/0/Ac/Grid" not in topic:
            inst = _peer_instance(topic, "grid")
            if inst is not None:
                if topic.endswith("/ProductName"):
                    _update_peer("grid_meter", inst, "product_name", val)
                elif topic.endswith("/DeviceInstance"):
                    _update_peer("grid_meter", inst, "device_instance", val)
                elif topic.endswith("/Ac/Power"):
                    _update_peer("grid_meter", inst, "ac_power_w", val)
                elif topic.endswith("/Ac/L1/Power"):
                    _update_peer("grid_meter", inst, "ac_l1_power_w", val)
                    state["grid_l1_w"] = val
                elif topic.endswith("/Ac/L2/Power"):
                    _update_peer("grid_meter", inst, "ac_l2_power_w", val)
                    state["grid_l2_w"] = val
                elif topic.endswith("/Ac/L3/Power"):
                    _update_peer("grid_meter", inst, "ac_l3_power_w", val)
                    state["grid_l3_w"] = val

        # ----- solarcharger peer (presence + DC power) ------------------------
        if "/solarcharger/" in topic:
            inst = _peer_instance(topic, "solarcharger")
            if inst is not None:
                if topic.endswith("/ProductName"):
                    _update_peer("solarcharger", inst, "product_name", val)
                elif topic.endswith("/DeviceInstance"):
                    _update_peer("solarcharger", inst, "device_instance", val)
                elif topic.endswith("/Dc/0/Power"):
                    _update_peer("solarcharger", inst, "dc_power_w", val)

        # ----- battery peers (multi-instance: BMS + Aggregator + ...) ----------
        if "/battery/" in topic:
            inst = _peer_instance(topic, "battery")
            if inst is not None:
                if topic.endswith("/ProductName"):
                    _update_peer("battery", inst, "product_name", val)
                elif topic.endswith("/CustomName"):
                    _update_peer("battery", inst, "custom_name", val)
                elif topic.endswith("/DeviceInstance"):
                    _update_peer("battery", inst, "device_instance", val)
                elif topic.endswith("/Dc/0/Power"):
                    _update_peer("battery", inst, "dc_power_w", val)
                elif topic.endswith("/Soc"):
                    _update_peer("battery", inst, "soc_pct", val)

        # Track VE.Bus (Multi/Quattro) presence → ESS availability
        if "/vebus/" in topic:
            state["vebus_last_ts"] = time.time()
            state["ess_available"] = True
            inst = _peer_instance(topic, "vebus")
            if inst is not None:
                if topic.endswith("/ProductName"):
                    _update_peer("vebus", inst, "product_name", val)
                elif topic.endswith("/DeviceInstance"):
                    _update_peer("vebus", inst, "device_instance", val)
                elif topic.endswith("/Dc/0/Power"):
                    _update_peer("vebus", inst, "dc_power_w", val)
                elif topic.endswith("/Ac/Out/P"):
                    _update_peer("vebus", inst, "ac_out_power_w", val)
                else:
                    # touch the peer so presence is recorded even for /State pings
                    _update_peer("vebus", inst, "last_ts", time.time())
        key = topic.split("/")[-1]
        if key == "MaxFeedInPower":
            state["max_feed_in_w"] = val if val >= 0 else -1
        elif key == "PreventFeedback":
            state["prevent_feedback"] = bool(val)
        elif key == "OvervoltageFeedIn":
            state["overvoltage_feed_in"] = bool(val)
        elif key == "MaxDischargePower":
            state["max_inverter_w"] = val if val >= 0 else -1
        elif key == "PvPowerLimiterActive":
            state["limiter_active"] = bool(val)
        elif key == "AcPowerSetPoint":
            state["ac_setpoint_w"] = val
        elif key == "PowerLimit":
            state["pv_limit_w"] = val
        elif "Grid" in topic:
            if "L1/Power" in topic:
                state["grid_l1_w"] = val
            elif "L2/Power" in topic:
                state["grid_l2_w"] = val
            elif "L3/Power" in topic:
                state["grid_l3_w"] = val
            # Calculate total feed-in (negative grid = exporting)
            total = state["grid_l1_w"] + state["grid_l2_w"] + state["grid_l3_w"]
            state["grid_feed_in_w"] = max(0, -total)
        state["ts"] = time.time()

    while True:
        try:
            s = _mqtt_connect(host, port)
            logger.info("venus_mqtt_connected", host=host)
            app_ctx.venus_mqtt_connected = True
            now_ts = time.time()
            state["connected_since_ts"] = now_ts
            app_ctx.venus_connected_since_ts = now_ts

            _mqtt_subscribe(s, sub_topics)

            # Request initial values
            for rt in request_topics:
                _mqtt_publish(s, rt, "")

            s.settimeout(1)
            last_keepalive_ts = 0.0

            while True:
                # Send a keepalive every 4 s independent of recv activity —
                # Venus only continues to push dynamic topics (per-phase grid
                # power, vebus DC etc.) while it sees periodic R/keepalive
                # requests; if all our time goes into recv() the
                # socket.timeout branch never runs and the topics fall silent.
                now = time.time()
                if now - last_keepalive_ts >= 4.0:
                    try:
                        _mqtt_publish(s, f"R/{portal}/keepalive", "")
                        last_keepalive_ts = now
                    except Exception:
                        break
                try:
                    data = s.recv(8192)
                    if not data:
                        break
                    for topic, payload in _parse_mqtt_messages(data):
                        update_from_topic(topic, payload)
                    app_ctx.venus_settings = dict(state)
                except socket.timeout:
                    if state["vebus_last_ts"] > 0 and time.time() - state["vebus_last_ts"] > 30:
                        state["ess_available"] = False
                    try:
                        s.send(bytes([0xC0, 0x00]))
                    except Exception:
                        break
                await asyncio.sleep(0.1)

        except Exception as e:
            app_ctx.venus_mqtt_connected = False
            app_ctx.venus_connected_since_ts = None
            state["connected_since_ts"] = None
            logger.debug("venus_mqtt_error", error=str(e))

        # Reconnect after 5s
        await asyncio.sleep(5)
