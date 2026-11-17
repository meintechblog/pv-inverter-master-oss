"""Discovery routes: subnet scan, mDNS MQTT discovery and per-type
probe/discover endpoints (Shelly, Sungrow, SENEC, Hoymiles).
"""
from __future__ import annotations

import asyncio

import structlog
from aiohttp import web

from pv_inverter_master.config import Config, InverterEntry, save_config
from pv_inverter_master.modbus_gateway import GatewayError, probe_read
from pv_inverter_master.mdns_discovery import discover_mqtt_brokers
from pv_inverter_master.scanner import ScanConfig, scan_subnet
from pv_inverter_master.shelly_discovery import (
    discover_shelly_devices,
    probe_shelly_device,
)

from .broadcasts import (
    _broadcast_scan_complete,
    _broadcast_scan_error,
    _broadcast_scan_progress,
)
from .reconfigure import _reconfigure_active

log = structlog.get_logger()


async def _run_scan(
    app: web.Application,
    scan_config: ScanConfig,
    auto_add: bool = False,
    subnet_override: str | None = None,
) -> None:
    """Run scan as background task, broadcasting progress via WS.

    If auto_add=True, automatically add supported discovered devices to config
    and start polling. Used for first-run onboarding and manual "Scan & Add".

    subnet_override (optional): CIDR or shorthand passed straight to
    scan_subnet so VPN-reachable remote subnets can be scanned.
    """
    app["_scan_running"] = True
    app_ctx = app["app_ctx"]
    # Pause polling during scan to avoid ESP32 contention (OpenDTU)
    was_paused = app_ctx.polling_paused
    app_ctx.polling_paused = True
    log.info("scan.polling_paused")
    await asyncio.sleep(1)  # Let active polls finish
    try:
        def progress_cb(phase: str, current: int, total: int) -> None:
            asyncio.create_task(_broadcast_scan_progress(app, phase, current, total))

        devices = await scan_subnet(
            scan_config,
            progress_callback=progress_cb,
            subnet_override=subnet_override,
        )

        # Auto-add supported devices that aren't already configured
        if auto_add and devices:
            config: Config = app["config"]
            existing_serials = {inv.serial for inv in config.inverters if inv.serial}
            existing_hosts = {(inv.host, inv.port, inv.unit_id) for inv in config.inverters}
            added = 0

            for dev in devices:
                if not dev.supported:
                    continue

                # Skip if already configured by serial (primary dedup key)
                if dev.serial_number and dev.serial_number in existing_serials:
                    log.info("scan.auto_add_skipped", ip=dev.ip, serial=dev.serial_number, reason="already_configured")
                    continue
                # For Modbus devices (no serial yet), dedup by host:port:unit_id
                if not dev.serial_number and (dev.ip, dev.port, dev.unit_id) in existing_hosts:
                    log.info("scan.auto_add_skipped", ip=dev.ip, reason="host_port_match")
                    continue

                entry = InverterEntry(
                    host=dev.ip,
                    port=dev.port,
                    unit_id=dev.unit_id,
                    enabled=True,
                    manufacturer=dev.manufacturer,
                    model=dev.model,
                    serial=dev.serial_number,
                    firmware_version=dev.firmware_version,
                    type=dev.device_type,
                    name=dev.model if dev.device_type == "opendtu" else "",
                    gateway_host=dev.ip if dev.device_type == "opendtu" else "",
                )
                config.inverters.append(entry)
                added += 1
                log.info("scan.auto_added", device_id=entry.id, ip=dev.ip, manufacturer=dev.manufacturer, model=dev.model, type=dev.device_type)

            if added > 0:
                save_config(app["config_path"], config)
                # Start newly added devices
                for inv in config.inverters:
                    if inv.enabled and inv.id not in {d_id for d_id in app["app_ctx"].devices}:
                        try:
                            await _reconfigure_active(app, config, device_id=inv.id, action="start")
                        except Exception as exc:
                            log.warning("scan.auto_start_failed", device_id=inv.id, error=str(exc))
                log.info("scan.auto_add_complete", added=added, total_inverters=len(config.inverters))

        await _broadcast_scan_complete(app, devices)
    except Exception as e:
        log.error("scanner.background_scan_failed", error=str(e))
        await _broadcast_scan_error(app, str(e))
    finally:
        app["_scan_running"] = False
        # Resume polling
        if not was_paused:
            app_ctx.polling_paused = False
            log.info("scan.polling_resumed")


