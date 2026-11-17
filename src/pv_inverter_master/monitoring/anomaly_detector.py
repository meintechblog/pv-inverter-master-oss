"""Hourly PV anomaly detector (deterministic core).

Runs as a systemd oneshot (``pv-inverter-anomaly.service``) on an hourly
timer. One pass: fetch local proxy state via the webapp HTTP API, gate on
solar elevation (so we never false-alarm at night), classify three anomaly
classes, auto-fix the one known auto-fixable class (Sungrow Modbus hang),
and dedupe alerts through a cooldown/state file.

Critical design rules (mirrors recovery.py):
1. NEVER depend on a loadable config.yaml. This is a oneshot that must run
   even when the main service is down — use a bare structlog setup writing
   JSON to stdout (systemd captures it into journald).
2. main() returns 0 on a clean run regardless of how many anomalies were
   found; an unreachable API logs an error and still exits 0. Non-zero is
   reserved for the detector's own unexpected crash.
3. Every I/O and clock seam is injectable (fetch / post / sleep / now) so the
   entire decision path is unit-testable without real time, HTTP, or root.
4. Escalation webhooks and push notifications are gated behind feature flags
   and sent to a configurable webhook base URL.

Anomaly classes:
  DEAD_INVERTER   — a PV inverter reading ~0W while the sun is up and a
                    sibling at the same location is producing. Lower-severity
                    WEATHER_OR_SITE when ALL site PV inverters read ~0W.
  STALE_POLL      — connection lost / stale last-successful-poll / poll error.
  MODBUS_HANG     — Modbus-TCP inverter (sungrow/solaredge) with a Modbus/
                    no-response/IO error signature. Auto-fixed via a reboot
                    POST (capped per device per window) where the device
                    supports_reboot; otherwise classify + escalate only.
"""
from __future__ import annotations

import json
import math
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import structlog

from pv_inverter_master import instance_lock


# ---------------------------------------------------------------------------
# Domain constants
# ---------------------------------------------------------------------------

#: Device types that are real inverters we act on. Pseudo rows
#: ("venus"/"virtual"/"mqtt_pub"/"influx") are filtered out before any rule.
KNOWN_DEVICE_TYPES: frozenset[str] = frozenset(
    {"solaredge", "opendtu", "shelly", "sungrow", "hoymiles", "senec"}
)

#: PV inverter types eligible for the DEAD_INVERTER rule. senec is a battery
#: system, not a PV string source, so it is excluded from the DEAD rule.
PV_TYPES: frozenset[str] = frozenset({"sungrow", "solaredge", "hoymiles", "opendtu"})

#: Micro-inverter PV types. A single micro legitimately sits at 0W (shading /
#: orientation) while siblings produce, so the DEAD rule for these is gated on
#: "was producing earlier today then dropped" (seen_producing AND
#: zero_runs >= cfg.micro_dead_min_zero_runs). The remaining PV_TYPES
#: (sungrow / solaredge) are "large/string" inverters and keep the immediate
#: DEAD behavior.
MICRO_PV_TYPES: frozenset[str] = frozenset({"opendtu", "hoymiles"})

#: Device ids excluded from anomaly evaluation by default. aabbccdd0001 and
#: aabbccdd0002 are the two remote-site OpenDTU micros on the SAME physical rig
#: (203.0.113.186) — both known-ok, they legitimately read 0W (low-yield /
#: afternoon-shaded) so they must not trip the DEAD rule. This is the greppable
#: default mirrored into DetectorConfig.exclude_ids; override at runtime via
#: PV_ANOMALY_EXCLUDE_IDS (comma-separated, REPLACES this default).
EXCLUDE_IDS: frozenset[str] = frozenset({"aabbccdd0001", "aabbccdd0002"})

#: Case-insensitive substrings that mark a Modbus hang. "connection refused"
#: and "timed out" added 2026-06-11: a powered-off /
#: hard-dead Modbus-TCP inverter raises plain socket errors that matched
#: nothing here, leaving a multi-hour daylight outage stuck at STALE_POLL
#: warning forever (DEAD no longer fires without fresh data since the
#: freshness gate — this keeps the critical escalation path).
#: "not connected" added 2026-06-11 evening (SE30K wedge #2): pymodbus wraps
#: a refused TCP connect into ConnectionException("Not connected"), so the
#: HARDEST failure mode (Modbus server gone, port refusing) matched no
#: signature — the live device's MODBUS_HANG flipped to resolved and the
#: outage downgraded itself to STALE_POLL/warning when it got WORSE.
MODBUS_HANG_SIGNATURES: tuple[str, ...] = (
    "modbus",
    "no response",
    "input/output",
    "connection refused",
    "timed out",
    "not connected",
)

#: Modbus-TCP inverter types eligible for the MODBUS_HANG rule. Extended from
#: sungrow-only on 2026-06-10 (SE30K incident): a SolarEdge SunSpec/Modbus
#: server hang carries the exact same error signature, but was classified as
#: STALE_POLL/warning — understating a multi-hour daylight outage. Auto-fix
#: (reboot POST) stays gated per device on ``supports_reboot``; the solaredge
#: plugin has no reboot(), so for it MODBUS_HANG classifies + escalates only.
MODBUS_TCP_TYPES: frozenset[str] = frozenset({"sungrow", "solaredge"})

# Anomaly type labels (kept as module constants so tests + the follow-up can
# import them rather than matching magic strings).
DEAD_INVERTER = "DEAD_INVERTER"
WEATHER_OR_SITE = "WEATHER_OR_SITE"
STALE_POLL = "STALE_POLL"
MODBUS_HANG = "MODBUS_HANG"
#: A Modbus-TCP device that is hang-classified but ACCEPTS fresh TCP connects
#: while a FOREIGN client holds established sessions to it (Influx
#: ``modbus_conntrack`` evidence). On session-limited inverters (SolarEdge:
#: effectively single-session) this is not a firmware wedge — the remedy is
#: killing the foreign client, NOT a power-cycle (incident 2026-07-16: two
#: zombie legacy instances held 22+27 leaked connections for weeks while the
#: outage was read as a wedge). Only ever produced by RETYPING a corroborated
#: MODBUS_HANG when both extra evidence probes agree, so it inherits the
#: hang rule's single-sample guard.
MULTI_CLIENT = "MULTI_CLIENT"
#: A micro-inverter that would read DEAD_INVERTER but is reporting
#: ``source_stale`` (OpenDTU DTU<->micro radio gap: success poll, no fresh
#: data). A transient radio gap is NOT a dead inverter, so it rides a softer
#: WARNING channel instead of the DEAD CRITICAL — UNTIL the gap persists long
#: enough in daylight that it can no longer be a transient gap (escalates back
#: to DEAD; see classify_anomalies). Crucially this only ever DOWNGRADES the
#: alert for a positively-identified radio gap; a gap that does not recover is
#: re-promoted to DEAD, so a real (radio-silent) death is never permanently
#: masked.
RADIO_GAP = "RADIO_GAP"

#: Per-run cap on the daylight-staleness delta added to a gap's persistence
#: clock. Bounds over-counting if the detector missed runs (e.g. was down):
#: a single huge gap between evaluations cannot vault a young gap over the
#: escalation threshold. ~2h = 2x the hourly cadence.
RADIO_GAP_MAX_DELTA_S: float = 2 * 3600.0

#: Anomaly types that drive alerting (push notification + webhook escalation + recovery
#: notifications). WEATHER_OR_SITE is intentionally EXCLUDED: it is an
#: informational classification ("all site PV at ~0W in daylight = likely
#: weather, not a fault"), so it is detected, logged and counted but must never
#: reach cooldown / alerts / escalation / recovery.
#: RADIO_GAP is intentionally NOT alertable: a positively-identified transient
#: OpenDTU radio gap is informational (logged + counted like WEATHER_OR_SITE),
#: NOT a push notification — that is the whole point (kill the alarm fatigue). The
#: safety net against a real radio-silent death is the time-ESCALATION
#: RADIO_GAP->DEAD_INVERTER (classify_anomalies), which IS alertable. So a
#: transient gap is silent; a gap that won't recover pages as DEAD.
ALERTABLE_TYPES: frozenset[str] = frozenset(
    {DEAD_INVERTER, STALE_POLL, MODBUS_HANG, MULTI_CLIENT}
)


def _configure_logging() -> None:
    """Minimal standalone structlog setup (mirrors recovery._configure_logging).

    Does NOT import logging_config — that expects a loadable config.yaml,
    which may not exist when this oneshot runs. Bare JSON to stdout; systemd
    captures it into journald via StandardOutput=journal.
    """
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(),
    )


