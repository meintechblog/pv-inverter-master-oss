"""YAML configuration loading with dataclass schema and sensible defaults.

Loads configuration from a YAML file (default: /etc/pv-inverter-master/config.yaml).
Missing file or missing keys silently use defaults. Unknown keys are ignored.
"""
from __future__ import annotations

import ipaddress
import os
import shutil
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field

import structlog
import yaml

log = structlog.get_logger()


DEFAULT_CONFIG_PATH = "/etc/pv-inverter-master/config.yaml"

CONVERGENCE_PARAMS: dict[str, float] = {
    "convergence_tolerance_pct": 5.0,
    "convergence_max_samples": 10,
    "target_change_tolerance_pct": 2.0,
    "binary_off_threshold_w": 50.0,
}


def _generate_id() -> str:
    """Generate a 12-character hex identifier for an inverter entry."""
    return uuid.uuid4().hex[:12]


@dataclass
class PanelSpec:
    """A bundle of identical panels on one string.

    A single string can carry several PanelSpecs when mixed panel
    types share the same MPPT input (e.g. a SolarEdge string with
    legacy + new panels).
    """
    count: int = 1
    wp_each: int = 0
    brand: str = ""        # optional, e.g. "JA Solar"
    model: str = ""        # optional, e.g. "JAM72S30-545-MR"

    @property
    def wp_total(self) -> int:
        return int(self.count or 0) * int(self.wp_each or 0)


@dataclass
class StringSpec:
    """A logical PV string. Where the inverter reports per-MPPT data
    (Hoymiles, OpenDTU), the strings list is index-aligned to the
    inverter's MPPT inputs. For inverters without per-string telemetry
    (SolarEdge, Sungrow), strings is just a logical grouping of the
    panel inventory.
    """
    name: str = ""
    panels: list = field(default_factory=list)  # list[PanelSpec]

    @property
    def wp_total(self) -> int:
        return sum(p.wp_total for p in self.panels)


@dataclass
class InverterEntry:
    """A single inverter connection entry."""
    host: str = "192.0.2.18"
    port: int = 1502
    unit_id: int = 1
    enabled: bool = True
    id: str = field(default_factory=_generate_id)
    manufacturer: str = ""
    model: str = ""
    serial: str = ""
    firmware_version: str = ""
    type: str = "solaredge"       # "solaredge" | "opendtu" | "shelly" | "sungrow" | "hoymiles"
    name: str = ""                # User-friendly display name
    info: str = ""                # Optional free-text note shown beside the name
    gateway_host: str = ""        # OpenDTU gateway IP (opendtu type only)
    gateway_user: str = ""        # OpenDTU Basic Auth username (empty = default "admin")
    gateway_password: str = ""    # OpenDTU Basic Auth password (empty = default "openDTU42")
    shelly_gen: str = ""              # Shelly generation: "gen1" or "gen2" (auto-detected, persisted)
    shelly_channel: int = 0           # Shelly switch/relay channel this entry polls
                                      # (multi-relay devices add one entry per channel)
    rated_power: int = 0           # Inverter rated power in watts (0 = unknown)
    panel_power_wp: int = 0        # DC panel peak power in Wp (0 = unknown)
    throttle_order: int = 1           # TO 1 = first to throttle
    throttle_enabled: bool = True     # False = monitoring-only (no limit commands)
    throttle_dead_time_s: float = 0.0 # Per-device dead-time in seconds (default: no wait)
    aggregate: bool = True            # Include in Fronius aggregation for Venus OS
    location_id: str = ""             # Assigned Standort (Location.id), empty = unassigned
    poll_interval: float = 0.0        # Per-device poll interval (s); 0 = use proxy/gateway default
    phase: int = 1                    # AC phase for single-phase inverters (1/2/3 = L1/L2/L3).
                                      # Ignored for three-phase devices (which report
                                      # current and voltage on all three phases directly).
    phase_explicit: bool = False      # True = user actively assigned this phase (via save,
                                      # Phase-Detective apply, or migration from a non-default
                                      # phase value). False = default L1 untouched. Drives the
                                      # UI's "L1 (Standard)" hint and the
                                      # "Erkennung starten" vs "Erkennung verifizieren" button.
    string_names: list = field(default_factory=list)  # Per-DC-string group name,
                                      # index-aligned to dc_channels. Strings sharing
                                      # a name form one named sub-unit in the Standort
                                      # view; "" = ungrouped (rolls up to the device).
                                      # NOTE: legacy field. New code should read names
                                      # from ``strings[i].name``; this field is kept
                                      # for back-compat and migrated by load_config().
    strings: list = field(default_factory=list)  # list[StringSpec] — panel inventory
                                      # per logical PV string. Populated by migration
                                      # from panel_power_wp + string_names on first
                                      # load; user maintains it via the device-config
                                      # page. Total Wp = sum across all strings.
    dynamic_power_control: bool = True  # SolarEdge only: use the Enhanced Dynamic
                                      # Power Control block (0xF300) so limits carry a
                                      # command-timeout watchdog — on comms loss the
                                      # inverter reverts to dpc_fallback_pct by itself
                                      # instead of latching the last written limit.
                                      # Falls back to the legacy static write path if
                                      # the device rejects the init probe.
    dpc_command_timeout_s: int = 120  # Watchdog window (0xF310): no dynamic command
                                      # within this many seconds -> inverter reverts to
                                      # dpc_fallback_pct. Distributor refreshes every
                                      # ~5s, so 120s leaves >20x margin while bounding
                                      # a stuck-limit loss to 2 minutes.
    dpc_fallback_pct: float = 100.0   # Fallback active power limit (0xF312) the
                                      # inverter applies after the watchdog fires.


