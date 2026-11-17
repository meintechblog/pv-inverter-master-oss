"""Entry point for pv-inverter-master service.

Loads YAML config, configures structured JSON logging, handles SIGTERM
for graceful shutdown. Uses DeviceRegistry for N poll loops and
AggregationLayer for SunSpec aggregation into a single virtual inverter.
Runs a health heartbeat every 5 minutes.
"""
from __future__ import annotations

import argparse
import asyncio
import signal
import sys
import time
from pathlib import Path

import aiohttp
import structlog
from aiohttp import web

from pv_inverter_master.aggregation import AggregationLayer
from pv_inverter_master.config import load_config, DEFAULT_CONFIG_PATH
from pv_inverter_master.context import AppContext
from pv_inverter_master.control import OverrideLog
from pv_inverter_master.device_registry import DeviceRegistry
from pv_inverter_master.distributor import PowerLimitDistributor
from pv_inverter_master.logging_config import configure_logging
from pv_inverter_master.proxy import run_modbus_server
from pv_inverter_master.mqtt_publisher import mqtt_publish_loop
from pv_inverter_master.influx_writer import influx_write_loop
from pv_inverter_master.releases import INSTALL_ROOT
from pv_inverter_master.updater.config import load_update_config
from pv_inverter_master.updater.github_client import GithubReleaseClient, ReleaseInfo
from pv_inverter_master.updater.scheduler import UpdateCheckScheduler
from pv_inverter_master.updater.version import (
    Version,
    get_commit_hash,
    get_current_version,
    resolve_release_commit,
)
from pv_inverter_master.webapp import broadcast_available_update, create_webapp


HEARTBEAT_INTERVAL = 300  # 5 minutes
# Bounded wait at shutdown for detached connect blocks / deferred session closes.
SHUTDOWN_WAIT_S = 15.0

HEALTHY_FLAG_PATH = Path("/run/pv-inverter-master/healthy")
LAST_BOOT_SUCCESS_MARKER_PATH = Path("/var/lib/pv-inverter-master/last-boot-success.marker")


def _write_healthy_flag_once(app_ctx, logger) -> None:
    """Write /run/pv-inverter-master/healthy and last-boot-success marker.

    Best-effort. Errors logged but never raised (we don't want to crash the
    service over a sentinel file). SAFETY-06 + SAFETY-04 companion write.

    Order of operations:
    1. Write the tmpfs /run/pv-inverter-master/healthy sentinel.
    2. Set app_ctx.healthy_flag_written so this function becomes a no-op.
    3. Write the persistent /var/lib/pv-inverter-master/last-boot-success.marker.
    4. Clear any stale PENDING marker (we succeeded post-update).

    Steps 3/4 are best-effort and do not gate step 1/2: if the tmpfs write
    succeeds but the persistent write fails, we still consider "this boot"
    healthy. The stale PENDING marker (if any) will just stick around
    harmlessly until the next successful persistent write.
    """
    if app_ctx.healthy_flag_written:
        return
    try:
        HEALTHY_FLAG_PATH.parent.mkdir(parents=True, exist_ok=True)
        HEALTHY_FLAG_PATH.touch(exist_ok=True)
        app_ctx.healthy_flag_written = True
        logger.info("healthy_flag_written", path=str(HEALTHY_FLAG_PATH))
    except OSError as e:
        logger.warning(
            "healthy_flag_write_failed",
            path=str(HEALTHY_FLAG_PATH),
            error=str(e),
        )
        return
    # Persistent last-boot-success + clear stale PENDING marker
    try:
        LAST_BOOT_SUCCESS_MARKER_PATH.parent.mkdir(parents=True, exist_ok=True)
        LAST_BOOT_SUCCESS_MARKER_PATH.touch(exist_ok=True)
        logger.info(
            "last_boot_success_marker_written",
            path=str(LAST_BOOT_SUCCESS_MARKER_PATH),
        )
        from pv_inverter_master.recovery import clear_pending_marker
        clear_pending_marker()
    except OSError as e:
        logger.warning("last_boot_success_write_failed", error=str(e))
    except Exception as e:  # pragma: no cover - belt and braces
        logger.warning("last_boot_success_unexpected_error", error=str(e))