log = structlog.get_logger(component="anomaly")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class DetectorConfig:
    """Tunables for one detector pass. Defaults are production values.

    ``base_url``, ``webhook_base_url`` and ``state_path`` may be overridden
    via ``PV_ANOMALY_BASE_URL``, ``PV_ANOMALY_WEBHOOK_BASE_URL`` and
    ``PV_ANOMALY_STATE_PATH``; feature flags and detection overrides are
    applied by ``from_env``.
    """

    base_url: str = "http://localhost"
    state_path: Path = Path("/var/lib/pv-inverter-master/anomaly-state.json")
    cooldown_s: float = 6 * 3600
    http_timeout_s: float = 5.0
    reboot_backoff_s: float = 25.0
    autofix_max_per_window: int = 2
    autofix_window_s: float = 6 * 3600
    webhook_base_url: str = "http://192.0.2.127:7890"
    # SAFE-BY-DEFAULT: escalation + push notifications are OFF so dev/test/manual
    # runs can never fire a real alert at the configured webhook (incident 2026-05-29: a
    # dev smoke-run hit production). PRODUCTION enables them via the systemd
    # unit's Environment= (PV_ANOMALY_ESCALATION / PV_ANOMALY_PUSH). The env
    # vars are truthy/falsy overrides (unset -> keep these False defaults).
    escalation_enabled: bool = False
    push_enabled: bool = False
    daylight_min_elev_deg: float = 3.0
    ac_zero_w: float = 5.0
    sibling_producing_w: float = 50.0
    #: Micro-inverter DEAD gate: a micro must have read ~0W for at least this
    #: many consecutive detector runs (AFTER having produced earlier today)
    #: before it is flagged DEAD_INVERTER. Large inverters are unaffected.
    micro_dead_min_zero_runs: int = 2
    #: In-run DEAD re-sample confirmation: before a DEAD_INVERTER anomaly is
    #: allowed to alert, run_detection re-reads /api/devices this many times
    #: with ``dead_confirm_gap_s`` seconds between samples; if ANY re-sample
    #: shows that device producing (> ac_zero_w), the DEAD anomaly is dropped
    #: (transient 0W dip, not a fault). resamples=0 disables confirmation
    #: (legacy single-sample behavior).
    dead_confirm_resamples: int = 2
    dead_confirm_gap_s: float = 3.0
    #: Radio-gap escalation threshold (seconds of accumulated DAYLIGHT
    #: staleness). An OpenDTU micro reporting source_stale while the sun is up
    #: and a sibling produces is treated as a transient RADIO_GAP (warning)
    #: until its daylight-staleness clock exceeds this — then it is re-promoted
    #: to DEAD_INVERTER (critical), because a gap that won't recover after this
    #: much daylight is a real fault (dead micro or broken radio), not a
    #: transient link drop. Default 3 h: comfortably above the longest observed
    #: *recovered* gap (~91 min, n=3) — conservative because n is too thin to
    #: fit a real distribution; every gap duration is logged so this can be
    #: tuned data-driven later. Override via PV_ANOMALY_RADIO_GAP_ESCALATE_S.
    radio_gap_escalate_s: float = 3 * 3600.0
    #: Device ids skipped entirely from anomaly evaluation (no STALE, no DEAD,
    #: no production tracking). Defaults to EXCLUDE_IDS; PV_ANOMALY_EXCLUDE_IDS
    #: REPLACES this default when set.
    exclude_ids: frozenset[str] = field(default_factory=lambda: EXCLUDE_IDS)

    @classmethod
    def from_env(cls) -> "DetectorConfig":
        """Build a config, applying the supported env overrides.

        ``PV_ANOMALY_PUSH`` / ``PV_ANOMALY_ESCALATION`` are truthy/falsy
        overrides over the safe-by-default OFF state: any recognised truthy
        value turns the corresponding feature ON (this is how PRODUCTION enables
        them, via the systemd unit's Environment=); any recognised falsy value
        keeps it OFF. Unset -> keep the default (False). So a bare dev/manual run
        with no env set never alerts the configured webhook.
        """
        cfg = cls()
        base_url = os.environ.get("PV_ANOMALY_BASE_URL")
        if base_url:
            cfg.base_url = base_url
        webhook_base_url = os.environ.get("PV_ANOMALY_WEBHOOK_BASE_URL")
        if webhook_base_url:
            cfg.webhook_base_url = webhook_base_url
        state_path = os.environ.get("PV_ANOMALY_STATE_PATH")
        if state_path:
            cfg.state_path = Path(state_path)
        push = _env_bool(os.environ.get("PV_ANOMALY_PUSH"))
        if push is not None:
            cfg.push_enabled = push
        esc = _env_bool(os.environ.get("PV_ANOMALY_ESCALATION"))
        if esc is not None:
            cfg.escalation_enabled = esc
        # Re-sample confirmation tunables — tolerant parse: only override the
        # default when the value is present AND parses, keep the default on a
        # malformed/empty value (never crash from_env).
        raw_resamples = os.environ.get("PV_ANOMALY_DEAD_CONFIRM_RESAMPLES")
        if raw_resamples is not None:
            try:
                cfg.dead_confirm_resamples = int(raw_resamples)
            except (ValueError, TypeError):
                pass
        raw_gap = os.environ.get("PV_ANOMALY_DEAD_CONFIRM_GAP_S")
        if raw_gap is not None:
            try:
                cfg.dead_confirm_gap_s = float(raw_gap)
            except (ValueError, TypeError):
                pass
        raw_escalate = os.environ.get("PV_ANOMALY_RADIO_GAP_ESCALATE_S")
        if raw_escalate is not None:
            try:
                cfg.radio_gap_escalate_s = float(raw_escalate)
            except (ValueError, TypeError):
                pass
        # PV_ANOMALY_EXCLUDE_IDS (comma-separated) REPLACES the default
        # exclude set when present, so the operator can override which devices are
        # skipped. An explicitly empty value clears the default (excludes none).
        raw_exclude = os.environ.get("PV_ANOMALY_EXCLUDE_IDS")
        if raw_exclude is not None:
            cfg.exclude_ids = frozenset(
                part.strip() for part in raw_exclude.split(",") if part.strip()
            )
        return cfg


_TRUTHY = frozenset({"1", "true", "yes", "on", "y", "t"})
_FALSY = frozenset({"0", "false", "no", "off", "n", "f", ""})


def _env_bool(value: str | None) -> bool | None:
    """Parse an env var to bool. Returns None when unset/unrecognised."""
    if value is None:
        return None
    norm = value.strip().lower()
    if norm in _TRUTHY:
        return True
    if norm in _FALSY:
        return False
    return None


# ---------------------------------------------------------------------------
# HTTP seam (injectable for tests)
# ---------------------------------------------------------------------------


def fetch_json(url: str, timeout: float) -> dict[str, Any] | None:
    """GET ``url`` and parse JSON. Returns None on any failure.

    Never raises — a dead/unreachable API must degrade to "no data" so the
    oneshot still exits 0. Catches URLError, timeout, and JSON decode errors.
    """
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log.error("fetch_failed", url=url, error=str(e))
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as e:
        log.error("fetch_bad_json", url=url, error=str(e))
        return None
    if not isinstance(data, dict):
        log.error("fetch_not_object", url=url)
        return None
    return data


def post_reboot(base_url: str, device_id: str, timeout: float) -> bool:
    """POST the reboot endpoint for ``device_id``. Returns success boolean.

    Never raises — a failed reboot POST is just a False outcome the caller
    records in state. HTTP 200 with {"success": true} == success.
    """
    url = f"{base_url}/api/devices/{device_id}/reboot"
    try:
        req = urllib.request.Request(url, data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        log.error("reboot_http_error", device_id=device_id, status=e.code)
        return False
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log.error("reboot_failed", device_id=device_id, error=str(e))
        return False
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        # 200 with an unparseable body — treat as success (HTTP layer ok).
        return True
    return bool(isinstance(data, dict) and data.get("success"))


def _post_json(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any] | None:
    """POST ``payload`` as JSON to ``url``; parse and return the JSON body.

    Never raises (mirrors post_reboot): any HTTP/network/decode failure logs
    and returns None so escalation seams degrade to "best effort" — the oneshot
    must still exit 0 even when the webhook is unreachable.

    On HTTPError we still try to read+parse the body so callers can react to a
    structured response carried on a non-2xx status (e.g. 202 pending).
    """
    try:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        # Try to surface a structured body carried on a non-2xx response.
        try:
            raw = e.read()
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError, ValueError):
            log.error("post_json_http_error", url=url, status=e.code)
            return None
        if isinstance(data, dict):
            return data
        log.error("post_json_http_error", url=url, status=e.code)
        return None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log.error("post_json_failed", url=url, error=str(e))
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as e:
        log.error("post_json_bad_json", url=url, error=str(e))
        return None
    if not isinstance(data, dict):
        log.error("post_json_not_object", url=url)
        return None
    return data


# ---------------------------------------------------------------------------
# MULTI_CLIENT evidence probes (default implementations for the seams)
# ---------------------------------------------------------------------------


#: Bare IP/hostname characters only — the host value is interpolated into a
#: Flux string literal, so anything that could break out (quotes, spaces,
#: backslashes) is rejected before any probe runs.
_HOST_RE = re.compile(r"[A-Za-z0-9._\-]+")