@dataclass
class Location:
    """A physical site (Standort) one or more inverters belong to.

    Coordinates may be left blank (lat/lon = None) and fed live via MQTT
    instead — the caravan use-case. ``mqtt_coords`` records that intent so
    the UI shows the MQTT topic hint and the live feed is honoured.
    """
    id: str = field(default_factory=_generate_id)
    name: str = ""
    lat: float | None = None
    lon: float | None = None
    street: str = ""
    postal_code: str = ""
    city: str = ""
    country: str = ""
    image_filename: str = ""          # File under <config_dir>/location-images/
    subnet: str = ""                  # Network the site's devices live on; used
                                      # to target discovery scans (CIDR / "x"-
                                      # shorthand / single IP). Empty = local subnet.
    mqtt_coords: bool = False         # True = coordinates fed live via MQTT
    mqtt_fallback_days: int = 7       # Revert to static address after N days without
                                      # an MQTT position; 0 = never revert (infinite)
    enabled: bool = True              # False = Standort deaktiviert; alle zugehörigen
                                      # Inverter werden mit-deaktiviert (Polling gestoppt)


@dataclass
class VirtualInverterConfig:
    """Configuration for the aggregated virtual inverter identity."""
    name: str = "Fronius Proxy"
    location_ids: list[str] = field(default_factory=list)  # Locations counted in aggregate (empty = all)


@dataclass
class GatewayConfig:
    """Configuration for a gateway device (e.g., OpenDTU)."""
    host: str = ""
    user: str = "admin"
    password: str = "openDTU42"
    poll_interval: float = 5.0


@dataclass
class ProxyConfig:
    host: str = "0.0.0.0"
    port: int = 502
    poll_interval: float = 1.0
    staleness_timeout: float = 30.0


@dataclass
class ControlConfig:
    """Tuning knobs for Venus OS write-path hysteresis (proxy.py).

    See ``control.HYSTERESIS_PCT`` for the reasoning. Set
    ``hysteresis_pct=0`` to disable the filter entirely (all writes
    pass through, recovering pre-patch behaviour).
    """
    hysteresis_pct: float = 3.0
    min_hold_after_down_s: float = 15.0
    near_full_pct: float = 95.0


@dataclass
class NightModeConfig:
    threshold_seconds: float = 300.0


@dataclass
class WebappConfig:
    port: int = 80