async def scanner_config_get_handler(request: web.Request) -> web.Response:
    """GET /api/scanner/config -- scanner ports + detected local subnet.

    `local_subnet` is the auto-detected subnet of the host this proxy
    runs on, in CIDR form (e.g. "192.0.2.0/24"). The Add-Device
    discovery UI pre-fills its Subnet field with this so the user sees
    what will be scanned by default and can override it to sweep a
    VPN-reachable remote network instead. Empty string if detection
    fails — the UI then just leaves the field blank.
    """
    config: Config = request.app["config"]
    local_subnet = ""
    try:
        from pv_inverter_master.scanner import detect_subnet
        local_subnet = str(detect_subnet())
    except Exception:
        pass
    return web.json_response({
        "ports": config.scanner.ports,
        "local_subnet": local_subnet,
    })


async def scanner_config_save_handler(request: web.Request) -> web.Response:
    """PUT /api/scanner/config -- update scanner port configuration."""
    try:
        body = await request.json()
    except (ValueError, TypeError) as e:
        return web.json_response(
            {"success": False, "error": f"Invalid request: {e}"}, status=400,
        )

    ports = body.get("ports")
    if not isinstance(ports, list) or not ports:
        return web.json_response(
            {"success": False, "error": "ports must be a non-empty list of integers"},
            status=400,
        )
    for p in ports:
        if not isinstance(p, int) or not (1 <= p <= 65535):
            return web.json_response(
                {"success": False, "error": f"Invalid port: {p} (must be int 1-65535)"},
                status=400,
            )

    config: Config = request.app["config"]
    config.scanner.ports = ports
    save_config(request.app["config_path"], config)
    return web.json_response({"success": True})


async def scanner_discover_handler(request: web.Request) -> web.Response:
    """POST /api/scanner/discover -- trigger background subnet scan.

    Body (all fields optional):
        ports: list[int] — Modbus ports to probe (default: from config)
        scan_unit_ids: list[int] — Modbus unit IDs to verify (default: [1])
        subnet: str — CIDR ("192.0.2.0/24"), shorthand ("192.0.2.x" /
            "192.0.2.*"), or single IP ("192.0.2.101" -> /24). Empty
            string or omitted = auto-detect local subnet. Useful for
            scanning VPN-reachable remote networks.
        auto_add: bool
    """
    app = request.app
    if app.get("_scan_running"):
        return web.json_response({"error": "Scan already running"}, status=409)

    config: Config = app["config"]
    skip_ips = {inv.host for inv in config.inverters if inv.enabled}

    try:
        body = await request.json()
    except Exception:
        body = {}

    ports = body.get("ports", config.scanner.ports)
    scan_unit_ids = body.get("scan_unit_ids", [1])
    subnet_override = body.get("subnet") or None

    # Validate subnet eagerly so the user gets a 400 instead of a silent
    # background failure.
    if subnet_override:
        from pv_inverter_master.scanner import parse_subnet_override
        try:
            parse_subnet_override(subnet_override)
        except ValueError as e:
            return web.json_response(
                {"error": f"Invalid subnet: {e}"}, status=400,
            )

    scan_config = ScanConfig(ports=ports, skip_ips=skip_ips, scan_unit_ids=scan_unit_ids)

    # Auto-add: default True if no inverters configured (first-run), else require explicit request
    has_inverters = len(config.inverters) > 0
    auto_add = body.get("auto_add", not has_inverters)

    log.info(
        "user_action", action="scan_triggered",
        ports=ports, subnet=subnet_override or "auto", auto_add=auto_add,
    )
    asyncio.create_task(
        _run_scan(app, scan_config, auto_add=auto_add, subnet_override=subnet_override),
    )
    return web.json_response({
        "status": "started",
        "auto_add": auto_add,
        "subnet": subnet_override or "auto",
    })


async def mqtt_discover_handler(request: web.Request) -> web.Response:
    """POST /api/mqtt/discover -- scan LAN for MQTT brokers via mDNS."""
    try:
        brokers = await discover_mqtt_brokers(timeout=3.0)
        return web.json_response({"success": True, "brokers": brokers})
    except Exception as e:
        log.error("mdns_scan_failed", error=str(e))
        return web.json_response(
            {"success": False, "error": f"mDNS scan failed: {e}"},
            status=500,
        )


async def shelly_probe_handler(request: web.Request) -> web.Response:
    """POST /api/shelly/probe -- probe a Shelly device at given host."""
    try:
        body = await request.json()
        host = body["host"]
    except (KeyError, ValueError, TypeError) as e:
        return web.json_response({"error": f"Invalid request: {e}"}, status=400)
    result = await probe_shelly_device(host)
    return web.json_response(result)