def _own_source_ip(host: str) -> str | None:
    """Our source address towards ``host`` WITHOUT sending a packet.

    A UDP ``connect()`` only asks the kernel for the route; nothing is sent.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((host, 9))
            return s.getsockname()[0]
    except OSError:
        return None


def _tcp_accept_probe(host: str, port: int, timeout: float) -> tuple[bool, str | None]:
    """One bare TCP connect+close against ``host:port`` - only when no proxy runs.

    Returns ``(accepted, own_ip)`` - ``own_ip`` is our source address as seen
    by the device, used to exclude ourselves from the foreign-client check.
    Sends NO Modbus bytes (quiesce discipline: one connect per hourly run).

    The inverter tolerates ONE session. While the proxy service holds the
    instance lock it owns that session, so this detector (a separate process)
    must not open another one: the connect is skipped and the evidence comes
    from the conntrack query alone (``accepted`` is reported as True so the
    query still runs; our own address is taken from the routing table). The
    connect happens only while this process holds the instance lock itself.
    Never raises.
    """
    try:
        with instance_lock.try_exclusive_lock() as free:
            if not free:
                if instance_lock.running_instance_pid() is None:
                    return False, None   # lock unusable: no mutual exclusion, no connect
                log.info("anomaly_tcp_probe_skipped", host=host,
                         reason="proxy holds the instance lock")
                return True, _own_source_ip(host)
            try:
                with socket.create_connection((host, port), timeout=timeout) as s:
                    return True, s.getsockname()[0]
            except OSError:
                return False, None
    except Exception:
        return False, None


def _load_influx_settings() -> dict[str, str] | None:
    """Best-effort Influx settings for the conntrack query.

    Env overrides win (``PV_ANOMALY_INFLUX_URL`` / ``_TOKEN`` / ``_ORG`` /
    ``_BUCKET``); otherwise fall back to the proxy's own config.yaml influx
    section. Returns None when no usable settings exist (fail-open: the
    MULTI_CLIENT retype then simply never fires).
    """
    url = os.environ.get("PV_ANOMALY_INFLUX_URL")
    token = os.environ.get("PV_ANOMALY_INFLUX_TOKEN")
    org = os.environ.get("PV_ANOMALY_INFLUX_ORG")
    bucket = os.environ.get("PV_ANOMALY_INFLUX_BUCKET")
    if not (url and token and org):
        try:
            import yaml  # deferred: only needed on this path

            with open("/etc/pv-inverter-master/config.yaml", encoding="utf-8") as fh:
                influx = (yaml.safe_load(fh) or {}).get("influx") or {}
            url = url or influx.get("url")
            token = token or influx.get("token")
            org = org or influx.get("org")
            bucket = bucket or influx.get("bucket")
        except Exception:
            return None
    if not (url and token and org):
        return None
    return {"url": url, "token": token, "org": org, "bucket": bucket or "default"}


def _conntrack_foreign_sources(
    host: str, own_ips: set[str], timeout: float
) -> list[str] | None:
    """Foreign src_ips with established conntrack sessions to ``host``.

    Queries the fleet's ``modbus_conntrack`` measurement (written by the
    external network watcher) for established>0 rows in the last 15 minutes.
    Returns None when no data/settings/HTTP failure (fail-open — absence of
    watcher coverage must never block the plain MODBUS_HANG path); returns a
    possibly-empty list when the query succeeded. NOTE: an empty list is NOT
    proof of "no foreign client" — the watcher coverage is incomplete, so the
    caller treats [] and None identically (check-clients-first hint). Never
    raises.
    """
    settings = _load_influx_settings()
    if settings is None:
        return None
    flux = (
        f'from(bucket:"{settings["bucket"]}") |> range(start:-15m) '
        f'|> filter(fn:(r)=> r._measurement=="modbus_conntrack" '
        f'and r._field=="established" and r.host=="{host}") '
        f"|> last() "
        f'|> keep(columns:["_value","src_ip"])'
    )
    try:
        req = urllib.request.Request(
            f'{settings["url"].rstrip("/")}/api/v2/query?org={settings["org"]}',
            data=flux.encode("utf-8"),
            headers={
                "Authorization": f'Token {settings["token"]}',
                "Content-Type": "application/vnd.flux",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log.info("conntrack_query_failed", host=host, error=str(e))
        return None
    sources: set[str] = set()
    header: list[str] | None = None
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            header = None
            continue
        cells = line.split(",")
        if header is None:
            header = cells
            continue
        row = dict(zip(header, cells))
        src = (row.get("src_ip") or "").strip()
        try:
            established = float(row.get("_value") or 0)
        except ValueError:
            established = 0.0
        if src and established > 0 and src not in own_ips:
            sources.add(src)
    return sorted(sources)


def _enrich_multi_client(
    anomalies: list[dict[str, Any]],
    devices_payload: Any,
    cfg: "DetectorConfig",
    *,
    tcp_probe: Callable[[str, int, float], tuple[bool, str | None]],
    conntrack: Callable[[str, set[str], float], list[str] | None],
) -> list[dict[str, Any]]:
    """Retype MODBUS_HANG anomalies to MULTI_CLIENT where evidence supports it.

    Per corroborated MODBUS_HANG device: one TCP accept probe. Accepting TCP
    while Modbus is silent means either a foreign client holds the session
    slot(s) or a genuine firmware wedge. The conntrack measurement decides:

    - foreign established sources found -> retype to MULTI_CLIENT/critical
      naming the sources (remedy: kill the client, NOT a power-cycle);
    - anything else (no data OR empty result) -> keep MODBUS_HANG but prepend
      the check-clients-first runbook hint. An EMPTY query result is NOT
      positive wedge evidence: the watcher coverage is provably incomplete
      (a field case had 27 connections invisible to it), so absence of rows is
      indistinguishable from absence of coverage. Claiming "no foreign client"
      here would stamp the exact misdiagnosis this pass exists to prevent.

    Pure over its seams — classify stays untouched, all IO happens here.
    """
    by_id: dict[str, dict[str, Any]] = {}
    rows = devices_payload if isinstance(devices_payload, list) else (
        (devices_payload or {}).get("devices") or []
    )
    for d in rows:
        by_id[str(d.get("id"))] = d
    for anomaly in anomalies:
        if anomaly.get("type") != MODBUS_HANG:
            continue
        device = by_id.get(str(anomaly.get("device_id")))
        if device is None:
            continue
        host = str(device.get("host") or "")
        port = int(_as_float(device.get("port")) or 502)
        # Host allowlist pattern: bare IP/hostname chars only. Blocks Flux
        # string-literal breakout via a hostile host value (the host is
        # interpolated into the conntrack query) and garbage before the
        # socket probe. Operator-config values are trusted-ish, this is
        # defense-in-depth.
        if not host or not _HOST_RE.fullmatch(host):
            continue
        accepted, own_ip = tcp_probe(host, port, cfg.http_timeout_s)
        if not accepted:
            continue  # port dead/refused: plain hang evidence, leave as-is
        own_ips = {own_ip} if own_ip else set()
        foreign = conntrack(host, own_ips, cfg.http_timeout_s)
        if foreign:
            anomaly["type"] = MULTI_CLIENT
            anomaly["severity"] = "critical"
            anomaly["detail"] = (
                f"foreign Modbus client(s) {', '.join(foreign)} hold established "
                f"sessions to {host}:{port} while our polls fail — session-limited "
                f"inverter is client-blocked, kill the foreign client (NO reset "
                f"needed). Was: {anomaly.get('detail')}"
            )
            log.info(
                "multi_client_detected",
                device_id=anomaly.get("device_id"),
                foreign=foreign,
            )
        else:
            # None (no data/error) and [] (empty result) intentionally fold
            # into ONE branch: an empty result does not prove coverage, so it
            # must never read as "no foreign client".
            anomaly["detail"] = (
                f"TCP accept ok (or not probed: the proxy holds the session), no conntrack evidence — BEFORE any reset check "
                f"for foreign Modbus clients (ss/conntrack on all sites). "
                f"{anomaly.get('detail')}"
            )
    return anomalies


# ---------------------------------------------------------------------------
# Solar elevation (NOAA approximation, pure)
# ---------------------------------------------------------------------------


def solar_elevation_deg(lat: float, lon: float, when_utc: datetime) -> float:
    """Solar elevation angle in degrees for (lat, lon) at ``when_utc``.

    NOAA solar-position approximation. ``when_utc`` is interpreted as UTC:
    pass a timezone-aware UTC datetime, or a naive datetime that you know to
    be UTC. Positive elevation = sun above the horizon. Pure (math only).

    Reference: NOAA Solar Calculator equations (fractional year -> equation
    of time + solar declination -> hour angle -> elevation).
    """
    # Normalise to UTC. If tz-aware, convert; if naive, assume already UTC.
    if when_utc.tzinfo is not None:
        when_utc = when_utc.astimezone(timezone.utc)
    else:
        when_utc = when_utc.replace(tzinfo=timezone.utc)

    day_of_year = when_utc.timetuple().tm_yday
    hour = when_utc.hour + when_utc.minute / 60.0 + when_utc.second / 3600.0

    # Fractional year (radians). NOAA uses 365 (or 366 in leap years); the
    # one-day approximation is well within the precision we need for a >3°
    # daylight gate.
    gamma = (2.0 * math.pi / 365.0) * (day_of_year - 1 + (hour - 12.0) / 24.0)

    # Equation of time (minutes).
    eqtime = 229.18 * (
        0.000075
        + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma)
        - 0.040849 * math.sin(2 * gamma)
    )

    # Solar declination (radians).
    decl = (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma)
        + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma)
        + 0.00148 * math.sin(3 * gamma)
    )

    # True solar time (minutes). Longitude in degrees; UTC, so timezone
    # offset is 0. time_offset = eqtime + 4*lon.
    time_offset = eqtime + 4.0 * lon
    tst = (hour * 60.0) + time_offset
    # Hour angle (degrees): 0 at solar noon, negative in morning.
    ha = (tst / 4.0) - 180.0
    ha_rad = math.radians(ha)

    lat_rad = math.radians(lat)
    # Solar zenith angle.
    cos_zenith = (
        math.sin(lat_rad) * math.sin(decl)
        + math.cos(lat_rad) * math.cos(decl) * math.cos(ha_rad)
    )
    cos_zenith = max(-1.0, min(1.0, cos_zenith))
    zenith = math.acos(cos_zenith)
    elevation = 90.0 - math.degrees(zenith)
    return elevation


# ---------------------------------------------------------------------------
# Device parsing helpers
# ---------------------------------------------------------------------------


def _as_float(value: Any, default: float = 0.0) -> float:
    """Coerce a possibly-None/str value to float, defaulting on failure."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def is_inverter_row(device: dict[str, Any]) -> bool:
    """True iff ``device`` is a real inverter (not a venus/virtual pseudo row).

    A real device row has a known type AND carries the per-device poll keys
    (last_successful_poll_at / poll_interval) that pseudo rows lack.
    """
    if device.get("type") not in KNOWN_DEVICE_TYPES:
        return False
    if device.get("id") in {"venus", "virtual", "mqtt_pub", "influx"}:
        return False
    return "last_successful_poll_at" in device and "poll_interval" in device


def build_location_map(
    locations_payload: dict[str, Any] | None,
) -> dict[str, tuple[float | None, float | None]]:
    """Build location_id -> (lat, lon) from the /api/locations payload."""
    result: dict[str, tuple[float | None, float | None]] = {}
    if not locations_payload:
        return result
    for loc in locations_payload.get("locations", []) or []:
        if not isinstance(loc, dict):
            continue
        loc_id = loc.get("id")
        if not isinstance(loc_id, str):
            continue
        lat = loc.get("lat")
        lon = loc.get("lon")
        result[loc_id] = (
            lat if isinstance(lat, (int, float)) else None,
            lon if isinstance(lon, (int, float)) else None,
        )
    return result