@dataclass
class VenusConfig:
    host: str = ""           # Empty = not configured (proxy runs without MQTT)
    port: int = 1883         # MQTT standard port
    portal_id: str = ""      # Empty = auto-discover via N/+/system/0/Serial
    name: str = ""           # Display name for sidebar (default: "Venus OS")


@dataclass
class ScannerConfig:
    ports: list[int] = field(default_factory=lambda: [502, 1502])


@dataclass
class MqttPublishConfig:
    """Configuration for external MQTT data publishing."""
    enabled: bool = False
    host: str = "mqtt.example"
    port: int = 1883
    topic_prefix: str = "pv-inverter-master"
    interval_s: int = 5
    client_id: str = "pv-proxy-pub"
    name: str = ""           # Display name for sidebar (default: "MQTT Publishing")


@dataclass
class InfluxConfig:
    """Configuration for InfluxDB 2.x time-series logging."""
    enabled: bool = False
    url: str = "http://192.0.2.70:8086"   # InfluxDB 2.x base URL
    token: str = ""          # API token, scoped to the target bucket
    org: str = "default"
    bucket: str = "default"
    interval_s: int = 10     # Batched write interval in seconds
    name: str = ""           # Display name for sidebar (default: "InfluxDB")


@dataclass
class Config:
    inverters: list[InverterEntry] = field(default_factory=lambda: [InverterEntry()])
    locations: list[Location] = field(default_factory=list)
    gateways: dict[str, list[GatewayConfig]] = field(default_factory=dict)
    proxy: ProxyConfig = field(default_factory=ProxyConfig)
    control: ControlConfig = field(default_factory=ControlConfig)
    night_mode: NightModeConfig = field(default_factory=NightModeConfig)
    webapp: WebappConfig = field(default_factory=WebappConfig)
    venus: VenusConfig = field(default_factory=VenusConfig)
    scanner: ScannerConfig = field(default_factory=ScannerConfig)
    mqtt_publish: MqttPublishConfig = field(default_factory=MqttPublishConfig)
    influx: InfluxConfig = field(default_factory=InfluxConfig)
    virtual_inverter: VirtualInverterConfig = field(default_factory=VirtualInverterConfig)
    log_level: str = "INFO"

    @property
    def inverter(self) -> InverterEntry:
        """Backward compatibility: return first inverter entry."""
        return self.inverters[0] if self.inverters else InverterEntry()