# ---------------------------------------------------------------------------
# Phase 44: Passive Version Badge — scheduler callback + WS probe
# ---------------------------------------------------------------------------


async def _on_update_available(
    app_ctx: AppContext,
    release: "ReleaseInfo | None",
) -> None:
    """Scheduler callback: version-compare, mutate AppContext, broadcast.

    Module-level (not a closure) so tests can import it directly and drive
    it with a fake AppContext. The scheduler's per-iteration exception
    shield (CHECK-06) still wraps this call -- we do best-effort error
    handling here so the UI state stays coherent, but never raise.

    Contract:
        - Always bumps ``app_ctx.update_last_check_at`` to time.time() so
          the UI can show "last checked just now" even if the fetch was a
          no-op.
        - If ``release is None`` (fetch error / no release / prerelease
          filtered), leave ``app_ctx.available_update`` UNCHANGED. A
          transient network failure must not clear a previously-announced
          update; the scheduler's own ``last_check_failed_at`` tracks the
          failure and the next successful fetch will refresh state.
        - If ``release`` is strictly newer than the current version, set
          ``app_ctx.available_update`` to a plain-dict summary.
        - If ``release`` is same-or-older, clear ``available_update`` so
          the UI stops advertising a stale upgrade.
        - Broadcast via ``broadcast_available_update(app_ctx.webapp)`` only
          when the available_update dict actually changed (coarse-grained
          equality check).
    """
    log = structlog.get_logger(component="updater.callback")

    app_ctx.update_last_check_at = time.time()
    previous = app_ctx.available_update

    if release is not None:
        try:
            latest = Version.parse(release.tag_name)
            current_str = app_ctx.current_version or "unknown"
            if current_str == "unknown":
                # Can't compare -- defensively show the release so the UI
                # still offers an upgrade path.
                is_newer = True
            else:
                try:
                    current = Version.parse(current_str)
                except ValueError:
                    # Current version unparseable (e.g. dev build) -- defer
                    # to UI: show the release as available.
                    log.debug(
                        "update_current_version_unparseable",
                        current=current_str,
                    )
                    is_newer = True
                else:
                    is_newer = latest > current
        except ValueError as e:
            log.warning(
                "update_version_parse_failed",
                latest=release.tag_name,
                current=app_ctx.current_version,
                error=str(e),
            )
            is_newer = False

        if is_newer:
            # Resolve the concrete install target from the RELEASE TAG (not
            # origin/main): the SHA the release tag points at is exactly the
            # released commit. This avoids installing whatever happens to be
            # at origin/main, which on deployed boxes is a stale/divergent
            # mirror and previously caused a silent prod downgrade.
            #
            # Fail-closed: None (git/network unavailable, or tag unresolvable)
            # still surfaces available_update to the UI for a re-check but
            # writes NO install target. auto_update.run_auto_update_once
            # already guards `if not sha: return` (auto_update.py:87-89), so a
            # None target_sha can never produce a bad install trigger.
            latest_commit = resolve_release_commit(release.tag_name, INSTALL_ROOT)
            app_ctx.available_update = {
                "latest_version": release.tag_name,
                "tag_name": release.tag_name,
                "release_notes": release.body,
                "published_at": release.published_at,
                "html_url": release.html_url,
                "latest_commit": latest_commit,
            }
            log.info(
                "update_available",
                current=app_ctx.current_version,
                latest=release.tag_name,
            )
        else:
            app_ctx.available_update = None

    # Only broadcast when something user-visible changed.
    if app_ctx.available_update != previous and app_ctx.webapp is not None:
        try:
            await broadcast_available_update(app_ctx.webapp)
        except Exception as e:  # pragma: no cover - defensive
            log.warning("update_broadcast_failed", error=str(e))