# ---------------------------------------------------------------------------
# Cooldown state
# ---------------------------------------------------------------------------


@dataclass
class StateEntry:
    """Per (device_id, anomaly_type) cooldown + auto-fix bookkeeping."""

    first_seen: float
    last_alerted: float = 0.0
    last_autofix_attempt: float = 0.0
    autofix_count: int = 0
    resolved: bool = False
    #: Anomaly type presented on the last alert for this (aliased) key. Lets
    #: the shared MODBUS_HANG/MULTI_CLIENT incident stream re-alert exactly
    #: once on a diagnosis UPGRADE (hang -> named foreign client) without
    #: re-alerting on the downgrade or on every run. "" on legacy state files
    #: (tolerant from_dict) — treated as "unknown previous type".
    last_type: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "first_seen": self.first_seen,
            "last_alerted": self.last_alerted,
            "last_autofix_attempt": self.last_autofix_attempt,
            "autofix_count": self.autofix_count,
            "resolved": self.resolved,
            "last_type": self.last_type,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StateEntry":
        return cls(
            first_seen=_as_float(data.get("first_seen")),
            last_alerted=_as_float(data.get("last_alerted")),
            last_autofix_attempt=_as_float(data.get("last_autofix_attempt")),
            autofix_count=int(_as_float(data.get("autofix_count"))),
            resolved=bool(data.get("resolved", False)),
            last_type=str(data.get("last_type") or ""),
        )


@dataclass
class ProductionRecord:
    """Per-device daily production history for the micro DEAD gate.

    Persisted in the anomaly-state.json under ``production[device_id]`` so it
    survives across hourly runs. ``day`` is the local/UTC day key (YYYY-MM-DD);
    a run on a new day resets the record. ``seen_producing`` flips True the
    first time the device read above sibling_producing_w today;
    ``zero_runs`` counts consecutive detector runs at ~0W.
    """

    day: str = ""
    seen_producing: bool = False
    zero_runs: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day,
            "seen_producing": self.seen_producing,
            "zero_runs": self.zero_runs,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProductionRecord":
        day = data.get("day")
        return cls(
            day=day if isinstance(day, str) else "",
            seen_producing=bool(data.get("seen_producing", False)),
            zero_runs=int(_as_float(data.get("zero_runs"))),
        )


@dataclass
class RadioGapRecord:
    """Per-device persistence clock for an OpenDTU source-stale radio gap.

    Persisted under ``radio_gaps[device_id]``. Unlike ProductionRecord this
    is NOT reset on a day boundary — a radio gap that survives the night must
    keep its clock so a real (radio-silent) death is paged the next day rather
    than silently forgotten.

    ``daylight_stale_s`` accumulates wall-seconds the device has been
    source-stale *while a sibling was producing* (= daylight confirmed). Night
    excludes itself (the sibling is also 0W at night, so the device never
    reaches the accumulation path then). The gap is reclassified DEAD once this
    exceeds ``cfg.radio_gap_escalate_s``. ``gap_start_ts`` is kept only for the
    duration log (R36 — tune the threshold from real data later).
    """

    gap_start_ts: float = 0.0
    daylight_stale_s: float = 0.0
    last_eval_ts: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "gap_start_ts": self.gap_start_ts,
            "daylight_stale_s": self.daylight_stale_s,
            "last_eval_ts": self.last_eval_ts,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RadioGapRecord":
        return cls(
            gap_start_ts=_as_float(data.get("gap_start_ts")),
            daylight_stale_s=_as_float(data.get("daylight_stale_s")),
            last_eval_ts=_as_float(data.get("last_eval_ts")),
        )


@dataclass
class DetectorState:
    """In-memory mirror of the on-disk cooldown state file."""

    entries: dict[str, StateEntry] = field(default_factory=dict)
    production: dict[str, ProductionRecord] = field(default_factory=dict)
    radio_gaps: dict[str, RadioGapRecord] = field(default_factory=dict)

    @staticmethod
    def key(device_id: str, anomaly_type: str) -> str:
        return f"{device_id}:{anomaly_type}"


def load_state(path: Path) -> DetectorState:
    """Load the cooldown state file. Defensive: corrupt/missing -> fresh.

    Mirrors recovery.load_pending_marker's tolerant parsing — a corrupt state
    file must never crash the detector; we just start with empty state.
    """
    if not path.exists():
        return DetectorState()
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError, ValueError) as e:
        log.warning("state_unreadable", path=str(path), error=str(e))
        return DetectorState()
    if not isinstance(data, dict):
        log.warning("state_wrong_type", path=str(path))
        return DetectorState()
    entries: dict[str, StateEntry] = {}
    raw_entries = data.get("entries")
    if isinstance(raw_entries, dict):
        for k, v in raw_entries.items():
            if isinstance(v, dict):
                try:
                    entries[k] = StateEntry.from_dict(v)
                except (TypeError, ValueError):
                    continue
    # Production history sub-dict — tolerate missing/corrupt (start fresh),
    # mirroring the entries defensiveness above.
    production: dict[str, ProductionRecord] = {}
    raw_production = data.get("production")
    if isinstance(raw_production, dict):
        for k, v in raw_production.items():
            if isinstance(v, dict):
                try:
                    production[k] = ProductionRecord.from_dict(v)
                except (TypeError, ValueError):
                    continue
    # Radio-gap persistence clocks — same tolerant parse (start fresh on
    # missing/corrupt). A lost radio_gaps map fails safe: a still-active gap
    # rebuilds its clock from the next run (worst case: paged slightly later).
    radio_gaps: dict[str, RadioGapRecord] = {}
    raw_radio_gaps = data.get("radio_gaps")
    if isinstance(raw_radio_gaps, dict):
        for k, v in raw_radio_gaps.items():
            if isinstance(v, dict):
                try:
                    radio_gaps[k] = RadioGapRecord.from_dict(v)
                except (TypeError, ValueError):
                    continue
    return DetectorState(entries=entries, production=production, radio_gaps=radio_gaps)


def save_state(path: Path, state: DetectorState) -> None:
    """Atomically persist the state file (tempfile + os.replace).

    Best-effort: a write failure is logged, not raised — the next run just
    starts from the prior persisted state. Creates the parent dir if needed.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "entries": {k: e.to_dict() for k, e in state.entries.items()},
            "production": {k: p.to_dict() for k, p in state.production.items()},
            "radio_gaps": {k: r.to_dict() for k, r in state.radio_gaps.items()},
        }
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        os.replace(tmp, path)
    except OSError as e:
        log.warning("state_write_failed", path=str(path), error=str(e))


# ---------------------------------------------------------------------------
# Escalation webhooks / push notifications (via stdlib HTTP)
# ---------------------------------------------------------------------------

#: Webhook payload limits.
_PUSH_TEXT_MAX = 300
_ESC_CONTEXT_MAX = 4000

#: Source labels carried in the webhook payloads.
_PUSH_SOURCE = "pv-inverter-master:anomaly"
_ESC_SOURCE = "pv-inverter-master:anomaly"

#: Delay before the single retry on a 202 pending escalation.
_ESC_PENDING_RETRY_S = 5.0


def notify_push(
    text: str,
    cfg: DetectorConfig,
    *,
    severity: str = "info",
    dedup_key: str | None = None,
) -> None:
    """POST a push notification to {cfg.webhook_base_url}/api/push.

    Never raises. Gated by ``cfg.push_enabled`` (defense-in-depth — the
    caller in main() also gates). Text is truncated to the endpoint's 300-char
    limit. ``severity`` is one of info|warn|error|recovered; ``dedup_key`` is
    forwarded when provided so the receiver can collapse/clear duplicate alerts.
    """
    if not cfg.push_enabled:
        log.info("notify_push_disabled", text=text, severity=severity)
        return
    text = text[:_PUSH_TEXT_MAX]
    payload: dict[str, Any] = {
        "text": text,
        "source": _PUSH_SOURCE,
        "severity": severity,
    }
    if dedup_key is not None:
        payload["dedup_key"] = dedup_key
    resp = _post_json(f"{cfg.webhook_base_url}/api/push", payload, cfg.http_timeout_s)
    if resp is None:
        log.error("notify_push_no_response", severity=severity, dedup_key=dedup_key)
        return
    log.info(
        "notify_push_sent",
        severity=severity,
        dedup_key=dedup_key,
        outcome=resp.get("status") or resp.get("action") or resp.get("result"),
        rate_limited=bool(resp.get("rate_limited")),
        dedup=bool(resp.get("dedup") or resp.get("deduped")),
    )


def _build_escalation_context(anomaly: dict[str, Any]) -> str:
    """Summarize an anomaly into a webhook context string (<=4000 chars).

    Only the fields the detector actually carries on an anomaly/alert dict are
    used (device_id, type, severity, detail, location_id) plus any optional
    enrichment the caller attached (device name/type, autofix outcome). Missing
    fields degrade gracefully rather than asserting a shape that may not exist.
    """
    parts: list[str] = []
    atype = anomaly.get("type", "UNKNOWN")
    device_id = anomaly.get("device_id", "?")
    parts.append(f"Anomaly {atype} on device {device_id}.")
    name = anomaly.get("device_name")
    dtype = anomaly.get("device_type")
    if name or dtype:
        parts.append(f"Device: {name or device_id} ({dtype or 'unknown type'}).")
    loc = anomaly.get("location_id")
    if loc:
        parts.append(f"Location: {loc}.")
    severity = anomaly.get("severity")
    if severity:
        parts.append(f"Severity: {severity}.")
    detail = anomaly.get("detail")
    if detail:
        parts.append(f"Detail: {detail}.")
    autofix = anomaly.get("autofix")
    if autofix:
        parts.append(f"Auto-fix attempted: {autofix}.")
    parts.append(
        "This is an unresolved PV inverter anomaly surfaced by the hourly "
        "anomaly detector in pv-inverter-master. Please investigate."
    )
    return " ".join(parts)[:_ESC_CONTEXT_MAX]


def escalate(
    anomaly: dict[str, Any],
    cfg: DetectorConfig,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Send an unresolved anomaly to the escalation webhook.

    POSTs to {cfg.webhook_base_url}/api/escalate.
    Never raises. Gated by ``cfg.escalation_enabled``. When the receiver
    answers ``action == "pending"`` (HTTP 202 — startup pending),
    wait ~5s via the injected ``sleep`` and POST once more to deliver the
    context once the receiver is ready.
    """
    if not cfg.escalation_enabled:
        log.info("escalation_disabled", anomaly=anomaly)
        return
    # Stable per-anomaly key lets the receiver coalesce repeated escalations
    # for the same device and cooldown type. The detector still POSTs every
    # 6 h so a persistent critical condition is never masked. Suppressing
    # unchanged warnings here could also hide a real inverter outage whose
    # only signal is STALE_POLL. The key mirrors push notification dedup
    # (device_id:cooldown_type); coalescing remains the receiver's decision.
    dedup_key = (
        f"pv-inverter-master:anomaly:"
        f"{anomaly.get('device_id')}:{_cooldown_type(str(anomaly.get('type')))}"
    )
    payload = {
        "repo": "pv-inverter-master",
        "context": _build_escalation_context(anomaly),
        "source": _ESC_SOURCE,
        "dedup_key": dedup_key,
    }
    url = f"{cfg.webhook_base_url}/api/escalate"
    resp = _post_json(url, payload, cfg.http_timeout_s)
    if resp is None:
        log.error("escalation_no_response", device_id=anomaly.get("device_id"))
        return
    if resp.get("action") == "pending":
        log.info("escalation_pending_retry", device_id=anomaly.get("device_id"))
        sleep(_ESC_PENDING_RETRY_S)
        resp = _post_json(url, payload, cfg.http_timeout_s)
        if resp is None:
            log.error(
                "escalation_retry_no_response",
                device_id=anomaly.get("device_id"),
            )
            return
    log.info(
        "escalation_sent",
        device_id=anomaly.get("device_id"),
        action=resp.get("action"),
        receiver_id=resp.get("receiver_id"),
    )


