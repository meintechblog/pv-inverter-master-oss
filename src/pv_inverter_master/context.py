"""Typed application context replacing the flat shared_ctx dict.

AppContext holds all runtime state for the proxy. DeviceState holds
per-device state (one entry per inverter). DeviceRegistry manages
device lifecycle (Phase 22+).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field


@dataclass
class DeviceState:
    """Per-device runtime state."""
    collector: object = None       # DashboardCollector (avoid circular import)
    poll_counter: dict = field(default_factory=lambda: {"success": 0, "total": 0})
    conn_mgr: object = None        # ConnectionManager
    last_poll_data: dict | None = None  # raw poll registers for register viewer
    plugin: object = None          # InverterPlugin instance
    # asyncio.Lock guarding the in-flight Modbus poll transaction — held by
    # _device_poll_loop during connect+poll; stop_device acquires it to drain
    # before cancel (read-drain-then-close, 260611-hnk). Typed object to match
    # the file's avoid-circular-import style; default None keeps legacy direct
    # DeviceState(...) constructions (tests) valid.
    poll_lock: object = None

    # Per-device poll health surface (260528-hhj):
    # Quick task — surface "when did we last poll", "when did we last succeed",
    # and the truncated last error so the UI can show a hover tooltip and a
    # status card without tailing structlog.
    last_poll_at: float | None = None              # time.time() of the last poll attempt
    last_successful_poll_at: float | None = None   # time.time() of the last successful poll
    # Monotonic twin of last_successful_poll_at, written in the same success
    # branch. `last_successful_poll_at` is contractually WALL-CLOCK — the UI
    # renders it via formatRelativeTime and the anomaly detector subtracts it
    # from an epoch — so it cannot be switched to monotonic. Age comparisons
    # that must survive an NTP step (the aggregation freshness gate) read this
    # field instead: a backward wall-clock jump would otherwise make every
    # device, including dead ones, test as "fresh" for the duration of the jump.
    last_successful_poll_monotonic: float | None = None
    # Monotonic seconds between the previous successful poll and the most
    # recent one. Telemetry only — nothing in the control path reads it. It
    # exists because the staleness floors (aggregation 120s, detector 300s)
    # were both chosen without a measured distribution of how far apart real
    # successes actually land. None until a device has succeeded twice.
    last_poll_gap_s: float | None = None
    # Effective poll interval (s) as resolved by DeviceRegistry.start_device
    # (per-device override > gateway > proxy default, then floored by the
    # plugin's min_poll_interval). Mirrored here so consumers that lack the
    # gateway/plugin context — notably the aggregation freshness gate — can
    # derive a per-device staleness bound without re-implementing that
    # precedence. 0.0 = unknown/not started; callers must fall back.
    poll_interval: float = 0.0
    last_poll_error: str | None = None             # truncated (<=200 chars) last error
    # Source-stale: last SUCCESSFUL poll returned no fresh data because the
    # upstream source is unreachable / its frame is too old (OpenDTU radio gap).
    # Surfaced in /api/devices so the anomaly detector can tell a transient
    # radio gap apart from a genuinely-producing-0W (or dead) inverter. Only
    # the success branch writes it; a hard failure leaves it (that path is
    # covered by last_poll_error -> STALE_POLL). Default False = fail-safe.
    source_stale: bool = False


@dataclass
class AppContext:
    """Typed application context replacing flat shared_ctx dict."""

    # Core infrastructure
    cache: object = None           # RegisterCache
    control_state: object = None   # ControlState
    config: object = None          # Config
    config_path: str = ""

    # Device states (keyed by InverterEntry.id)
    devices: dict[str, DeviceState] = field(default_factory=dict)

    # Venus OS
    venus_mqtt_connected: bool = False
    venus_os_detected: bool = False
    venus_os_detected_ts: float = 0.0
    venus_os_client_ip: str = ""
    venus_task: object = None      # asyncio.Task
    venus_settings: dict | None = None
    venus_connected_since_ts: float | None = None  # GON: time.time() of last fresh MQTT connect

    # Webapp
    webapp: object = None          # aiohttp web.Application

    # Locations: live MQTT-fed coordinates keyed by Location.id.
    # {location_id: {"lat": float, "lon": float, "ts": float}}
    location_positions: dict = field(default_factory=dict)
    location_mqtt_task: object = None  # asyncio.Task for the coord subscriber

    # Auto-Transfer-Switch (ATS) state, fed by the Loxone MQTT publish
    # subscribed via ats_mqtt_loop. None until the first message arrives.
    # 1 = Netzparallel (grid), 2 = ESS/Inselbetrieb (AC.Out), -1 = error.
    # Phase-Detective records the active value at detection time so a
    # later verification can flag a topology change.
    ats_state: int | None = None
    ats_state_ts: float = 0.0
    ats_mqtt_task: object = None  # asyncio.Task for the ATS subscriber

    # Internal
    polling_paused: bool = False
    healthy_flag_written: bool = False  # True after first successful poll (SAFETY-06)
    _last_modbus_client_ip: str = ""
    override_log: object = None    # OverrideLog
    shutdown_event: asyncio.Event = field(default_factory=asyncio.Event)

    # DeviceRegistry reference (set in __main__.py after creation)
    device_registry: object = None

    distributor: object = None

    # Device IDs currently reserved for an interactive operation (e.g. the
    # Phasen-Detective probe) — the throttle distributor MUST skip these so
    # its 5s WMaxLimPct=100 keepalive does not overwrite a manual setpoint.
    # Use ``app_ctx.is_device_reserved(device_id)`` instead of inspecting
    # this set directly.
    reserved_device_ids: set = field(default_factory=set)

    def reserve_device(self, device_id: str) -> bool:
        """Reserve a device id for an exclusive operation. Returns False if
        the id is already reserved by another caller."""
        if device_id in self.reserved_device_ids:
            return False
        self.reserved_device_ids.add(device_id)
        return True

    def release_device(self, device_id: str) -> None:
        """Release a previously reserved device id. Idempotent."""
        self.reserved_device_ids.discard(device_id)

    def is_device_reserved(self, device_id: str) -> bool:
        return device_id in self.reserved_device_ids

    # MQTT Publisher (Phase 25)
    mqtt_pub_task: object = None           # asyncio.Task
    mqtt_pub_connected: bool = False
    mqtt_pub_queue: object = None          # asyncio.Queue

    # MQTT Publisher stats
    mqtt_pub_messages: int = 0             # Total messages published
    mqtt_pub_bytes: int = 0                # Total bytes published
    mqtt_pub_skipped: int = 0              # Messages skipped (change detection)
    mqtt_pub_last_ts: float = 0.0          # Timestamp of last publish

    # InfluxDB time-series logger
    influx_task: object = None             # asyncio.Task
    influx_connected: bool = False
    influx_queue: object = None            # asyncio.Queue

    # InfluxDB logger stats
    influx_points: int = 0                 # Total line-protocol points written
    influx_bytes: int = 0                  # Total bytes written
    influx_writes: int = 0                 # Total successful write requests
    influx_errors: int = 0                 # Total failed write requests
    influx_last_ts: float = 0.0            # Timestamp of last successful write
    influx_last_error: str = ""            # Last error message (truncated)

    # Phase 44: Passive Version Badge (CHECK-01, CHECK-05, CHECK-06)
    current_version: str | None = None         # From importlib.metadata at startup
    current_commit: str | None = None          # From git rev-parse --short HEAD at startup
    available_update: dict | None = None       # Parsed GitHub release info when latest > current
    update_last_check_at: float | None = None  # UNIX timestamp of last successful scheduler iteration
    update_last_check_failed_at: float | None = None  # UNIX timestamp of last failed iteration

    # Phase 45 Plan 05: Maintenance mode (RESTART-01/02/03)
    distribution_error: bool = False            # last Venus OS limit distribution failed (proxy flush)
    maintenance_mode: bool = False              # True while an update is draining the Modbus server
    maintenance_entered_at: float | None = None  # time.time() when the flag was raised
    _pending_restore_limit_pct: float | None = None  # SAFETY-09 restore pointer set at boot
    _slave_ctx: object = None                   # StalenessAwareSlaveContext — exposed for drain_inflight