def load_config(path: str | None = None) -> Config:
    """Load config from YAML file. Missing file or missing keys use defaults.

    Supports typed multi-inverter entries (solaredge, opendtu) and gateway configs.
    Old single-inverter ``inverter:`` format is ignored (fresh config only).
    """
    config_path = path or DEFAULT_CONFIG_PATH
    try:
        with open(config_path) as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        data = {}

    # --- Build inverters list ---
    raw_inverters = data.get("inverters", [])
    if raw_inverters:
        inverters = [
            InverterEntry(**{
                k: v for k, v in entry.items()
                if k in InverterEntry.__dataclass_fields__
            })
            for entry in raw_inverters
        ]
    else:
        inverters = [InverterEntry()]

    # Normalize strings: dicts from YAML → StringSpec/PanelSpec, then
    # migrate the legacy panel_power_wp / string_names fields if the
    # new structure is still empty.
    for entry in inverters:
        _normalize_inverter_strings(entry)

    # Migration: existing configs may have phase set to a non-default value
    # without the new ``phase_explicit`` field. Treat phase != 1 as an
    # explicit assignment (user wouldn't change it otherwise) so the UI
    # doesn't show "L1 (Standard)" for an already-customized inverter.
    # Phase == 1 stays implicit until the user saves it (or Phase-Detective
    # applies a different value).
    for entry in inverters:
        if entry.phase != 1 and not entry.phase_explicit:
            entry.phase_explicit = True

    # --- Build locations list ---
    raw_locations = data.get("locations", [])
    locations: list[Location] = []
    if isinstance(raw_locations, list):
        for entry in raw_locations:
            if isinstance(entry, dict):
                locations.append(Location(**{
                    k: v for k, v in entry.items()
                    if k in Location.__dataclass_fields__
                }))

    # --- Build gateways dict ---
    raw_gateways = data.get("gateways", {})
    gateways: dict[str, list[GatewayConfig]] = {}
    for gw_type, gw_list in raw_gateways.items():
        if isinstance(gw_list, list):
            gateways[gw_type] = [
                GatewayConfig(**{
                    k: v for k, v in gw.items()
                    if k in GatewayConfig.__dataclass_fields__
                })
                for gw in gw_list
                if isinstance(gw, dict)
            ]

    config = Config(
        inverters=inverters,
        locations=locations,
        gateways=gateways,
        proxy=ProxyConfig(**{
            k: v for k, v in data.get("proxy", {}).items()
            if k in ProxyConfig.__dataclass_fields__
        }),
        control=ControlConfig(**{
            k: v for k, v in data.get("control", {}).items()
            if k in ControlConfig.__dataclass_fields__
        }),
        night_mode=NightModeConfig(**{
            k: v for k, v in data.get("night_mode", {}).items()
            if k in NightModeConfig.__dataclass_fields__
        }),
        webapp=WebappConfig(**{
            k: v for k, v in data.get("webapp", {}).items()
            if k in WebappConfig.__dataclass_fields__
        }),
        venus=VenusConfig(**{
            k: v for k, v in data.get("venus", {}).items()
            if k in VenusConfig.__dataclass_fields__
        }),
        scanner=ScannerConfig(**{
            k: v for k, v in data.get("scanner", {}).items()
            if k in ScannerConfig.__dataclass_fields__
        }),
        mqtt_publish=MqttPublishConfig(**{
            k: v for k, v in data.get("mqtt_publish", {}).items()
            if k in MqttPublishConfig.__dataclass_fields__
        }),
        influx=InfluxConfig(**{
            k: v for k, v in data.get("influx", {}).items()
            if k in InfluxConfig.__dataclass_fields__
        }),
        virtual_inverter=VirtualInverterConfig(**{
            k: v for k, v in data.get("virtual_inverter", {}).items()
            if k in VirtualInverterConfig.__dataclass_fields__
        }),
        log_level=data.get("log_level", "INFO"),
    )

    return config


def coerce_strings_in_place(entry: InverterEntry) -> None:
    """Convert ``entry.strings`` items from raw dicts (YAML or JSON body)
    into ``StringSpec`` / ``PanelSpec`` dataclasses. Idempotent; safe to
    call on entries whose strings are already dataclasses."""
    raw = list(entry.strings or [])
    rebuilt: list[StringSpec] = []
    for s in raw:
        if isinstance(s, StringSpec):
            rebuilt.append(s)
            continue
        if not isinstance(s, dict):
            continue
        panels: list[PanelSpec] = []
        for p in s.get("panels") or []:
            if isinstance(p, PanelSpec):
                panels.append(p)
            elif isinstance(p, dict):
                panels.append(PanelSpec(**{
                    k: v for k, v in p.items()
                    if k in PanelSpec.__dataclass_fields__
                }))
        rebuilt.append(StringSpec(name=str(s.get("name", "") or ""), panels=panels))
    entry.strings = rebuilt


def _normalize_inverter_strings(entry: InverterEntry) -> None:
    """Coerce ``entry.strings`` to dataclasses, then migrate the legacy
    ``panel_power_wp`` / ``string_names`` fields if the new structure
    is still empty.

    Migration rule: one dummy panel group preserving the full legacy
    Wp value, placed in the first string (named from ``string_names[0]``
    if present). Any additional ``string_names`` are appended as
    empty-panel strings so their names are not lost. The user can then
    refine the inventory via the device-config UI.
    """
    coerce_strings_in_place(entry)

    if entry.strings:
        return  # already populated — no migration needed

    legacy_wp = int(entry.panel_power_wp or 0)
    names = [str(n) for n in (entry.string_names or [])]
    if legacy_wp <= 0 and not names:
        return  # nothing to migrate

    if names:
        first = StringSpec(
            name=names[0],
            panels=([PanelSpec(count=1, wp_each=legacy_wp)] if legacy_wp > 0 else []),
        )
        entry.strings = [first] + [StringSpec(name=n, panels=[]) for n in names[1:]]
    else:
        entry.strings = [StringSpec(
            name="",
            panels=[PanelSpec(count=1, wp_each=legacy_wp)],
        )]