# ---------------------------------------------------------------------------
# Detection rules
# ---------------------------------------------------------------------------


def _has_dc_production(device: dict[str, Any], ac_zero_w: float) -> bool:
    """True iff the device shows DC string production above the zero threshold.

    The /api/devices list payload does NOT carry per-string dc_channels, so
    in practice this reduces to the AC power reading. The dc_channels branch
    is kept in code shape so a future richer payload slots in without a
    rule rewrite.
    """
    dc_channels = device.get("dc_channels")
    if isinstance(dc_channels, list) and dc_channels:
        total = 0.0
        for ch in dc_channels:
            if isinstance(ch, dict):
                total += _as_float(ch.get("power_w"))
        return total > ac_zero_w
    # No per-string data: fall back to AC power.
    return _as_float(device.get("power_w")) > ac_zero_w


def _poll_cadence_s(device: dict[str, Any]) -> float:
    """The cadence the device ACTUALLY runs at, for staleness maths.

    `poll_interval` in the payload is the RAW config value, where 0.0 means
    "use the default" — it is 0.0 for almost every device, so any threshold
    derived from it silently collapses to its floor. `effective_poll_interval`
    is the value the registry resolved (per-device > gateway > proxy default,
    then floored by the plugin minimum).

    Falls back to the raw value when the resolved one is absent: the detector
    also runs against REDUCED payloads (tests, pseudo rows — see
    `_is_real_device_row`) that do not carry the newer field.

    NOTE: on the current fleet this changes no threshold at all — the largest
    effective cadence is 20s (hoymiles), and 5x that is still under the 300s
    floor. It is a latent-correctness fix, not a recalibration. Whether 300s
    is the right floor is a separate, measurement-driven question.
    """
    effective = _as_float(device.get("effective_poll_interval"))
    if effective > 0:
        return effective
    return _as_float(device.get("poll_interval"))


def _data_is_fresh(device: dict[str, Any], now_epoch: float) -> bool:
    """True iff the device's power reading comes from a fresh successful poll.

    Mirrors the STALE_POLL threshold (max(5x effective cadence, 300s)). A device
    that has never polled successfully (last_successful_poll_at None/0) or
    whose last success is older than the threshold carries a default/frozen
    power_w that must not drive power-based rules (DEAD_INVERTER evidence,
    sibling "producing" anchor).
    """
    if "last_successful_poll_at" not in device:
        # Field absent entirely (not part of the payload shape): give the
        # benefit of the doubt — absence of evidence is not staleness. The
        # live /api/devices payload ALWAYS carries the key (None until the
        # first success), so this branch only affects reduced payloads.
        return True
    last_ok = device.get("last_successful_poll_at")
    if not isinstance(last_ok, (int, float)) or not last_ok:
        return False
    threshold = max(5.0 * _poll_cadence_s(device), 300.0)
    return (now_epoch - float(last_ok)) <= threshold


def _update_production_record(
    record: ProductionRecord,
    device: dict[str, Any],
    cfg: DetectorConfig,
    day_key: str,
) -> None:
    """Update a device's daily production record in place for this run.

    - Reset on a new local/UTC day (seen_producing=False, zero_runs=0).
    - power_w > sibling_producing_w  -> seen_producing=True, zero_runs=0.
    - power_w <= ac_zero_w           -> zero_runs += 1.
    - in-between                     -> leave zero_runs as-is.
    """
    if record.day != day_key:
        record.day = day_key
        record.seen_producing = False
        record.zero_runs = 0
    power_w = _as_float(device.get("power_w"))
    if power_w > cfg.sibling_producing_w:
        record.seen_producing = True
        record.zero_runs = 0
    elif power_w <= cfg.ac_zero_w:
        record.zero_runs += 1


