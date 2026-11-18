"""Application assembler: wires middleware, app state and the route
table, then returns a configured aiohttp ``AppRunner``.
"""
from __future__ import annotations

import time
import weakref

from aiohttp import web

from pv_inverter_master.config import Config
from pv_inverter_master.updater.progress import start_broadcaster, stop_broadcaster
from pv_inverter_master.updater.security import csrf_middleware

from .origin_guard import origin_guard_middleware

from .routes_config import (
    config_export_handler,
    config_get_handler,
    config_import_handler,
    config_save_handler,
    config_test_handler,
    inverter_export_handler,
    inverter_import_handler,
)
from .routes_control import (
    power_clamp_handler,
    power_limit_handler,
    venus_dbus_handler,
    venus_lock_handler,
    venus_write_handler,
)
from .routes_core import (
    dashboard_handler,
    health_handler,
    index_handler,
    static_handler,
    status_handler,
    ws_handler,
)
from .routes_devices import (
    device_snapshot_handler,
    devices_list_handler,
    inverter_reboot_handler,
    inverters_add_handler,
    inverters_delete_handler,
    inverters_list_handler,
    inverters_update_handler,
    virtual_snapshot_handler,
)
from .routes_discovery import (
    hoymiles_discover_handler,
    hoymiles_probe_handler,
    mqtt_discover_handler,
    scanner_config_get_handler,
    scanner_config_save_handler,
    scanner_discover_handler,
    senec_discover_handler,
    senec_probe_handler,
    shelly_discover_handler,
    shelly_probe_handler,
    sungrow_probe_handler,
)
from .routes_locations import (
    geocode_handler,
    location_export_handler,
    location_image_delete_handler,
    location_image_get_handler,
    location_image_upload_handler,
    location_import_handler,
    location_set_enabled_handler,
    location_subnet_check_handler,
    locations_add_handler,
    locations_delete_handler,
    locations_list_handler,
    locations_update_handler,
)
from .routes_plugins import (
    hoymiles_power_handler,
    hoymiles_status_handler,
    opendtu_inverters_handler,
    opendtu_power_handler,
    opendtu_status_handler,
    opendtu_test_auth_handler,
    shelly_switch_handler,
)
from .routes_phase_detect import (
    phase_detect_start_handler,
    phase_detect_status_handler,
)
from .routes_registers import device_registers_handler, registers_handler
from .routes_venus import venus_health_handler
from .routes_updater import (
    update_available_handler,
    update_check_handler,
    update_config_get_handler,
    update_config_patch_handler,
    update_rollback_handler,
    update_start_handler,
    update_status_handler,
    version_handler,
)