async def _graceful_shutdown_maintenance(ctx: AppContext) -> None:
    """Plan 45-05 RESTART-02: drain + 3s grace before task cancellation.

    Called from run_with_shutdown after ``shutdown_event`` is set. If
    the shutdown was triggered by an update (maintenance_mode already
    True because update_start_handler raised the flag), this path
    drains in-flight Modbus writes with a 2s timeout, then sleeps 3s
    to guarantee at least one full Venus OS poll cycle observes the
    DEVICE_BUSY response.

    If maintenance_mode is False the shutdown is unplanned (admin kill,
    crash) and we skip the drain — no point blocking an emergency stop.
    """
    log = structlog.get_logger(component="main.shutdown")
    if not ctx.maintenance_mode:
        log.info("unplanned_shutdown_no_drain")
        return
    from pv_inverter_master.updater.maintenance import drain_inflight_modbus

    log.info("maintenance_shutdown_draining", timeout_s=2.0)
    try:
        drained = await drain_inflight_modbus(ctx, timeout_s=2.0)
        log.info("maintenance_shutdown_drain_result", drained=drained)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("maintenance_shutdown_drain_error", error=str(exc))
    # Hold the DEVICE_BUSY window open for one full Venus OS poll cycle
    # (~2s) plus safety margin. This is the RESTART-02 "at least 3s" gate.
    await asyncio.sleep(3.0)
    log.info("maintenance_shutdown_grace_complete")


def _has_active_ws_client(app_ctx: AppContext) -> bool:
    """CHECK-07: return True iff a WebSocket client is currently connected.

    Module-level for testability. The scheduler receives a zero-arg
    ``Callable[[], bool]`` so ``run_with_shutdown`` wraps this in a tiny
    closure that binds ``app_ctx``.
    """
    app = app_ctx.webapp
    if app is None:
        return False
    clients = app.get("ws_clients")
    if not clients:
        return False
    try:
        return len(clients) > 0
    except TypeError:  # pragma: no cover - defensive
        return False