def classify_anomalies(
    devices_payload: dict[str, Any] | None,
    location_map: dict[str, tuple[float | None, float | None]],
    cfg: DetectorConfig,
    now_utc: datetime,
    production: dict[str, ProductionRecord] | None = None,
    radio_gaps: dict[str, RadioGapRecord] | None = None,
) -> list[dict[str, Any]]:
    """Run the three detection rules over enabled inverter rows.

    Returns a list of anomaly dicts: {device_id, type, severity, detail,
    location_id}. State seams are ``production`` (per-device daily production
    history) and ``radio_gaps`` (per-device source-stale persistence clocks),
    both read AND mutated in place; everything else is pure over the inputs
    (clock via now_utc). Devices in ``cfg.exclude_ids`` are skipped entirely —
    no anomalies, no production/radio-gap tracking.
    """
    anomalies: list[dict[str, Any]] = []
    if not devices_payload:
        return anomalies
    if production is None:
        production = {}
    if radio_gaps is None:
        radio_gaps = {}

    # Day key for the production-history reset, derived from the injected
    # clock (no real datetime in the decision path).
    day_key = now_utc.strftime("%Y-%m-%d")

    if cfg.exclude_ids:
        log.debug("anomaly_excluded_ids", excluded=sorted(cfg.exclude_ids))

    rows = [
        d
        for d in (devices_payload.get("devices", []) or [])
        if isinstance(d, dict)
        and is_inverter_row(d)
        and d.get("enabled", True)
        and str(d.get("id")) not in cfg.exclude_ids
    ]

    now_epoch = now_utc.timestamp()

    # Maintain per-device production history before running the rules so the
    # micro DEAD gate sees this run's updated counters.
    for d in rows:
        if d.get("type") in PV_TYPES:
            device_id = str(d.get("id"))
            record = production.get(device_id)
            if record is None:
                record = ProductionRecord()
                production[device_id] = record
            _update_production_record(record, d, cfg, day_key)
            # Radio-gap persistence clock: SUBSTANTIAL recovery (the device
            # genuinely produces again) clears it. A flap — a single
            # reachable-but-0W frame — has power_w<=ac_zero_w and does NOT
            # reset, so a dead micro that flickers reachable can't sneak its
            # clock back to zero (masking via the flap back door).
            if _as_float(d.get("power_w")) > cfg.ac_zero_w:
                gap = radio_gaps.pop(device_id, None)
                if gap is not None and gap.gap_start_ts > 0:
                    # R36: log the recovered-gap duration so the escalation
                    # threshold can be tuned from a real distribution later.
                    log.info(
                        "radio_gap_recovered",
                        device_id=device_id,
                        gap_duration_s=int(now_epoch - gap.gap_start_ts),
                        daylight_stale_s=int(gap.daylight_stale_s),
                    )

    # Group by location_id for the DEAD/WEATHER sibling logic.
    by_location: dict[str, list[dict[str, Any]]] = {}
    for d in rows:
        loc_id = d.get("location_id") or ""
        by_location.setdefault(loc_id, []).append(d)

    for d in rows:
        device_id = str(d.get("id"))
        dtype = d.get("type")
        loc_id = d.get("location_id") or ""
        last_error = d.get("last_poll_error")

        conn = d.get("connection_state")
        last_ok = d.get("last_successful_poll_at")
        stale_threshold = max(5.0 * _poll_cadence_s(d), 300.0)

        # --- MODBUS_HANG (Modbus-TCP types: sungrow + solaredge) ---
        if dtype in MODBUS_TCP_TYPES and isinstance(last_error, str) and last_error:
            lowered = last_error.lower()
            # Single-sample guard: last_poll_error is set after ANY one failed
            # poll and cleared on the next success, so the signature alone can
            # be a transient timeout that happens to coincide with the hourly
            # oneshot. A real hang always corroborates: the connection manager
            # has left "connected" (3 consecutive failures) and/or the last
            # successful poll is older than the staleness threshold. Without
            # this gate a flapping device re-alerts critical every run (the
            # resolved->active transition bypasses the cooldown).
            corroborated = conn not in (None, "connected") or (
                isinstance(last_ok, (int, float))
                and (now_epoch - float(last_ok)) > stale_threshold
            )
            if corroborated and any(sig in lowered for sig in MODBUS_HANG_SIGNATURES):
                anomalies.append(
                    {
                        "device_id": device_id,
                        "type": MODBUS_HANG,
                        "severity": "critical",
                        "detail": f"{dtype} Modbus hang: {last_error}",
                        "location_id": loc_id,
                    }
                )

        # --- STALE_POLL / DISCONNECTED (all types) ---
        is_stale = False
        stale_detail = ""
        if conn not in (None, "connected"):
            is_stale = True
            # Carry the staleness age alongside the raw state name. The state
            # alone is misleading: "night_mode" is just the connection
            # manager's >5min-continuous-failure label and says nothing about
            # HOW LONG the device has been unreachable (SE30K incident
            # 2026-06-10: a 13h daylight Modbus hang read as "night_mode").
            stale_detail = f"connection_state={conn}"
            if isinstance(last_ok, (int, float)):
                age = max(0, int(now_epoch - float(last_ok)))
                stale_detail += f", no successful poll for {age}s"
        elif isinstance(last_ok, (int, float)) and (
            now_epoch - float(last_ok)
        ) > stale_threshold:
            is_stale = True
            stale_detail = (
                f"no successful poll for {int(now_epoch - float(last_ok))}s "
                f"(threshold {int(stale_threshold)}s)"
            )
        elif isinstance(last_error, str) and last_error:
            is_stale = True
            stale_detail = f"poll error: {last_error}"
        if is_stale:
            anomalies.append(
                {
                    "device_id": device_id,
                    "type": STALE_POLL,
                    "severity": "warning",
                    "detail": stale_detail,
                    "location_id": loc_id,
                }
            )

        # --- DEAD_INVERTER (PV types only, sun-gated) ---
        if dtype in PV_TYPES:
            dead = _classify_dead(d, by_location.get(loc_id, []), location_map, cfg, now_utc)
            if dead is not None:
                # Micro-inverter gate: a single micro legitimately reads 0W
                # (shading/orientation) while siblings produce. Only KEEP a
                # micro DEAD_INVERTER if it produced earlier today AND has read
                # ~0W for >= micro_dead_min_zero_runs consecutive runs. Large
                # inverters and WEATHER_OR_SITE are unaffected.
                if dtype in MICRO_PV_TYPES and dead["type"] == DEAD_INVERTER:
                    # Radio-gap branch: source_stale is POSITIVE evidence of an
                    # OpenDTU DTU<->micro radio gap (success poll, no fresh
                    # data) — distinct from a genuinely reachable 0W reading. A
                    # transient gap is not a dead inverter, so downgrade to
                    # RADIO_GAP (warning) — UNTIL the gap has persisted through
                    # enough DAYLIGHT that it can no longer be transient, then
                    # re-promote to DEAD (a non-recovering gap = real fault: a
                    # dead micro or a broken radio). We only reach here when
                    # _classify_dead returned DEAD, i.e. sun is up AND a sibling
                    # is producing — so daylight is already confirmed (this is
                    # the sibling/daylight anchor that also makes an all-micros-
                    # stale infra outage NOT page as a single death: no
                    # producing sibling -> not DEAD -> never enters here).
                    if bool(d.get("source_stale")):
                        gap = radio_gaps.get(device_id)
                        if gap is None:
                            gap = RadioGapRecord(
                                gap_start_ts=now_epoch, last_eval_ts=now_epoch
                            )
                            radio_gaps[device_id] = gap
                        else:
                            delta = now_epoch - gap.last_eval_ts
                            if delta < 0.0:
                                delta = 0.0
                            elif delta > RADIO_GAP_MAX_DELTA_S:
                                delta = RADIO_GAP_MAX_DELTA_S
                            gap.daylight_stale_s += delta
                            gap.last_eval_ts = now_epoch
                        if gap.daylight_stale_s <= cfg.radio_gap_escalate_s:
                            # Young gap -> soft warning, NOT a DEAD critical.
                            anomalies.append(
                                {
                                    "device_id": device_id,
                                    "type": RADIO_GAP,
                                    "severity": "warning",
                                    "detail": (
                                        "OpenDTU radio gap (source stale; "
                                        f"{int(gap.daylight_stale_s)}s daylight "
                                        "stale) — likely transient, escalates to "
                                        f"DEAD after {int(cfg.radio_gap_escalate_s)}s"
                                    ),
                                    "location_id": loc_id,
                                }
                            )
                            continue
                        # Chronic gap -> fall through and keep DEAD (critical).
                        log.info(
                            "radio_gap_escalated_to_dead",
                            device_id=device_id,
                            daylight_stale_s=int(gap.daylight_stale_s),
                            threshold_s=int(cfg.radio_gap_escalate_s),
                        )
                    else:
                        # NOT source-stale: a genuinely reachable 0W micro.
                        # Keep the original gate (a micro legitimately reads 0W
                        # when shaded; only flag if it produced earlier today
                        # then dropped for >= micro_dead_min_zero_runs runs).
                        record = production.get(device_id)
                        gated = (
                            record is not None
                            and record.seen_producing
                            and record.zero_runs >= cfg.micro_dead_min_zero_runs
                        )
                        if not gated:
                            continue
                anomalies.append(dead)

    return anomalies


def _classify_dead(
    device: dict[str, Any],
    siblings: list[dict[str, Any]],
    location_map: dict[str, tuple[float | None, float | None]],
    cfg: DetectorConfig,
    now_utc: datetime,
) -> dict[str, Any] | None:
    """DEAD_INVERTER / WEATHER_OR_SITE classification for one PV device.

    Returns None when not dead. Requires sun-up at the device's location
    (sun-gate is a hard precondition); a device whose location has no lat/lon
    cannot be sun-gated and is never flagged DEAD here.
    """
    device_id = str(device.get("id"))
    loc_id = device.get("location_id") or ""

    # Freshness gate: a ~0W reading is only EVIDENCE if it comes from a
    # fresh successful poll. With no (or stale) last success, power_w is a
    # startup default or frozen value — that device is already covered by
    # STALE_POLL / MODBUS_HANG. Live misfire 2026-06-11 09:00: DEAD_INVERTER
    # critical on a never-polled, Modbus-wedged SE30K (power_w=0 default).
    if not _data_is_fresh(device, now_utc.timestamp()):
        return None

    # This device must read ~0W (AC fallback / no DC production).
    if _has_dc_production(device, cfg.ac_zero_w):
        return None

    # Sun gate: required precondition.
    coords = location_map.get(loc_id)
    if not coords or coords[0] is None or coords[1] is None:
        # Cannot sun-gate -> never emit DEAD (STALE/MODBUS still apply elsewhere).
        return None
    lat, lon = coords
    elevation = solar_elevation_deg(lat, lon, now_utc)
    if elevation <= cfg.daylight_min_elev_deg:
        return None  # Night / too low — no DEAD alert.

    # Look at PV siblings at the same location.
    pv_siblings = [
        s
        for s in siblings
        if s.get("type") in PV_TYPES and str(s.get("id")) != device_id
    ]
    # A sibling only counts as "producing" with FRESH data — a wedged
    # device's frozen power_w (e.g. 5658 W frozen during the 2026-06-10
    # SE30K hang) must not mark a genuinely weather-zero sibling DEAD.
    now_epoch = now_utc.timestamp()
    producing_sibling = any(
        _as_float(s.get("power_w")) > cfg.sibling_producing_w
        and _data_is_fresh(s, now_epoch)
        for s in pv_siblings
    )

    if producing_sibling:
        return {
            "device_id": device_id,
            "type": DEAD_INVERTER,
            "severity": "critical",
            "detail": (
                "PV inverter at ~0W in daylight "
                f"(elev={elevation:.1f}°) while a sibling is producing"
            ),
            "location_id": loc_id,
        }

    # No sibling producing. If ALL site PV inverters read ~0W in daylight,
    # this is a weather/site condition, not a dead inverter.
    all_zero = all(
        not _has_dc_production(s, cfg.ac_zero_w)
        for s in ([device] + pv_siblings)
    )
    if all_zero:
        return {
            "device_id": device_id,
            "type": WEATHER_OR_SITE,
            "severity": "info",
            "detail": (
                "All site PV inverters at ~0W in daylight "
                f"(elev={elevation:.1f}°) — weather or site-wide condition"
            ),
            "location_id": loc_id,
        }

    return None


# ---------------------------------------------------------------------------
# Auto-fix (MODBUS_HANG only)
# ---------------------------------------------------------------------------


def _device_recovered(devices_payload: dict[str, Any] | None, device_id: str, before_last_ok: float) -> bool:
    """True iff ``device_id`` looks healthy after a reboot.

    Recovery = connected AND last_successful_poll_at advanced AND no
    last_poll_error.
    """
    if not devices_payload:
        return False
    for d in devices_payload.get("devices", []) or []:
        if not isinstance(d, dict) or str(d.get("id")) != device_id:
            continue
        conn = d.get("connection_state")
        last_ok = d.get("last_successful_poll_at")
        last_error = d.get("last_poll_error")
        advanced = isinstance(last_ok, (int, float)) and float(last_ok) > before_last_ok
        return conn == "connected" and advanced and not last_error
    return False


def _supports_reboot(devices_payload: dict[str, Any], device_id: str) -> bool:
    """True iff the device opts into the reboot endpoint.

    The webapp payload carries ``supports_reboot`` (duck-typed on the plugin
    having a ``reboot()``). Legacy payloads without the field fall back to the
    pre-2026-06-10 behavior: only sungrow was ever auto-rebooted.
    """
    for d in devices_payload.get("devices", []) or []:
        if isinstance(d, dict) and str(d.get("id")) == device_id:
            if "supports_reboot" in d:
                return bool(d["supports_reboot"])
            return d.get("type") == "sungrow"
    return False