async def shelly_discover_handler(request: web.Request) -> web.Response:
    """POST /api/shelly/discover -- scan for Shelly devices.

    Body (optional): {"subnet": "203.0.113.0/24"}. Without a subnet this
    browses mDNS (link-local only). With one it IP-sweeps that subnet, so
    VPN-reachable remote networks can be scanned too. Accepts CIDR,
    "x"-shorthand and a bare IP.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}

    subnet_override = body.get("subnet") or None
    if subnet_override:
        from pv_inverter_master.scanner import parse_subnet_override
        try:
            parse_subnet_override(subnet_override)
        except ValueError as e:
            return web.json_response(
                {"success": False, "error": f"Invalid subnet: {e}"}, status=400,
            )

    try:
        config: Config = request.app["config"]
        existing = {
            (inv.host, inv.shelly_channel)
            for inv in config.inverters if inv.type == "shelly"
        }
        devices = await discover_shelly_devices(
            timeout=3.0, skip=existing, subnet_override=subnet_override,
        )
        log.info(
            "user_action", action="shelly_discover",
            subnet=subnet_override or "mdns", found=len(devices),
        )
        return web.json_response({"success": True, "devices": devices})
    except Exception as e:
        log.error("shelly_discover_failed", error=str(e))
        return web.json_response(
            {"success": False, "error": f"Shelly discovery failed: {e}"},
            status=500,
        )


async def sungrow_probe_handler(request: web.Request) -> web.Response:
    """POST /api/sungrow/probe -- probe a Sungrow inverter via Modbus TCP."""
    try:
        body = await request.json()
        host = body["host"]
        port = int(body.get("port", 502))
        unit_id = int(body.get("unit_id", 1))
    except (KeyError, ValueError, TypeError) as e:
        return web.json_response({"error": f"Invalid request: {e}"}, status=400)
    try:
        # Via the host's gateway (see config_test_handler): no second session
        # to a configured inverter. FC04 read of the device type at wire
        # address 5000 (doc 5001), 2 registers; unit id goes out as the
        # pymodbus-3.x ``device_id`` (the old ``slave=`` kwarg is gone).
        try:
            resp = await probe_read(
                host, port, unit_id, 5000, 2, input_registers=True, timeout=3,
            )
        except GatewayError as e:
            return web.json_response({"success": False, "error": f"Gateway: {e}"})
        model = "Sungrow Inverter"
        if not resp.isError() and hasattr(resp, "registers"):
            device_code = resp.registers[0]
            model = f"Sungrow (code {device_code:#06x})"
        return web.json_response({
            "success": True,
            "manufacturer": "Sungrow",
            "model": model,
            "host": host,
            "port": port,
            "unit_id": unit_id,
        })
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)})


async def senec_probe_handler(request: web.Request) -> web.Response:
    """POST /api/senec/probe -- probe a SENEC appliance at the given host."""
    try:
        body = await request.json()
        host = body["host"]
    except (KeyError, ValueError, TypeError) as e:
        return web.json_response({"error": f"Invalid request: {e}"}, status=400)

    from pv_inverter_master.plugins.senec import probe_senec
    result = await probe_senec(host)
    if result is None:
        return web.json_response({
            "success": False,
            "error": "Keine SENEC-Anlage unter dieser Adresse erreichbar",
        })
    return web.json_response({
        "success": True,
        "manufacturer": "SENEC",
        "model": "SENEC.Home",
        "host": host,
        "serial": result.get("serial", ""),
        "pv_power_w": result.get("pv_power_w", 0),
        "producing": result.get("producing", False),
    })


async def senec_discover_handler(request: web.Request) -> web.Response:
    """POST /api/senec/discover -- scan a subnet for SENEC appliances.

    Body (optional): {"subnet": "203.0.113.0/24"}. Empty / omitted =
    auto-detect the local subnet. Accepts CIDR, "x"-shorthand and a bare
    IP (-> /24), so VPN-reachable remote networks can be swept too.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}

    subnet_override = body.get("subnet") or None
    if subnet_override:
        from pv_inverter_master.scanner import parse_subnet_override
        try:
            parse_subnet_override(subnet_override)
        except ValueError as e:
            return web.json_response(
                {"success": False, "error": f"Invalid subnet: {e}"}, status=400,
            )

    config: Config = request.app["config"]
    skip_ips = {inv.host for inv in config.inverters if inv.type == "senec"}

    from pv_inverter_master.plugins.senec import discover_senec_devices
    try:
        devices = await discover_senec_devices(
            subnet_override=subnet_override, skip_ips=skip_ips,
        )
        log.info(
            "user_action", action="senec_discover",
            subnet=subnet_override or "auto", found=len(devices),
        )
        return web.json_response({"success": True, "devices": devices})
    except Exception as e:
        log.error("senec_discover_failed", error=str(e))
        return web.json_response(
            {"success": False, "error": f"Discovery failed: {e}"}, status=500,
        )