async def create_webapp(
    app_ctx: object,
    config: Config,
    config_path: str,
) -> web.AppRunner:
    """Create and set up the aiohttp webapp.

    Returns an AppRunner (caller manages site creation and lifecycle).
    """
    # Middleware order: origin_guard_middleware first, then csrf_middleware.
    # Both run before any body parsing. The origin guard rejects cross-site
    # writes to any /api/ route with 403 (no audit line); same-origin
    # requests then still pass the double-submit CSRF check on
    # /api/update/*, whose rejections keep their audit trail.
    # 32 MB body cap (aiohttp default is 1 MB): Standort-import ZIPs carry the
    # site photo (≤3 MB) plus all its inverters, and config-import bodies grow
    # with many devices. Without this, a Standort-with-image upload is rejected
    # with HTTP 413 ("Request Entity Too Large") before the handler runs.
    app = web.Application(
        middlewares=[origin_guard_middleware, csrf_middleware],
        client_max_size=32 * 1024 * 1024,
    )
    app["app_ctx"] = app_ctx
    app["config"] = config
    app["config_path"] = config_path
    app["start_time"] = time.monotonic()
    app["reconfiguring"] = False
    app["_scan_running"] = False
    app["ws_clients"] = weakref.WeakSet()

    # Phase 46 D-22/D-23: start the update-status.json poll + WS broadcaster
    # on startup and stop it on cleanup. Stores the singleton under
    # app["progress_broadcaster"].
    app.on_startup.append(start_broadcaster)
    app.on_cleanup.append(stop_broadcaster)

    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/", index_handler)
    app.router.add_get("/api/status", status_handler)
    app.router.add_get("/api/health", health_handler)
    # Phase 44: Passive Version Badge (CHECK-05)
    app.router.add_get("/api/update/available", update_available_handler)
    # Phase 45-02: EXEC-01, EXEC-02 — update trigger producer
    app.router.add_post("/api/update/start", update_start_handler)
    # Phase 46 Plan 04 (D-27, D-41): new update routes
    app.router.add_get("/api/version", version_handler)
    app.router.add_get("/api/update/status", update_status_handler)
    app.router.add_post("/api/update/rollback", update_rollback_handler)
    app.router.add_post("/api/update/check", update_check_handler)
    # Phase 46 Plan 05 (CFG-02 / D-04 / D-06): update-config GET+PATCH.
    app.router.add_get("/api/update/config", update_config_get_handler)
    app.router.add_patch("/api/update/config", update_config_patch_handler)
    app.router.add_get("/api/config", config_get_handler)
    app.router.add_post("/api/config", config_save_handler)
    app.router.add_post("/api/config/test", config_test_handler)
    app.router.add_get("/api/registers", registers_handler)
    app.router.add_get("/api/dashboard", dashboard_handler)
    app.router.add_post("/api/power-limit", power_limit_handler)
    app.router.add_post("/api/power-clamp", power_clamp_handler)
    app.router.add_post("/api/venus-write", venus_write_handler)
    app.router.add_post("/api/venus-dbus", venus_dbus_handler)
    app.router.add_post("/api/venus-lock", venus_lock_handler)
    app.router.add_get("/api/venus/health", venus_health_handler)
    app.router.add_get("/api/scanner/config", scanner_config_get_handler)
    app.router.add_put("/api/scanner/config", scanner_config_save_handler)
    app.router.add_post("/api/scanner/discover", scanner_discover_handler)
    app.router.add_post("/api/mqtt/discover", mqtt_discover_handler)
    app.router.add_post("/api/shelly/probe", shelly_probe_handler)
    app.router.add_post("/api/shelly/discover", shelly_discover_handler)
    app.router.add_post("/api/sungrow/probe", sungrow_probe_handler)
    app.router.add_post("/api/senec/probe", senec_probe_handler)
    app.router.add_post("/api/senec/discover", senec_discover_handler)
    app.router.add_post("/api/opendtu/test-auth", opendtu_test_auth_handler)
    app.router.add_post("/api/hoymiles/probe", hoymiles_probe_handler)
    app.router.add_post("/api/hoymiles/discover", hoymiles_discover_handler)
    app.router.add_get("/api/devices/{id}/opendtu/status", opendtu_status_handler)
    app.router.add_get("/api/devices/{id}/opendtu/inverters", opendtu_inverters_handler)
    app.router.add_post("/api/devices/{id}/opendtu/power", opendtu_power_handler)
    app.router.add_get("/api/devices/{id}/hoymiles/status", hoymiles_status_handler)
    app.router.add_post("/api/devices/{id}/hoymiles/power", hoymiles_power_handler)
    app.router.add_post("/api/devices/{id}/shelly/switch", shelly_switch_handler)
    app.router.add_post(
        "/api/devices/{id}/detect-phase",
        phase_detect_start_handler,
    )
    app.router.add_get(
        "/api/devices/{id}/detect-phase",
        phase_detect_status_handler,
    )
    app.router.add_get("/api/inverters", inverters_list_handler)
    app.router.add_post("/api/inverters", inverters_add_handler)
    app.router.add_put("/api/inverters/{id}", inverters_update_handler)
    app.router.add_delete("/api/inverters/{id}", inverters_delete_handler)
    # Device-centric GET endpoints (Phase 24)
    app.router.add_get("/api/devices", devices_list_handler)
    app.router.add_get("/api/devices/virtual/snapshot", virtual_snapshot_handler)
    app.router.add_get("/api/devices/{id}/snapshot", device_snapshot_handler)
    app.router.add_post("/api/devices/{id}/reboot", inverter_reboot_handler)
    app.router.add_get("/api/devices/{id}/registers", device_registers_handler)
    # CRUD aliases at /api/devices (frontend uses these, /api/inverters kept for backward compat)
    app.router.add_post("/api/devices", inverters_add_handler)
    app.router.add_put("/api/devices/{id}", inverters_update_handler)
    app.router.add_delete("/api/devices/{id}", inverters_delete_handler)
    app.router.add_get("/api/config/export", config_export_handler)
    app.router.add_post("/api/config/import", config_import_handler)
    # Per-inverter import/export (single device → downloadable file, and back)
    app.router.add_get("/api/inverters/{id}/export", inverter_export_handler)
    app.router.add_post("/api/inverters/import", inverter_import_handler)
    # Locations / Standorte
    app.router.add_get("/api/locations", locations_list_handler)
    app.router.add_post("/api/locations", locations_add_handler)
    app.router.add_post("/api/locations/check-subnet", location_subnet_check_handler)
    # Whole-Standort import/export (ZIP: location + image + assigned inverters)
    app.router.add_post("/api/locations/import", location_import_handler)
    app.router.add_get("/api/locations/{id}/export", location_export_handler)
    app.router.add_put("/api/locations/{id}", locations_update_handler)
    app.router.add_post("/api/locations/{id}/enabled", location_set_enabled_handler)
    app.router.add_delete("/api/locations/{id}", locations_delete_handler)
    app.router.add_post("/api/locations/{id}/image", location_image_upload_handler)
    app.router.add_get("/api/locations/{id}/image", location_image_get_handler)
    app.router.add_delete("/api/locations/{id}/image", location_image_delete_handler)
    app.router.add_post("/api/geocode", geocode_handler)
    app.router.add_get("/static/{filename}", static_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    return runner