def autofix_modbus_hang(
    anomaly: dict[str, Any],
    devices_payload: dict[str, Any],
    state: DetectorState,
    cfg: DetectorConfig,
    *,
    post: Callable[[str, str, float], bool],
    fetch: Callable[[str, float], dict[str, Any] | None],
    sleep: Callable[[float], None],
    now: Callable[[], float],
) -> dict[str, Any]:
    """Attempt to auto-fix a MODBUS_HANG via a reboot POST, capped per window.

    Returns an outcome dict {attempted, rebooted, recovered, capped}. Records
    last_autofix_attempt + autofix_count into ``state``.
    """
    device_id = anomaly["device_id"]
    skey = DetectorState.key(device_id, MODBUS_HANG)
    entry = state.entries.get(skey)
    now_ts = now()

    # Count attempts within the rolling window.
    if entry is not None and entry.last_autofix_attempt > 0:
        if (now_ts - entry.last_autofix_attempt) > cfg.autofix_window_s:
            # Window elapsed — reset the counter.
            entry.autofix_count = 0
    attempts = entry.autofix_count if entry is not None else 0

    if attempts >= cfg.autofix_max_per_window:
        log.warning(
            "autofix_capped",
            device_id=device_id,
            attempts=attempts,
            cap=cfg.autofix_max_per_window,
        )
        return {"attempted": False, "rebooted": False, "recovered": False, "capped": True}

    # Snapshot the pre-reboot last_successful_poll_at for the recovery check.
    before_last_ok = 0.0
    for d in devices_payload.get("devices", []) or []:
        if isinstance(d, dict) and str(d.get("id")) == device_id:
            before_last_ok = _as_float(d.get("last_successful_poll_at"))
            break

    log.info("autofix_reboot_attempt", device_id=device_id, attempt=attempts + 1)
    rebooted = post(cfg.base_url, device_id, cfg.http_timeout_s)

    # Record the attempt regardless of reboot POST success.
    if entry is None:
        entry = StateEntry(first_seen=now_ts)
        state.entries[skey] = entry
    entry.last_autofix_attempt = now_ts
    entry.autofix_count = attempts + 1

    recovered = False
    if rebooted:
        sleep(cfg.reboot_backoff_s)
        refreshed = fetch(f"{cfg.base_url}/api/devices", cfg.http_timeout_s)
        recovered = _device_recovered(refreshed, device_id, before_last_ok)
        log.info("autofix_reboot_result", device_id=device_id, recovered=recovered)
    else:
        log.warning("autofix_reboot_post_failed", device_id=device_id)

    return {
        "attempted": True,
        "rebooted": rebooted,
        "recovered": recovered,
        "capped": False,
    }


# ---------------------------------------------------------------------------
# Cooldown dedupe + recovery transitions
# ---------------------------------------------------------------------------


#: Cooldown/dedup identity aliasing: MULTI_CLIENT is a diagnosis REFINEMENT of
#: MODBUS_HANG (same underlying incident: our polls fail), so both share ONE
#: state key. Without the alias a conntrack flap (Influx blip -> coverage
#: gone -> back) would zip between the two keys producing a spurious critical
#: PLUS a bogus "recovered" per flip, and a deploy onto an active
#: MODBUS_HANG entry would fire a false all-clear on a live incident.
_COOLDOWN_TYPE_ALIAS: dict[str, str] = {MULTI_CLIENT: MODBUS_HANG}


def _cooldown_type(anomaly_type: str) -> str:
    """Canonical type for cooldown/recovery identity (see alias above)."""
    return _COOLDOWN_TYPE_ALIAS.get(anomaly_type, anomaly_type)