def main():
    parser = argparse.ArgumentParser(description="PV-Inverter-Proxy")
    parser.add_argument(
        "--config", "-c",
        default=None,
        help="Path to config YAML (default: /etc/pv-inverter-master/config.yaml)",
    )
    args = parser.parse_args()

    # One proxy per box: a second instance would hold a second Modbus session
    # to the single-session SE30K. Refuse BEFORE touching config or sockets.
    from pv_inverter_master.instance_lock import (
        InstanceAlreadyRunning,
        InstanceLockUnavailable,
        acquire_instance_lock,
    )
    try:
        acquire_instance_lock()
    except (InstanceAlreadyRunning, InstanceLockUnavailable) as exc:
        # Unavailable lock = abort too: no fallback path, or two instances
        # could each "win" a different file and open two inverter sessions.
        print(f"pv-inverter-master: {exc}", file=sys.stderr)
        sys.exit(1)

    # Load config
    config = load_config(args.config)

    # Configure structured JSON logging
    configure_logging(config.log_level)
    log = structlog.get_logger(component="main")

    enabled_count = sum(1 for inv in config.inverters if inv.enabled)
    log.info(
        "starting",
        enabled_inverters=enabled_count,
        total_inverters=len(config.inverters),
        proxy_port=config.proxy.port,
        venus_host=config.venus.host or "(disabled)",
        log_level=config.log_level,
    )

    # Build typed application context
    app_ctx = AppContext()
    app_ctx.config = config
    app_ctx.config_path = args.config or DEFAULT_CONFIG_PATH
    # OverrideLog ring buffer: write-path filter decisions, Venus OS/webapp
    # control events, and auto-revert/lock transitions land here. Surfaced
    # via /api/dashboard.override_log. The instantiation was missing pre-
    # 2026-05, so every append() was a no-op and diagnosing the Venus OS
    # write storm required ad-hoc logging.
    app_ctx.override_log = OverrideLog(maxlen=200)

    # Plan 45-05 SAFETY-09 completion: load persisted power-limit state
    # and stash it on app_ctx for the run_with_shutdown restore hook to
    # re-issue after the distributor is ready.
    try:
        from pv_inverter_master.state_file import load_state, is_power_limit_fresh
        persisted = load_state()
        if persisted.power_limit_pct is not None:
            # 900s is a placeholder for the SE30K CommandTimeout register.
            # Phase 47 will read the real value from register 0xF100.
            fresh = is_power_limit_fresh(persisted, command_timeout_s=900.0)
            if fresh:
                app_ctx._pending_restore_limit_pct = persisted.power_limit_pct
                log.info(
                    "persisted_state_restore_scheduled",
                    power_limit_pct=persisted.power_limit_pct,
                    power_limit_set_at=persisted.power_limit_set_at,
                    age_s=(
                        time.time() - persisted.power_limit_set_at
                        if persisted.power_limit_set_at else None
                    ),
                )
            else:
                log.info(
                    "persisted_state_stale_ignored",
                    power_limit_pct=persisted.power_limit_pct,
                    age_s=(
                        time.time() - persisted.power_limit_set_at
                        if persisted.power_limit_set_at else None
                    ),
                )
        else:
            log.info("persisted_state_empty")
    except Exception as e:
        log.warning("persisted_state_load_failed", error=str(e))

    async def _health_heartbeat(ctx: AppContext):
        """Log health heartbeat every 5 minutes (per locked CONTEXT.md decision).

        Emits: poll_success_rate, cache_age, last_control_value, connection_state.
        Uses aggregated stats from all devices.
        """
        hb_log = structlog.get_logger(component="health")
        while not ctx.shutdown_event.is_set():
            try:
                await asyncio.wait_for(
                    ctx.shutdown_event.wait(), timeout=HEARTBEAT_INTERVAL
                )
                break  # shutdown requested
            except asyncio.TimeoutError:
                pass  # 5 minutes elapsed, emit heartbeat

            cache = ctx.cache
            control_state = ctx.control_state

            # Aggregate poll stats from all devices
            poll_total = sum(ds.poll_counter["total"] for ds in ctx.devices.values())
            poll_success = sum(ds.poll_counter["success"] for ds in ctx.devices.values())

            cache_age = time.monotonic() - cache.last_successful_poll if cache._has_been_updated else -1
            success_rate = (
                poll_success / poll_total * 100
                if poll_total > 0
                else 0.0
            )

            # Connection state: "connected" if any device connected
            conn_states = []
            for ds in ctx.devices.values():
                if ds.conn_mgr is not None:
                    conn_states.append(ds.conn_mgr.state.value)
            connection_state = "connected" if "connected" in conn_states else (
                conn_states[0] if conn_states else "no_devices"
            )

            hb_log.info(
                "health_heartbeat",
                poll_success_rate=round(success_rate, 1),
                poll_total=poll_total,
                cache_age=round(cache_age, 1),
                cache_stale=cache.is_stale,
                connection_state=connection_state,
                device_count=len(ctx.devices),
                last_control_value=control_state.wmaxlimpct_float if control_state.is_enabled else None,
                control_enabled=control_state.is_enabled,
            )

    async def run_with_shutdown():
        loop = asyncio.get_running_loop()

        def handle_signal(sig):
            log.info("shutdown_signal_received", signal=sig.name)
            app_ctx.shutdown_event.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, handle_signal, sig)

        # Phase 44 CHECK-01: Resolve current version + commit ONCE at
        # startup and cache on AppContext. subprocess is run once here to
        # avoid repeated forks on every /api/update/available request.
        try:
            app_ctx.current_version = get_current_version()
            app_ctx.current_commit = get_commit_hash(INSTALL_ROOT)
            log.info(
                "version_resolved",
                version=app_ctx.current_version,
                commit=app_ctx.current_commit or "unknown",
            )
        except Exception as e:
            log.warning("version_resolution_failed", error=str(e))
            app_ctx.current_version = "unknown"
            app_ctx.current_commit = None

        # Create Modbus server infrastructure (no plugin needed)
        cache, control_state, server, server_task, slave_ctx = await run_modbus_server(
            host=config.proxy.host,
            port=config.proxy.port,
            app_ctx=app_ctx,
        )

        # Create AggregationLayer
        aggregation = AggregationLayer(app_ctx, cache, config)

        # Create DeviceRegistry with aggregation callback
        registry = DeviceRegistry(app_ctx, config, on_poll_success=aggregation.recalculate)
        app_ctx.device_registry = registry

        # Create PowerLimitDistributor for multi-device power limiting
        distributor = PowerLimitDistributor(registry, config, app_ctx=app_ctx)
        slave_ctx._distributor = distributor
        app_ctx.distributor = distributor
        registry._distributor = distributor
        log.info("distributor_created", msg="PowerLimitDistributor ready for multi-device power limiting")

        # Start all enabled devices
        await registry.start_all()

        # Plan 45-05 SAFETY-09: re-issue persisted power limit (if fresh)
        # AFTER distributor + devices are ready but BEFORE we wait on the
        # shutdown event. Belt-and-braces: the legacy _LAST_LIMIT_FILE
        # path still runs inside ControlState.__init__ for UI continuity.
        restore_pct = getattr(app_ctx, "_pending_restore_limit_pct", None)
        if restore_pct is not None:
            try:
                log.info("power_limit_restore_starting", pct=restore_pct)
                control_state.update_wmaxlimpct(int(restore_pct))
                control_state.update_wmaxlim_ena(1)
                control_state.set_from_venus_os()
                control_state.save_last_limit()
                # DESIGN v8.3.1 (poll-first): do NOT write to the devices
                # yet. The 2026-06-11 SE30K wedge hit on a boot restore
                # write that was the very first request of the fresh Modbus
                # session. Defer: the first successful poll re-runs
                # distribute() with the then-current ControlState value; a
                # Venus write arriving earlier supersedes it (distribute()
                # clears the flag).
                if app_ctx.distributor is not None:
                    app_ctx.distributor.pending_restore_redistribute = True
                log.info("power_limit_restore_deferred", pct=restore_pct)
            except Exception as e:
                log.warning("power_limit_restore_failed", error=str(e))

        if registry.get_active_count() == 0:
            log.warning("no_active_inverter", msg="No enabled inverter -- Modbus server will return stale errors")
            # Keep server running but it will return stale errors via StalenessAwareSlaveContext
            # This preserves Venus OS device discovery (per Pitfall 4 from research)

        # Start webapp (pass None for plugin -- multi-device mode)
        runner = await create_webapp(app_ctx, config, app_ctx.config_path)
        app_ctx.webapp = runner.app
        runner.app["slave_ctx"] = slave_ctx  # For Virtual PV connection status
        site = web.TCPSite(runner, "0.0.0.0", config.webapp.port)
        await site.start()
        log.info("webapp_started", port=config.webapp.port)

        # Wire broadcast callback into aggregation layer (Phase 24)
        from pv_inverter_master.webapp import broadcast_device_snapshot, broadcast_virtual_snapshot

        async def _on_aggregation_broadcast(device_id: str) -> None:
            app = app_ctx.webapp
            ds = app_ctx.devices.get(device_id)
            if ds and ds.collector and ds.collector.last_snapshot:
                await broadcast_device_snapshot(app, device_id, ds.collector.last_snapshot)
            await broadcast_virtual_snapshot(app)

        aggregation._broadcast_fn = _on_aggregation_broadcast

        # Start MQTT publisher if enabled (per D-11)
        if config.mqtt_publish.enabled:
            app_ctx.mqtt_pub_queue = asyncio.Queue(maxsize=100)
            app_ctx.mqtt_pub_task = asyncio.create_task(
                mqtt_publish_loop(
                    app_ctx, config.mqtt_publish,
                    inverters=config.inverters,
                    virtual_name=config.virtual_inverter.name,
                )
            )
            log.info("mqtt_publisher_started", host=config.mqtt_publish.host, port=config.mqtt_publish.port)
        else:
            log.info("mqtt_publish_skipped", reason="mqtt_publish.enabled is false")

        # Start InfluxDB time-series logger if enabled
        if config.influx.enabled:
            app_ctx.influx_queue = asyncio.Queue(maxsize=400)
            app_ctx.influx_task = asyncio.create_task(
                influx_write_loop(app_ctx, config.influx)
            )
            log.info("influx_writer_enabled", url=config.influx.url, bucket=config.influx.bucket)
        else:
            log.info("influx_writer_skipped", reason="influx.enabled is false")

        # Start Standort coordinate MQTT subscriber (caravan tracking) if
        # at least one location is fed live via MQTT.
        if any(loc.mqtt_coords for loc in config.locations):
            from pv_inverter_master.location_mqtt import location_mqtt_loop
            app_ctx.location_mqtt_task = asyncio.create_task(
                location_mqtt_loop(app_ctx, config.mqtt_publish)
            )
            log.info("location_mqtt_started", broker=config.mqtt_publish.host)
        else:
            log.info("location_mqtt_skipped", reason="no location uses mqtt_coords")

        # Start Auto-Transfer-Switch (ATS) state subscriber. The topic
        # is configurable (PV_INVERTER_ATS_TOPIC, see ats_mqtt.py) — only
        # installs with a transfer switch publish it. On other installs the
        # subscription simply never fires and ats_state stays None.
        from pv_inverter_master.ats_mqtt import ats_mqtt_loop
        app_ctx.ats_mqtt_task = asyncio.create_task(
            ats_mqtt_loop(app_ctx, config.mqtt_publish)
        )
        log.info("ats_mqtt_started", broker=config.mqtt_publish.host)

        # Start Venus OS MQTT reader only if host is configured
        if config.venus.host:
            from pv_inverter_master.venus_reader import venus_mqtt_loop
            venus_task = asyncio.create_task(
                venus_mqtt_loop(app_ctx, config.venus.host, config.venus.port, config.venus.portal_id)
            )
            app_ctx.venus_task = venus_task
        else:
            log.info("venus_mqtt_skipped", reason="no venus.host in config")
            app_ctx.venus_mqtt_connected = False

        # Periodic device list broadcast (keeps sidebar + MQTT stats live)
        from pv_inverter_master.webapp import broadcast_device_list, broadcast_location_list

        async def _device_list_refresh(ctx: AppContext):
            while not ctx.shutdown_event.is_set():
                try:
                    await asyncio.wait_for(ctx.shutdown_event.wait(), timeout=5.0)
                    break
                except asyncio.TimeoutError:
                    pass
                app = ctx.webapp
                if app is not None:
                    await broadcast_device_list(app)
                    await broadcast_location_list(app)

        device_list_task = asyncio.create_task(_device_list_refresh(app_ctx))

        async def _healthy_flag_watcher(ctx: AppContext):
            """Write /run/pv-inverter-master/healthy on first successful poll.

            Polls every 500ms for the first device to register a successful poll,
            then writes the healthy flag (and the persistent last-boot-success
            marker) exactly once, and exits. If shutdown fires before any poll
            succeeds, the task exits without writing anything.
            """
            watcher_log = structlog.get_logger(component="healthy_flag")
            while not ctx.shutdown_event.is_set():
                try:
                    await asyncio.wait_for(ctx.shutdown_event.wait(), timeout=0.5)
                    return  # shutdown before first healthy poll
                except asyncio.TimeoutError:
                    pass
                if any(
                    ds.poll_counter["success"] > 0 for ds in ctx.devices.values()
                ):
                    _write_healthy_flag_once(ctx, watcher_log)
                    return

        healthy_flag_task = asyncio.create_task(_healthy_flag_watcher(app_ctx))

        # Periodic redistribute so slack reallocation tracks changing
        # production (e.g. small inverters ramping up/down with cloud
        # cover) while the user-chosen ceiling stays exact.
        if app_ctx.distributor is not None:
            slack_refresh_task = asyncio.create_task(
                app_ctx.distributor.slack_refresh_loop(interval_s=5.0),
                name="distributor-slack-refresh",
            )
        else:
            slack_refresh_task = None

        # Start health heartbeat task
        heartbeat_task = asyncio.create_task(_health_heartbeat(app_ctx))

        # Phase 44 CHECK-02/03/07: Start GitHub update check scheduler.
        # Single shared aiohttp.ClientSession — do not create one per
        # request. The session lives for the whole process and is closed
        # in the graceful-shutdown block below.
        #
        # Skipped entirely when the in-app updater is disabled
        # (updater.enabled = false) — the case for rsync-deployed hosts
        # where a git-based update would diverge from what is running.
        update_http_session = None
        update_scheduler_task = None
        auto_update_task = None
        if load_update_config(app_ctx.config_path).enabled:
            update_http_session = aiohttp.ClientSession(
                headers={
                    "User-Agent": (
                        "pv-inverter-master/8.0 "
                        "(github.com/meintechblog/pv-inverter-master-oss)"
                    )
                }
            )
            update_github_client = GithubReleaseClient(session=update_http_session)

            async def _update_cb(release):
                await _on_update_available(app_ctx, release)

            def _update_active_probe() -> bool:
                return _has_active_ws_client(app_ctx)

            update_scheduler = UpdateCheckScheduler(
                github_client=update_github_client,
                on_update_available=_update_cb,
                has_active_websocket_client=_update_active_probe,
            )
            # Expose the scheduler to the webapp so POST /api/update/check
            # (the "Jetzt prüfen" button) can force an immediate poll —
            # without this the handler returns scheduler_not_running.
            app_ctx.webapp["update_scheduler"] = update_scheduler
            update_scheduler_task = update_scheduler.start()
            log.info("update_scheduler_started")

            # Nightly unattended auto-update (04:00 local). Fires the same
            # install trigger as the manual button when auto_install is on.
            from pv_inverter_master.updater.auto_update import nightly_auto_update_loop

            auto_update_task = asyncio.create_task(
                nightly_auto_update_loop(
                    app_ctx, app_ctx.config_path, scheduler=update_scheduler
                )
            )
            log.info("auto_update_loop_scheduled")
        else:
            log.info("update_scheduler_skipped", reason="updater disabled in config")

        # Wait for shutdown signal
        await app_ctx.shutdown_event.wait()

        # Plan 45-05 RESTART-02: drain in-flight Modbus writes + hold
        # the DEVICE_BUSY window open for one Venus OS poll cycle.
        await _graceful_shutdown_maintenance(app_ctx)

        log.info("graceful_shutdown_starting")

        # Cancel periodic tasks (Phase 44: include update_scheduler_task)
        cancellable_tasks = [
            heartbeat_task,
            device_list_task,
            healthy_flag_task,
        ]
        if update_scheduler_task is not None:
            cancellable_tasks.append(update_scheduler_task)
        if auto_update_task is not None:
            cancellable_tasks.append(auto_update_task)
        if slack_refresh_task is not None:
            cancellable_tasks.append(slack_refresh_task)
        for task in cancellable_tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Phase 44: close the shared aiohttp session used by the update
        # scheduler. Best-effort; failures here must not block shutdown.
        if update_http_session is not None:
            try:
                await update_http_session.close()
                log.info("update_http_session_closed")
            except Exception as e:  # pragma: no cover - defensive
                log.warning("update_http_session_close_failed", error=str(e))

        # Stop MQTT publisher
        if app_ctx.mqtt_pub_task is not None:
            app_ctx.mqtt_pub_task.cancel()
            try:
                await app_ctx.mqtt_pub_task
            except asyncio.CancelledError:
                pass
            log.info("mqtt_publisher_stopped")

        # Stop InfluxDB logger
        if app_ctx.influx_task is not None:
            app_ctx.influx_task.cancel()
            try:
                await app_ctx.influx_task
            except asyncio.CancelledError:
                pass
            log.info("influx_writer_stopped")

        # Stop Standort coordinate subscriber
        if app_ctx.location_mqtt_task is not None:
            app_ctx.location_mqtt_task.cancel()
            try:
                await app_ctx.location_mqtt_task
            except asyncio.CancelledError:
                pass
            log.info("location_mqtt_stopped")

        # Stop ATS subscriber
        if app_ctx.ats_mqtt_task is not None:
            app_ctx.ats_mqtt_task.cancel()
            try:
                await app_ctx.ats_mqtt_task
            except asyncio.CancelledError:
                pass
            log.info("ats_mqtt_stopped")

        # Stop webapp
        if runner is not None:
            await runner.cleanup()
            log.info("webapp_stopped")

        # Stop all device poll loops
        await registry.stop_all()
        log.info("devices_stopped")

        # Detached connect+DPC blocks and deferred session closes must finish
        # (bounded) before the loop ends, or exit would cut a transaction.
        from pv_inverter_master.device_registry import wait_connect_tasks
        from pv_inverter_master.modbus_gateway import shutdown_gateways
        await wait_connect_tasks(SHUTDOWN_WAIT_S)
        await shutdown_gateways(SHUTDOWN_WAIT_S)
        log.info("gateways_closed")

        # Cancel Modbus server
        server_task.cancel()
        try:
            await server_task
        except asyncio.CancelledError:
            pass

        log.info("shutdown_complete")

    try:
        asyncio.run(run_with_shutdown())
    except KeyboardInterrupt:
        pass

    sys.exit(0)


if __name__ == "__main__":
    main()