async def hoymiles_probe_handler(request: web.Request) -> web.Response:
    """Probe a Hoymiles HMS WLAN inverter before adding it.

    Body: {"host": "198.51.100.128"}
    Returns: {"success": bool, "error"?: str, "serial"?: str,
              "firmware"?: str, "panels"?: int, "sleeping"?: bool}

    Treats a TCP timeout as success=False with sleeping=true so the UI
    can still let the user save the entry (we re-poll once the inverter
    wakes up).
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"success": False, "error": "Invalid JSON"}, status=400)

    host = (body.get("host") or "").strip()
    if not host:
        return web.json_response({"success": False, "error": "Host required"}, status=400)

    from hoymiles_wifi.dtu import DTU
    dtu = DTU(host=host)
    try:
        info = await asyncio.wait_for(dtu.async_app_information_data(), timeout=8.0)
    except (asyncio.TimeoutError, OSError, ConnectionError):
        return web.json_response({
            "success": False,
            "sleeping": True,
            "error": "No response on TCP 10081 — inverter may be asleep (no DC, night) or IP wrong. You can still save the entry; polling will start when the inverter wakes up.",
        })
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)})

    if info is None:
        return web.json_response({
            "success": False,
            "sleeping": True,
            "error": "Empty response (CRC mismatch or device asleep)",
        })

    dtu_info = getattr(info, "dtu_info", None)
    pv_info = list(getattr(info, "pv_info", []) or [])
    serial = ""
    firmware = ""
    if dtu_info is not None:
        firmware = str(getattr(dtu_info, "dtu_sw_version", "") or "")
    if pv_info:
        serial = str(getattr(pv_info[0], "pv_serial_number", "") or "")

    # real_data carries the authoritative DTU serial and PV-channel count.
    # app_information_data's pv_info under-reports on some firmware revs
    # (FW 267 lists a single pv_info entry for a 2-MPPT HMS-800W-2T), so
    # prefer the pv_data length from real_data when we can get it.
    panels_from_realdata = 0
    try:
        rd = await asyncio.wait_for(dtu.async_get_real_data_new(), timeout=8.0)
        if rd is not None:
            dev_serial = str(getattr(rd, "device_serial_number", "") or "")
            if dev_serial:
                serial = dev_serial
            panels_from_realdata = len(list(getattr(rd, "pv_data", []) or []))
    except Exception:
        pass

    return web.json_response({
        "success": True,
        "serial": serial,
        "firmware": firmware,
        "panels": panels_from_realdata or len(pv_info) or 2,
    })


async def hoymiles_discover_handler(request: web.Request) -> web.Response:
    """POST /api/hoymiles/discover — scan a subnet for HMS WLAN inverters.

    Body (optional): {"subnet": "198.51.100.0/24"}. Empty / omitted =
    auto-detect the local subnet. Accepts CIDR, "x"-shorthand and a bare
    IP (→ /24), so VPN-reachable remote networks can be swept too.
    Returns {success, devices:[{host, serial, panels, producing,
    ac_power_w, sleeping}]}.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}

    subnet_override = body.get("subnet") or None
    if subnet_override:
        from pv_inverter_master.scanner import parse_subnet_override
        try:
            parse_subnet_override(subnet_override)
        except ValueError as e:
            return web.json_response(
                {"success": False, "error": f"Invalid subnet: {e}"}, status=400,
            )

    config: Config = request.app["config"]
    skip_ips = {inv.host for inv in config.inverters if inv.type == "hoymiles"}

    from pv_inverter_master.plugins.hoymiles import discover_hoymiles_devices
    try:
        devices = await discover_hoymiles_devices(
            subnet_override=subnet_override, skip_ips=skip_ips,
        )
        log.info(
            "user_action", action="hoymiles_discover",
            subnet=subnet_override or "auto", found=len(devices),
        )
        return web.json_response({"success": True, "devices": devices})
    except Exception as e:
        log.error("hoymiles_discover_failed", error=str(e))
        return web.json_response(
            {"success": False, "error": f"Discovery failed: {e}"}, status=500,
        )