def inverter_wp_total(entry: InverterEntry) -> int:
    """Sum of panel Wp across all strings; falls back to the legacy
    ``panel_power_wp`` value when the strings list is empty."""
    if entry.strings:
        return sum(s.wp_total for s in entry.strings)
    return int(entry.panel_power_wp or 0)


def get_gateway_for_inverter(config: Config, entry: InverterEntry) -> GatewayConfig | None:
    """Return the GatewayConfig matching an opendtu inverter's gateway_host.

    Returns None for non-opendtu types or if no matching gateway is found.
    """
    if entry.type != "opendtu":
        return None
    for gw in config.gateways.get("opendtu", []):
        if gw.host == entry.gateway_host:
            return gw
    return None


def get_active_inverter(config: Config) -> InverterEntry | None:
    """Return the first enabled inverter entry, or None if all disabled."""
    for entry in config.inverters:
        if entry.enabled:
            return entry
    return None


def get_location(config: Config, location_id: str) -> Location | None:
    """Return the Location with the given id, or None."""
    if not location_id:
        return None
    for loc in config.locations:
        if loc.id == location_id:
            return loc
    return None


def inverter_counts_for_aggregate(config: Config, entry: InverterEntry) -> bool:
    """True if an inverter's production feeds the virtual Fronius aggregate.

    Aggregate membership is controlled per-Standort via
    ``virtual_inverter.location_ids``: an inverter counts when its Standort
    is in scope. An empty ``location_ids`` means every location counts
    (backward-compatible default).
    """
    loc_ids = config.virtual_inverter.location_ids
    if not loc_ids:
        return True
    return entry.location_id in loc_ids


def validate_inverter_config(host: str, port: int, unit_id: int) -> str | None:
    """Validate inverter connection parameters.

    Returns None on success, error string on failure.
    """
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return f"Invalid IP address: {host}"

    if not (1 <= port <= 65535):
        return f"Port must be 1-65535, got {port}"

    if not (0 <= unit_id <= 247):
        return f"Unit ID must be 0-247, got {unit_id}"

    return None


def validate_venus_config(host: str, port: int) -> str | None:
    """Validate Venus OS MQTT connection parameters. Returns None on success."""
    if not host:
        return None  # Empty host = not configured, which is valid
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return f"Invalid IP address: {host}"
    if not (1 <= port <= 65535):
        return f"Port must be 1-65535, got {port}"
    return None


def save_config(config_path: str, config: Config) -> None:
    """Save config to YAML file atomically using temp file + os.replace.

    Before every write we copy the existing config to
    ``<config_path>.bak.<UTC-ISO>`` and prune to the last 20 backups, so a
    destructive save (wrong handler, missing field) is always one cp away
    from recovery. Backup failures are non-fatal — the write proceeds.
    """
    config_dir = os.path.dirname(os.path.abspath(config_path))
    if os.path.exists(config_path):
        try:
            ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            backup_path = f"{config_path}.bak.{ts}"
            shutil.copy2(config_path, backup_path)
            # Keep only the most recent 20 backups.
            backups = sorted(
                p for p in os.listdir(config_dir)
                if p.startswith(os.path.basename(config_path) + ".bak.")
            )
            for stale in backups[:-20]:
                try:
                    os.unlink(os.path.join(config_dir, stale))
                except OSError:
                    pass
        except OSError:
            pass

    data = asdict(config)
    fd, tmp_path = tempfile.mkstemp(dir=config_dir, suffix=".yaml")
    try:
        with os.fdopen(fd, "w") as f:
            yaml.dump(data, f, default_flow_style=False)
        os.replace(tmp_path, config_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
