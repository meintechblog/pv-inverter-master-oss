"""aiohttp REST API backend for the configuration webapp.

Serves status, health, config and register data endpoints. The register
viewer provides side-by-side SE30K source and Fronius target values. The
WebSocket endpoint at /ws pushes live inverter snapshots to browsers.

This package was split out of a single ``webapp.py`` module. The route
handlers live in ``routes_*`` submodules, shared payload builders in
``payloads``, WebSocket fan-out in ``broadcasts`` and the app assembler
in ``app``. Every name that was importable from the old module is
re-exported here so ``from pv_inverter_master.webapp import X`` keeps
working.
"""
from __future__ import annotations

from .app import create_webapp
from .broadcasts import (
    _broadcast_scan_complete,
    _broadcast_scan_error,
    _broadcast_scan_progress,
    broadcast_available_update,
    broadcast_device_list,
    broadcast_device_snapshot,
    broadcast_location_list,
    broadcast_location_position,
    broadcast_to_clients,
    broadcast_virtual_snapshot,
)
from .payloads import (
    _build_device_list,
    _build_location_list,
    _build_virtual_contributions,
    _string_group_rows,
)
from .reconfigure import _reconfigure_active, _reload_location_mqtt
from .routes_config import (
    config_export_handler,
    config_get_handler,
    config_import_handler,
    config_save_handler,
    config_test_handler,
)
from .routes_control import (
    power_clamp_handler,
    power_limit_handler,
    venus_dbus_handler,
    venus_lock_handler,
    venus_write_handler,
)
from .routes_core import (
    _derive_health_payload,
    _HEALTH_STARTUP_GRACE_S,
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
    location_image_delete_handler,
    location_image_get_handler,
    location_image_upload_handler,
    location_subnet_check_handler,
    locations_add_handler,
    locations_delete_handler,
    locations_list_handler,
    locations_update_handler,
    reverse_geocode,
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
from .routes_registers import (
    REGISTER_MODELS,
    device_registers_handler,
    registers_handler,
)
from .routes_updater import (
    broadcast_update_in_progress,
    update_available_handler,
    update_check_handler,
    update_config_get_handler,
    update_config_patch_handler,
    update_rollback_handler,
    update_start_handler,
    update_status_handler,
    version_handler,
)
from .routes_venus import venus_health_handler

__all__ = [
    "REGISTER_MODELS",
    "_HEALTH_STARTUP_GRACE_S",
    "_broadcast_scan_complete",
    "_broadcast_scan_error",
    "_broadcast_scan_progress",
    "_build_device_list",
    "_build_location_list",
    "_build_virtual_contributions",
    "_derive_health_payload",
    "_reconfigure_active",
    "_reload_location_mqtt",
    "_string_group_rows",
    "broadcast_available_update",
    "broadcast_device_list",
    "broadcast_device_snapshot",
    "broadcast_location_list",
    "broadcast_location_position",
    "broadcast_to_clients",
    "broadcast_update_in_progress",
    "broadcast_virtual_snapshot",
    "config_export_handler",
    "config_get_handler",
    "config_import_handler",
    "config_save_handler",
    "config_test_handler",
    "create_webapp",
    "dashboard_handler",
    "device_registers_handler",
    "device_snapshot_handler",
    "devices_list_handler",
    "geocode_handler",
    "health_handler",
    "hoymiles_discover_handler",
    "hoymiles_power_handler",
    "hoymiles_probe_handler",
    "hoymiles_status_handler",
    "index_handler",
    "inverters_add_handler",
    "inverters_delete_handler",
    "inverters_list_handler",
    "inverters_update_handler",
    "location_image_delete_handler",
    "location_image_get_handler",
    "location_image_upload_handler",
    "location_subnet_check_handler",
    "locations_add_handler",
    "locations_delete_handler",
    "locations_list_handler",
    "locations_update_handler",
    "mqtt_discover_handler",
    "opendtu_inverters_handler",
    "opendtu_power_handler",
    "opendtu_status_handler",
    "opendtu_test_auth_handler",
    "power_clamp_handler",
    "power_limit_handler",
    "registers_handler",
    "reverse_geocode",
    "scanner_config_get_handler",
    "scanner_config_save_handler",
    "scanner_discover_handler",
    "senec_discover_handler",
    "senec_probe_handler",
    "shelly_discover_handler",
    "shelly_probe_handler",
    "shelly_switch_handler",
    "static_handler",
    "status_handler",
    "sungrow_probe_handler",
    "update_available_handler",
    "update_check_handler",
    "update_config_get_handler",
    "update_config_patch_handler",
    "update_rollback_handler",
    "update_start_handler",
    "update_status_handler",
    "venus_dbus_handler",
    "venus_health_handler",
    "venus_lock_handler",
    "venus_write_handler",
    "version_handler",
    "virtual_snapshot_handler",
    "ws_handler",
]