def apply_cooldown(
    anomalies: list[dict[str, Any]],
    state: DetectorState,
    cfg: DetectorConfig,
    now_ts: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Filter anomalies through cooldown and detect recovery transitions.

    Returns (alerts, recoveries):
    - alerts: anomalies to (re-)alert on — new, or past cooldown, or a
      new->active transition. Updates state.last_alerted for each.
    - recoveries: synthetic "recovered" notifications for entries that were
      previously active (not resolved) and are no longer present this pass.
    """
    active_keys = {
        DetectorState.key(a["device_id"], _cooldown_type(a["type"])) for a in anomalies
    }
    alerts: list[dict[str, Any]] = []

    for anomaly in anomalies:
        skey = DetectorState.key(anomaly["device_id"], _cooldown_type(anomaly["type"]))
        entry = state.entries.get(skey)
        if entry is None:
            # Brand-new anomaly -> alert + seed state.
            entry = StateEntry(
                first_seen=now_ts,
                last_alerted=now_ts,
                resolved=False,
                last_type=anomaly["type"],
            )
            state.entries[skey] = entry
            alerts.append(anomaly)
            continue
        # Existing entry. A resolved->active transition re-alerts.
        if entry.resolved:
            entry.resolved = False
            entry.first_seen = now_ts
            entry.last_alerted = now_ts
            entry.last_type = anomaly["type"]
            alerts.append(anomaly)
            continue
        # Diagnosis UPGRADE inside the shared incident stream: a hang that is
        # now positively identified as MULTI_CLIENT (foreign src_ip known —
        # actionable: kill that client) re-alerts ONCE despite the cooldown.
        # The reverse transition (conntrack coverage drops, back to plain
        # MODBUS_HANG) is information LOSS and stays suppressed — this is what
        # keeps a flapping conntrack source from producing an alert storm or
        # bogus recovered notifications (the alias key means both types are
        # ONE incident stream with ONE recovery).
        if anomaly["type"] == MULTI_CLIENT and entry.last_type != MULTI_CLIENT:
            entry.last_alerted = now_ts
            entry.last_type = anomaly["type"]
            alerts.append(anomaly)
            continue
        # Still active: re-alert only after cooldown.
        if (now_ts - entry.last_alerted) >= cfg.cooldown_s:
            entry.last_alerted = now_ts
            entry.last_type = anomaly["type"]
            alerts.append(anomaly)
        # else: within cooldown -> suppress.

    # Recovery transitions: entries previously active but now absent.
    recoveries: list[dict[str, Any]] = []
    for skey, entry in state.entries.items():
        if skey in active_keys or entry.resolved:
            continue
        device_id, _, anomaly_type = skey.partition(":")
        entry.resolved = True
        recoveries.append(
            {
                "device_id": device_id,
                "type": anomaly_type,
                "severity": "info",
                "detail": "recovered",
                "location_id": "",
            }
        )

    return alerts, recoveries


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _confirm_dead_anomalies(
    anomalies: list[dict[str, Any]],
    cfg: DetectorConfig,
    state: DetectorState,
    *,
    fetch: Callable[[str, float], dict[str, Any] | None],
    sleep: Callable[[float], None],
) -> list[dict[str, Any]]:
    """Re-sample /api/devices to confirm DEAD_INVERTER anomalies before alerting.

    For each DEAD_INVERTER anomaly, re-read fresh /api/devices payloads up to
    ``cfg.dead_confirm_resamples`` times (calling ``sleep(cfg.dead_confirm_gap_s)``
    BEFORE each re-sample fetch). If ANY re-sample shows that device producing
    (power_w > cfg.ac_zero_w), the anomaly is a transient 0W dip and is dropped;
    on suppression the device's production[device_id].zero_runs is reset to 0.
    A device absent from a re-sample, or a re-sample fetch returning None, counts
    as "still zero" (no recovery signal) — the anomaly is kept. Non-DEAD
    anomalies pass through unchanged and in original order. resamples<=0 disables
    confirmation entirely (legacy single-sample path).
    """
    if cfg.dead_confirm_resamples <= 0:
        return anomalies

    confirmed: list[dict[str, Any]] = []
    for anomaly in anomalies:
        if anomaly.get("type") != DEAD_INVERTER:
            confirmed.append(anomaly)
            continue

        device_id = str(anomaly.get("device_id"))
        recovered_power: float | None = None
        for _ in range(cfg.dead_confirm_resamples):
            sleep(cfg.dead_confirm_gap_s)
            payload = fetch(f"{cfg.base_url}/api/devices", cfg.http_timeout_s)
            if not payload:
                continue  # fetch failed -> treat as "still zero".
            row = None
            for d in payload.get("devices", []) or []:
                if isinstance(d, dict) and str(d.get("id")) == device_id:
                    row = d
                    break
            if row is None:
                continue  # device absent -> "still zero".
            power_w = _as_float(row.get("power_w"))
            if power_w > cfg.ac_zero_w:
                recovered_power = power_w
                break  # recovery seen -> stop re-sampling this device early.

        if recovered_power is not None:
            record = state.production.get(device_id)
            if record is not None:
                record.zero_runs = 0
            log.info(
                "dead_resample_suppressed",
                device_id=device_id,
                recovered_power_w=recovered_power,
            )
            continue  # drop the transient DEAD anomaly.
        confirmed.append(anomaly)

    return confirmed


def run_detection(
    cfg: DetectorConfig,
    state: DetectorState,
    *,
    fetch: Callable[[str, float], dict[str, Any] | None],
    post: Callable[[str, str, float], bool],
    sleep: Callable[[float], None],
    now: Callable[[], float],
    tcp_probe: Callable[[str, int, float], tuple[bool, str | None]] | None = None,
    conntrack: Callable[[str, set[str], float], list[str] | None] | None = None,
) -> dict[str, Any]:
    """One full detection pass over injected seams. Returns a summary dict.

    Does not write state — the caller (main) persists it after this returns,
    so tests can inspect both the returned summary and the mutated state.
    """
    now_ts = now()
    now_utc = datetime.fromtimestamp(now_ts, tz=timezone.utc)

    devices_payload = fetch(f"{cfg.base_url}/api/devices", cfg.http_timeout_s)
    locations_payload = fetch(f"{cfg.base_url}/api/locations", cfg.http_timeout_s)
    if devices_payload is None:
        log.error("devices_unreachable", base_url=cfg.base_url)
        return {"reachable": False, "anomalies": [], "alerts": [], "recoveries": [], "autofixes": []}

    location_map = build_location_map(locations_payload)
    # state.production is read + mutated in place (daily reset + zero-run
    # counters) so it persists across hourly runs when main() saves state.
    anomalies = classify_anomalies(
        devices_payload,
        location_map,
        cfg,
        now_utc,
        production=state.production,
        radio_gaps=state.radio_gaps,
    )

    # In-run re-sample confirmation: re-read /api/devices before letting any
    # DEAD_INVERTER anomaly alert, so a single transient 0W dip (esp. OpenDTU
    # micros) cannot trip a critical false alarm. classify stays pure — all
    # fetch/sleep for confirmation happens here. Runs BEFORE the
    # WEATHER/alertable/autofix/cooldown stages consume `anomalies`.
    anomalies = _confirm_dead_anomalies(
        anomalies, cfg, state, fetch=fetch, sleep=sleep
    )

    # MULTI_CLIENT evidence pass: retype corroborated MODBUS_HANGs whose port
    # still ACCEPTS TCP while a foreign client holds established sessions
    # (conntrack evidence) — a client-blocked session-limited inverter, not a
    # wedge (incident 2026-07-16). Fail-open on missing coverage; classify
    # stays pure, all probe IO happens in the enrichment seams. The probes are
    # OPT-IN (main() wires the real ones): run_detection callers that don't
    # inject them — every pre-existing test — stay hermetic by construction,
    # no accidental socket/Influx IO from fixture data.
    if tcp_probe is not None:
        anomalies = _enrich_multi_client(
            anomalies,
            devices_payload,
            cfg,
            tcp_probe=tcp_probe,
            conntrack=conntrack or _conntrack_foreign_sources,
        )

    # WEATHER_OR_SITE is informational-only: log + count it but keep it out of
    # the cooldown/alert/escalation/recovery path entirely. Only ALERTABLE_TYPES
    # flow into apply_cooldown (so WEATHER entries never enter cooldown state and
    # can never yield a spurious recovery notification).
    for anomaly in anomalies:
        if anomaly["type"] == WEATHER_OR_SITE:
            log.info(
                "weather_or_site_note",
                device_id=anomaly.get("device_id"),
                location_id=anomaly.get("location_id"),
                detail=anomaly.get("detail"),
            )
        elif anomaly["type"] == RADIO_GAP:
            # Informational-only (like WEATHER_OR_SITE): a positively-identified
            # transient OpenDTU radio gap. Logged + counted, NOT alerted — the
            # noise-reduction goal. A non-recovering gap escalates to
            # DEAD_INVERTER (alertable) inside classify_anomalies.
            log.info(
                "radio_gap_note",
                device_id=anomaly.get("device_id"),
                location_id=anomaly.get("location_id"),
                detail=anomaly.get("detail"),
            )
    alertable = [a for a in anomalies if a["type"] in ALERTABLE_TYPES]

    # Auto-fix MODBUS_HANG anomalies before cooldown dedupe so a successful
    # auto-fix can be recorded even when alerting is suppressed.
    autofixes: list[dict[str, Any]] = []
    for anomaly in alertable:
        if anomaly["type"] == MODBUS_HANG:
            # Reboot-based auto-fix only helps devices whose plugin actually
            # implements reboot() (sungrow WiNet-S). For the rest (solaredge)
            # MODBUS_HANG classifies + escalates, but never POSTs a reboot.
            if not _supports_reboot(devices_payload, anomaly["device_id"]):
                log.info(
                    "autofix_skipped_no_reboot_support",
                    device_id=anomaly["device_id"],
                )
                continue
            outcome = autofix_modbus_hang(
                anomaly,
                devices_payload,
                state,
                cfg,
                post=post,
                fetch=fetch,
                sleep=sleep,
                now=now,
            )
            autofixes.append({"device_id": anomaly["device_id"], **outcome})

    alerts, recoveries = apply_cooldown(alertable, state, cfg, now_ts)

    return {
        "reachable": True,
        "anomalies": anomalies,
        "alerts": alerts,
        "recoveries": recoveries,
        "autofixes": autofixes,
    }


#: Push notification severity per anomaly type. DEAD/MODBUS-class -> error; the softer
#: STALE class -> warn. Unknown types default to warn. MODBUS_HANG was bumped
#: warn->error on 2026-06-10 (SE30K incident): a corroborated Modbus hang is a
#: production outage, and for devices without reboot support there is no
#: auto-fix softening it. WEATHER_OR_SITE is informational-only and never
#: reaches this map (it is filtered out of the alert path in run_detection).
_PUSH_SEVERITY_BY_TYPE: dict[str, str] = {
    DEAD_INVERTER: "error",
    STALE_POLL: "warn",
    MODBUS_HANG: "error",
    MULTI_CLIENT: "error",
}


def _alert_severity(anomaly_type: str) -> str:
    """Map an anomaly type to a push notification severity (error for dead/disconnect)."""
    return _PUSH_SEVERITY_BY_TYPE.get(anomaly_type, "warn")


def _dispatch_events(
    result: dict[str, Any],
    cfg: DetectorConfig,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Route the detector's three event classes to the webhook endpoints.

    - alerts: push notification (severity by type, dedup_key device:type) + webhook escalation.
    - successful autofixes: push notification (warn) with a human-readable text.
    - recoveries: push notification (recovered) reusing the alert's dedup_key so the receiver
      clears its dedup state and never suppresses the recovery.

    Each call is independently gated inside the seam functions; the explicit
    gate check here just avoids building payloads we'd throw away.
    """
    # Index autofix outcomes by device so an alert can carry what was tried.
    autofix_by_device: dict[str, dict[str, Any]] = {
        af["device_id"]: af for af in result.get("autofixes", []) if "device_id" in af
    }

    # One webhook escalation per DEVICE per run: a wedged device often raises
    # several anomaly types for the same root cause (MODBUS_HANG/critical +
    # STALE_POLL/warning) and each escalation duplicates the same incident. Keep
    # only the highest-severity alert per device for the escalation seam —
    # push notifications stay per-type (their dedup_key already throttles them
    # and the recovery path clears per-type keys).
    _sev_rank = {"error": 2, "warn": 1}
    escalation_pick: dict[str, dict[str, Any]] = {}
    for alert in result.get("alerts", []):
        device_id = alert.get("device_id", "?")
        rank = _sev_rank.get(_alert_severity(alert.get("type", "UNKNOWN")), 0)
        best = escalation_pick.get(device_id)
        if best is None or rank > _sev_rank.get(
            _alert_severity(best.get("type", "UNKNOWN")), 0
        ):
            escalation_pick[device_id] = alert

    for alert in result.get("alerts", []):
        device_id = alert.get("device_id", "?")
        atype = alert.get("type", "UNKNOWN")
        # Canonicalize the receiver's dedup key through the cooldown alias: the
        # recovery path clears the canonical family key (state-key derived),
        # so a MULTI_CLIENT alert must throttle under the SAME key or its
        # receiver's dedup entry would never be cleared.
        # Display text keeps the real type.
        dedup_key = f"{device_id}:{_cooldown_type(atype)}"
        severity = _alert_severity(atype)
        if cfg.push_enabled:
            notify_push(
                f"[{atype}] {device_id}: {alert.get('detail', '')}".strip(),
                cfg,
                severity=severity,
                dedup_key=dedup_key,
            )
        if cfg.escalation_enabled and escalation_pick.get(device_id) is alert:
            # Enrich the escalation context with the autofix outcome if any.
            enriched = dict(alert)
            af = autofix_by_device.get(device_id)
            if af is not None:
                enriched["autofix"] = (
                    f"reboot attempted={af.get('attempted')} "
                    f"rebooted={af.get('rebooted')} recovered={af.get('recovered')} "
                    f"capped={af.get('capped')}"
                )
            escalate(enriched, cfg, sleep=sleep)

    # Successful auto-fixes: report transparently (the operator opted in). Failed
    # auto-fixes already surface as alerts above, so only report recovered ones.
    if cfg.push_enabled:
        for af in result.get("autofixes", []):
            if not af.get("recovered"):
                continue
            device_id = af.get("device_id", "?")
            notify_push(
                f"WiNet-Reboot bei {device_id} durchgeführt — laeuft wieder.",
                cfg,
                severity="warn",
                dedup_key=f"{device_id}:autofix",
            )

    # Recoveries: clear the receiver's dedup with the original alert's key.
    if cfg.push_enabled:
        for rec in result.get("recoveries", []):
            device_id = rec.get("device_id", "?")
            atype = rec.get("type", "UNKNOWN")
            notify_push(
                f"[{atype}] {device_id}: wieder OK (recovered).",
                cfg,
                severity="recovered",
                dedup_key=f"{device_id}:{atype}",
            )


def main(
    fetch: Callable[[str, float], dict[str, Any] | None] = fetch_json,
    post: Callable[[str, str, float], bool] = post_reboot,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
) -> int:
    """Entry point for `python -m pv_inverter_master.monitoring.anomaly_detector`.

    Returns 0 on a clean pass (including an unreachable API); 1 only on the
    detector's own unexpected crash.
    """
    _configure_logging()
    try:
        cfg = DetectorConfig.from_env()
        state = load_state(cfg.state_path)
        result = run_detection(
            cfg,
            state,
            fetch=fetch,
            post=post,
            sleep=sleep,
            now=now,
            tcp_probe=_tcp_accept_probe,
            conntrack=_conntrack_foreign_sources,
        )
        _dispatch_events(result, cfg, sleep=sleep)
        save_state(cfg.state_path, state)
        log.info(
            "detection_complete",
            reachable=result["reachable"],
            anomalies=len(result["anomalies"]),
            alerts=len(result["alerts"]),
            recoveries=len(result["recoveries"]),
            autofixes=len(result["autofixes"]),
        )
        return 0
    except Exception as e:  # noqa: BLE001 - last-resort safety net
        log.critical("anomaly_detector_crash", error=str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
