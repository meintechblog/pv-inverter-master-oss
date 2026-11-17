"""Phasen-Detective — automatic phase-assignment detection for single-phase
inverters by toggling their power and measuring which grid phase responds.

Algorithm (v2 — single-throttle + multi-window robust)
------------------------------------------------------
The earlier v1 algorithm threw 3 throttle/restore cycles at the inverter and
voted across cycles. That fights both the Hoymiles HMS EEPROM cooldown (300 s
declared by the plugin) and Multiplus rebalancing noise. v2 takes a single
throttle event and reads the same throttled state across multiple measurement
windows — voting in time, not in wear cycles:

  1. Baseline grid + inverter (3 s average).
  2. Throttle once to 0 %; bypass the plugin's own dead-time gate (force=True).
  3. Wait for the inverter to actually ramp down (poll ac_power_w every 1 s,
     target < 10 % of baseline). Capture ramp_down_time_s — this is what
     the throttle-score *should* reflect, and we feed it back into the
     distributor's ``DeviceLimitState.measured_response_time_s`` so the
     score becomes truthful instead of statically declared.
  4. Take N=3 measurement windows of the throttled state, 2 s gap between
     them, each window 6 × 0.5 s = 3 s averaged. Compute Δ per phase per
     window.
  5. Per-phase median Δ across windows. Winner = phase with max median.
  6. Acceptance: winner_median ≥ max(40 W absolute, 8 % of baseline_inv)
     AND winner_median ≥ 2 × second-best (clear dominance).
  7. Restore inverter; release reservation.

The atomic device reservation (``app_ctx.reserve_device``) makes the
distributor skip its 5 s WMaxLimPct keepalive while we own the device.

Endpoints
---------
- ``POST /api/devices/<id>/detect-phase`` — start detection (returns 202).
- ``GET  /api/devices/<id>/detect-phase`` — poll current state.
"""
from __future__ import annotations

import asyncio
import statistics
import time
from typing import Any

import structlog
from aiohttp import web

from pv_inverter_master.plugin import get_throttle_caps, locked_write_power_limit

log = structlog.get_logger("phase_detect")

# In-memory task state, keyed by device_id.
_TASKS: dict[str, dict[str, Any]] = {}

# Algorithm tuning knobs.
_BASELINE_SAMPLES = 6              # × _SAMPLE_INTERVAL = ~3 s averaging window
_SAMPLE_INTERVAL = 0.5
_THROTTLE_WAIT_MAX_S = 20.0        # how long we wait for the inverter to fall
_THROTTLE_DONE_FRACTION = 0.10     # ramp-down considered complete when ac_power_w < 10% of baseline
# Minimum inverter power for meaningful detection. Scaled per-device:
# Shelly type runs binary on/off so even small balcony panels (100-200 W)
# produce a clean Δ when switched off — keep its floor low. For
# proportional plugins we want enough headroom that Multiplus
# rebalancing noise doesn't drown the signal.
_MIN_INVERTER_W_SHELLY = 50
_MIN_INVERTER_W_PROPORTIONAL_DEFAULT = 200
_MIN_INVERTER_W_PROPORTIONAL_FLOOR = 100


def _min_inverter_w(entry) -> float:
    """Per-entry detection-minimum power threshold."""
    if entry.type == "shelly":
        return float(_MIN_INVERTER_W_SHELLY)
    # Reference power = max of rated_power and panel_power_wp (whichever
    # was filled in by the user). When both are 0 we can't scale.
    ref = max(getattr(entry, "rated_power", 0) or 0,
              getattr(entry, "panel_power_wp", 0) or 0)
    if ref <= 0:
        # No rated/panel-Wp known. OpenDTU in particular is commonly
        # used for balcony-class inverters where panel power is in the
        # 100-300 W range, so the conservative 200 W default would
        # lock out the very devices it's meant to serve. Drop to the
        # Shelly floor (the verdict-side 30 W absolute residual +
        # 2× dominance check still rejects pure noise).
        if entry.type == "opendtu":
            return float(_MIN_INVERTER_W_SHELLY)
        return float(_MIN_INVERTER_W_PROPORTIONAL_DEFAULT)
    return max(
        float(_MIN_INVERTER_W_PROPORTIONAL_FLOOR),
        min(float(_MIN_INVERTER_W_PROPORTIONAL_DEFAULT), 0.5 * ref),
    )
_HOYMILES_FIXED_WAIT_S = 8.0       # Hoymiles: skip poll-based ramp wait (cache too slow), fixed wait sized to catch transient before Multiplus rebalances
_MEASUREMENT_WINDOWS = 5           # post-throttle measurement windows — more = finer time resolution to catch transients
_WINDOW_SAMPLES = 3                # samples per window — shorter window = better time resolution
_GAP_BETWEEN_WINDOWS_S = 1.0       # tighter gaps to catch fast transients
_DELTA_ABS_FLOOR_W = 30.0          # winner must be at least this many W above common-mode
_DELTA_BASELINE_FRACTION = 0.05    # OR this fraction of baseline_inverter — whichever larger
_DELTA_DOMINANCE_RATIO = 2.0       # winner must be ≥ ratio × second-best to count as clear


def _now_log_entry(msg: str, level: str = "info") -> dict:
    return {"ts": time.time(), "level": level, "msg": msg}


def _read_grid(app_ctx) -> tuple[float, float, float]:
    # Read venus_settings fresh on every call: venus_reader rebinds
    # ``app_ctx.venus_settings = dict(state)`` per recv-cycle, so any
    # captured reference goes stale and per-phase power freezes at the
    # value it had when the reference was taken.
    vs = app_ctx.venus_settings or {}
    return (
        float(vs.get("grid_l1_w") or 0),
        float(vs.get("grid_l2_w") or 0),
        float(vs.get("grid_l3_w") or 0),
    )


def _read_inverter_power(ds) -> float:
    poll = ds.last_poll_data
    if not poll:
        return 0.0
    # Decode AC power from Model 103 register block (offset 14, SF=0).
    regs = poll.get("inverter_registers") or []
    if len(regs) > 14:
        return float(regs[14])
    return 0.0


async def _measure_grid_avg(app_ctx, samples: int, interval: float) -> tuple[float, float, float]:
    acc = [0.0, 0.0, 0.0]
    for _ in range(samples):
        g = _read_grid(app_ctx)
        for i in range(3):
            acc[i] += g[i]
        await asyncio.sleep(interval)
    return (acc[0] / samples, acc[1] / samples, acc[2] / samples)


def _seconds_since_last_limit(plugin) -> float | None:
    """Return seconds since the plugin last wrote a power limit, or None
    if the plugin doesn't track this (e.g. Shelly which is no-op)."""
    last_ts = getattr(plugin, "_last_limit_ts", None)
    if not last_ts:
        return None
    return time.monotonic() - last_ts


def _cooldown_remaining_s(plugin) -> float:
    """Seconds the plugin still needs to wait before another power-limit
    write is safe (per declared ThrottleCaps.cooldown_s). 0 if ready."""
    caps = get_throttle_caps(plugin)
    if caps is None or caps.cooldown_s <= 0:
        return 0.0
    since = _seconds_since_last_limit(plugin)
    if since is None:
        return 0.0
    remaining = caps.cooldown_s - since
    return max(0.0, remaining)


def _check_preconditions(app_ctx, ds, entry) -> dict[str, dict]:
    """Return per-check status dict. Each entry has ok=bool + label."""
    vs = app_ctx.venus_settings or {}
    peers = vs.get("peers", {}) or {}
    # venus_settings is replaced as a whole dict per recv-cycle, so reading
    # it fresh here (not caching across calls) is required for live state.
    grid_bucket = peers.get("grid_meter") or {}
    grid_topics_live = (
        ("grid_l1_w" in vs or vs.get("grid_l1_w") is not None)
        and ("grid_l2_w" in vs or vs.get("grid_l2_w") is not None)
        and ("grid_l3_w" in vs or vs.get("grid_l3_w") is not None)
    )
    grid_ok = bool(grid_bucket) and grid_topics_live

    inverter_w = _read_inverter_power(ds)
    min_w = _min_inverter_w(entry)
    pv_ok = inverter_w >= min_w

    conn_mgr = getattr(ds, "conn_mgr", None)
    reachable = (getattr(conn_mgr, "state", None).value == "connected") if conn_mgr else False
    throttle_ok = bool(getattr(entry, "throttle_enabled", True))

    # Plugin support: write_power_limit must be the real proportional path,
    # OR the plugin must expose ``switch()`` for binary (Shelly). Shelly's
    # write_power_limit is a no-op that returns success — using it for
    # phase detection gives meaningless null-Δ results.
    has_proportional = bool(ds.plugin) and hasattr(ds.plugin, "write_power_limit")
    has_binary = bool(ds.plugin) and hasattr(ds.plugin, "switch")
    if entry.type == "shelly":
        plugin_ok = has_binary
        plugin_label = "Plugin unterstützt switch() (binary)"
    else:
        plugin_ok = has_proportional
        plugin_label = "Plugin unterstützt write_power_limit"

    # Cooldown check: prevents back-to-back detection runs from hitting
    # hardware that declares a multi-minute cooldown (Hoymiles EEPROM,
    # Shelly relay wear). Skipping this gate was the v1 source of
    # silently-failing Cycle 2/3 throttles.
    cooldown_remaining = _cooldown_remaining_s(ds.plugin) if ds and ds.plugin else 0.0
    cooldown_ok = cooldown_remaining <= 0
    if cooldown_remaining > 0:
        cooldown_label = (
            f"Plugin-Cooldown läuft noch {int(cooldown_remaining)} s "
            "(Hardware-Schonung)"
        )
    else:
        cooldown_label = "Plugin-Cooldown abgelaufen"

    return {
        "grid_meter_present": {"ok": grid_ok, "label": "Grid-Meter erkannt"},
        "pv_producing_enough": {
            "ok": pv_ok,
            "label": f"PV produziert ≥ {int(min_w)} W (aktuell {int(inverter_w)} W)",
        },
        "device_reachable": {"ok": reachable, "label": "Inverter verbunden"},
        "throttle_enabled": {"ok": throttle_ok, "label": "Drossel-Befehle erlaubt"},
        "plugin_supports_throttle": {"ok": plugin_ok, "label": plugin_label},
        "cooldown_ready": {"ok": cooldown_ok, "label": cooldown_label},
    }


def _persist_measured_response_time(app_ctx, device_id: str, ramp_down_time_s: float) -> bool:
    """Write the measured ramp-down time into the distributor's
    ``DeviceLimitState.measured_response_time_s`` so the throttle-score
    becomes truthful (live measurement overrides the plugin's statically
    declared response_time_s in ``_effective_score``)."""
    distributor = getattr(app_ctx, "distributor", None)
    if distributor is None:
        return False
    states = getattr(distributor, "_device_states", None) or {}
    ds_limit = states.get(device_id)
    if ds_limit is None:
        return False
    ds_limit.measured_response_time_s = ramp_down_time_s
    return True


def _evaluate_winner(median_deltas: list[float], baseline_inv: float) -> dict:
    """Pick the winning phase and decide whether the evidence is clean
    enough to call it. Returns a structured verdict for the log + API.

    Background processes (Multiplus battery rebalancing, parallel PV
    ramps, household load drift) shift ALL three phases together over
    the ~25 s of a probe. Subtract the per-probe **common-mode** (the
    median of the three Δs) so we measure the differential effect of
    the throttled inverter, not the household's noise floor. The
    winner is the phase with the highest residual above the median.
    """
    common_mode = statistics.median(median_deltas)
    residuals = [d - common_mode for d in median_deltas]

    winner_idx = max(range(3), key=lambda i: residuals[i])
    winner_residual = residuals[winner_idx]
    others = sorted(
        (residuals[i] for i in range(3) if i != winner_idx),
        reverse=True,
    )
    second_best = others[0] if others else 0.0

    abs_threshold = max(_DELTA_ABS_FLOOR_W, baseline_inv * _DELTA_BASELINE_FRACTION)
    floor_ok = winner_residual >= abs_threshold
    # When the second-best residual is at-or-below zero noise, dominance
    # is trivially satisfied (winner is the only positive residual).
    if second_best <= 0:
        dominance_ok = winner_residual > 0
        dominance_ratio = float("inf") if winner_residual > 0 else 0.0
    else:
        dominance_ratio = winner_residual / second_best
        dominance_ok = dominance_ratio >= _DELTA_DOMINANCE_RATIO

    accepted = floor_ok and dominance_ok
    return {
        "winner_phase": (winner_idx + 1) if accepted else None,
        "winner_idx": winner_idx,
        "winner_residual_w": winner_residual,
        "winner_raw_delta_w": median_deltas[winner_idx],
        "second_best_residual_w": second_best,
        "common_mode_w": common_mode,
        "residuals": residuals,
        "abs_threshold_w": abs_threshold,
        "dominance_ratio": dominance_ratio,
        "floor_ok": floor_ok,
        "dominance_ok": dominance_ok,
        "accepted": accepted,
    }


async def _throttle(ds, entry, log_step) -> None:
    """Issue the throttle command appropriate for the inverter type."""
    if entry.type == "shelly":
        log_step("→ Inverter ausschalten …")
        await ds.plugin.switch(False)
    else:
        log_step("→ Drossel auf 0 % senden …")
        # Choke point: hold the device's poll lock so this
        # write cannot race stop_device's drain / the poll loop.
        await locked_write_power_limit(
            ds.plugin, True, 0.0,
            force=True, poll_lock=getattr(ds, "poll_lock", None),
        )


async def _restore(ds, entry, log_step) -> None:
    """Issue the restore command appropriate for the inverter type."""
    try:
        if entry.type == "shelly":
            await ds.plugin.switch(True)
        else:
            await locked_write_power_limit(
                ds.plugin, False, 0.0,
                force=True, poll_lock=getattr(ds, "poll_lock", None),
            )
    except Exception as exc:
        log_step(f"WARN: restore failed: {exc}", "warn")


async def _wait_for_rampdown(ds, entry, baseline_inv: float, log_step) -> float | None:
    """Wait for the inverter to actually ramp down. Returns elapsed
    seconds if observed, else None. For Hoymiles the poll cache is too
    slow so we just do a fixed wait sized to land inside the post-
    throttle Multiplus-rebalance transient."""
    if entry.type == "hoymiles":
        log_step(
            f"  Hoymiles: feste Wartezeit {_HOYMILES_FIXED_WAIT_S:.0f} s "
            "(Polling-Cache zu langsam; gezielt früh genug damit Transient vor Multiplus-Rebalancing erwischt wird)"
        )
        await asyncio.sleep(_HOYMILES_FIXED_WAIT_S)
        return None

    target_w = max(20.0, baseline_inv * _THROTTLE_DONE_FRACTION)
    log_step(f"  Warte bis Inverter unter {target_w:.0f} W abgefallen ist …")
    ramp_start = time.monotonic()
    elapsed = 0.0
    while elapsed < _THROTTLE_WAIT_MAX_S:
        await asyncio.sleep(1.0)
        elapsed = time.monotonic() - ramp_start
        cur = _read_inverter_power(ds)
        if cur < target_w:
            log_step(f"  Inverter bei {cur:.0f} W nach {elapsed:.1f} s")
            return elapsed
    cur = _read_inverter_power(ds)
    log_step(
        f"  FEHLER: Inverter nach {_THROTTLE_WAIT_MAX_S:.0f} s noch bei {cur:.0f} W — "
        "Drossel hat nicht gegriffen, Δ-Messung wäre Müll",
        "error",
    )
    return None


async def _run_one_probe(app_ctx, device_id: str, ds, entry, run_idx: int, total_runs: int, log_step) -> dict:
    """Run one detection probe: throttle → wait → measure → verdict → restore.

    Returns a dict with all the data the caller needs to aggregate
    across multiple probes. Restoring at the end of each probe is
    important so the inverter doesn't sit at 0 % while the cooldown
    elapses between probes.
    """
    run_label = f"Run {run_idx + 1}/{total_runs}" if total_runs > 1 else ""
    if run_label:
        log_step(f"=== {run_label} ===")

    baseline_grid = await _measure_grid_avg(app_ctx, _BASELINE_SAMPLES, _SAMPLE_INTERVAL)
    baseline_inv = _read_inverter_power(ds)
    log_step(
        f"  Baseline Grid: L1={baseline_grid[0]:+.0f} W, "
        f"L2={baseline_grid[1]:+.0f} W, L3={baseline_grid[2]:+.0f} W"
    )
    log_step(f"  Inverter produziert: {baseline_inv:.0f} W")

    min_inv_w = _min_inverter_w(entry)
    if baseline_inv < min_inv_w:
        return {
            "ok": False,
            "error": f"PV-Output unter {int(min_inv_w)} W ({baseline_inv:.0f} W)",
            "baseline_inverter_w": baseline_inv,
        }

    try:
        await _throttle(ds, entry, log_step)
    except Exception as exc:
        return {
            "ok": False,
            "error": f"throttle command failed: {exc}",
            "baseline_inverter_w": baseline_inv,
        }

    ramp_down_time_s = await _wait_for_rampdown(ds, entry, baseline_inv, log_step)

    # Non-Hoymiles plugins use poll-based rampdown detection — if it
    # didn't fire, the hardware ignored our throttle (e.g. cooldown
    # silent-reject) and the Δ we'd measure is just noise.
    if entry.type != "hoymiles" and ramp_down_time_s is None:
        await _restore(ds, entry, log_step)
        return {
            "ok": False,
            "error": "Drossel-Befehl unwirksam (Inverter nicht abgerampt)",
            "baseline_inverter_w": baseline_inv,
        }

    # Persist response time once — first successful probe is enough.
    if ramp_down_time_s is not None:
        if _persist_measured_response_time(app_ctx, device_id, ramp_down_time_s):
            log_step(
                f"  Throttle-Score aktualisiert: response_time={ramp_down_time_s:.1f} s gespeichert"
            )

    # Multi-window measurement during the throttle event. The Multiplus
    # 3-phase rebalancer chases within ~10 s, so the inverter's signal
    # exists only as a transient peak in the first few windows. Short
    # windows + per-phase max-of-windows catches that transient where
    # a median would smooth it away.
    window_deltas: list[list[float]] = []
    window_grids: list[tuple[float, float, float]] = []
    for w in range(_MEASUREMENT_WINDOWS):
        log_step(f"  Messfenster {w + 1}/{_MEASUREMENT_WINDOWS} …")
        after = await _measure_grid_avg(app_ctx, _WINDOW_SAMPLES, _SAMPLE_INTERVAL)
        d = [after[i] - baseline_grid[i] for i in range(3)]
        window_grids.append(after)
        window_deltas.append(d)
        log_step(
            f"    Grid: L1={after[0]:+.0f} W, L2={after[1]:+.0f} W, L3={after[2]:+.0f} W; "
            f"Δ: L1={d[0]:+.0f}, L2={d[1]:+.0f}, L3={d[2]:+.0f}"
        )
        if w < _MEASUREMENT_WINDOWS - 1:
            await asyncio.sleep(_GAP_BETWEEN_WINDOWS_S)

    peaks = [max(w[i] for w in window_deltas) for i in range(3)]
    log_step(
        f"  Peak Δ: L1={peaks[0]:+.0f} W, L2={peaks[1]:+.0f} W, L3={peaks[2]:+.0f} W"
    )

    verdict = _evaluate_winner(peaks, baseline_inv)
    log_step(
        f"  Common-Mode: {verdict['common_mode_w']:+.0f} W, "
        f"Residuen: L1={verdict['residuals'][0]:+.0f}, L2={verdict['residuals'][1]:+.0f}, L3={verdict['residuals'][2]:+.0f}"
    )
    if verdict["winner_phase"] is not None:
        log_step(
            f"  → L{verdict['winner_phase']} "
            f"(Residual {verdict['winner_residual_w']:.0f} W, "
            f"Dominanz {verdict['dominance_ratio']:.1f}×)"
        )
    else:
        log_step("  → kein klarer Sieger", "warn")

    # Restore between probes so the inverter doesn't sit at 0% during
    # the cooldown gap before the next probe (or the final wrap-up).
    await _restore(ds, entry, log_step)

    return {
        "ok": True,
        "baseline_grid": baseline_grid,
        "baseline_inverter_w": baseline_inv,
        "ramp_down_time_s": ramp_down_time_s,
        "windows": [{"grid": g, "deltas": d} for g, d in zip(window_grids, window_deltas)],
        "peaks": peaks,
        "verdict": verdict,
        "winner_phase": verdict["winner_phase"],
    }


def _decide_run_count(entry, caps, phase_explicit: bool) -> tuple[int, float]:
    """Pick (N runs, gap_between_runs_s) for this device + mode.

    - Initial detection (phase_explicit=False): N=1, no gap. Today's
      behaviour. Auto-apply takes effect if accepted.
    - Verification (phase_explicit=True): N=3, gap respects the plugin's
      declared cooldown so the hardware actually accepts each probe.
      Shelly's 300 s cooldown is a relay-wear *advisory* (not silent-
      rejection), so we cap its verify-gap at 30 s — three switch
      cycles on a relay rated for 100k is negligible.
    """
    if not phase_explicit:
        return 1, 0.0
    if entry.type == "shelly":
        return 3, 30.0
    cooldown = caps.cooldown_s if caps and caps.cooldown_s > 0 else 30.0
    return 3, cooldown


def _aggregate_consensus(probes: list[dict]) -> dict:
    """Across N probes, count winner-phase votes and decide consensus.

    Returns:
      consensus_phase: int | None — the phase ≥ majority of probes agreed on
      consensus_count: int — how many probes voted for the consensus
      consensus_strong: bool — True if all N agree, False if it was a majority
    """
    from collections import Counter
    counter = Counter(p["winner_phase"] for p in probes if p.get("ok") and p.get("winner_phase"))
    if not counter:
        return {"consensus_phase": None, "consensus_count": 0, "consensus_strong": False}
    top_phase, top_count = counter.most_common(1)[0]
    n_with_winner = sum(1 for p in probes if p.get("ok") and p.get("winner_phase"))
    # Majority threshold: > half the *successful* probes. For N=3 that's
    # >= 2. For N=1 trivially 1. For all-equal results, consensus_strong.
    needs = max(1, (n_with_winner + 1) // 2)
    consensus = top_phase if top_count >= needs else None
    return {
        "consensus_phase": consensus,
        "consensus_count": top_count,
        "consensus_strong": top_count == len(probes),
    }


async def _run_detection(app_ctx, device_id: str, ds, entry, config=None, config_path: str = "") -> None:
    state = _TASKS[device_id]

    def log_step(msg: str, level: str = "info") -> None:
        state["logs"].append(_now_log_entry(msg, level))
        if len(state["logs"]) > 200:
            state["logs"] = state["logs"][-200:]
        log.info("phase_detect", device_id=device_id, msg=msg)

    if not app_ctx.reserve_device(device_id):
        state["status"] = "error"
        state["error"] = "device already reserved by another operation"
        log_step("FEHLER: Gerät bereits reserviert", "error")
        return
    log_step("Distributor reserviert dieses Gerät für die Erkennung")

    # Snapshot ATS state at probe start so the UI can later flag a
    # topology change ("you detected this in grid-mode, you're now
    # in island-mode — re-verify").
    ats_state_at_start = app_ctx.ats_state

    # Decide mode: quick (1 run) vs verify (N runs with cooldown gap).
    caps = get_throttle_caps(ds.plugin) if ds.plugin else None
    n_runs, gap_s = _decide_run_count(entry, caps, entry.phase_explicit)
    state["total_runs"] = n_runs
    mode_label = "Verifikation (Konsens aus mehreren Runs)" if n_runs > 1 else "Erst-Erkennung (1 Run)"
    log_step(f"Modus: {mode_label}")
    if n_runs > 1:
        total_min = ((n_runs - 1) * gap_s + n_runs * 30) / 60
        log_step(
            f"  {n_runs} Runs × ~30 s + {n_runs - 1} Cooldown-Pausen à {gap_s:.0f} s "
            f"(~{total_min:.0f} min gesamt)"
        )

    try:
        probes: list[dict] = []
        for run_idx in range(n_runs):
            state["current_run"] = run_idx + 1
            state["current_step"] = f"probe_{run_idx + 1}"
            probe = await _run_one_probe(app_ctx, device_id, ds, entry, run_idx, n_runs, log_step)
            probes.append(probe)
            if not probe.get("ok"):
                log_step(f"  Run {run_idx + 1} fehlgeschlagen: {probe.get('error')}", "warn")
                # If the first probe of a verify-run fails (e.g. cooldown
                # still active from a previous session), aborting saves the
                # user 10+ min of waiting on more guaranteed-failing probes.
                if run_idx == 0:
                    state["status"] = "error"
                    state["error"] = probe.get("error", "Probe fehlgeschlagen")
                    return
            if run_idx < n_runs - 1:
                state["current_step"] = "cooldown_gap"
                log_step(f"  Cooldown-Pause {gap_s:.0f} s vor nächstem Run …")
                await asyncio.sleep(gap_s)

        # Aggregate across all probes.
        state["current_step"] = "aggregating"
        consensus = _aggregate_consensus(probes)
        winner_phase = consensus["consensus_phase"]
        n_successful = sum(1 for p in probes if p.get("ok"))
        log_step(
            f"Konsens-Auswertung: {n_successful}/{n_runs} Runs erfolgreich, "
            f"Sieger-Stimmen: {consensus['consensus_count']}/{n_successful}, "
            f"Konsens-Phase: {'L' + str(winner_phase) if winner_phase else 'keiner'}"
        )

        # Auto-apply policy — same as before, but now consensus-gated:
        #   - First-time detection (phase_explicit=False): apply if any
        #     winner found (N=1, so consensus == winner)
        #   - Verification (phase_explicit=True): never auto-apply;
        #     surface mismatch with manual override button
        applied_to_config = False
        verify_mismatch: int | None = None
        if winner_phase is not None and config is not None and config_path:
            if entry.phase_explicit:
                if entry.phase != winner_phase:
                    verify_mismatch = winner_phase
                    log_step(
                        f"  Verifikation: erkannt L{winner_phase}, gespeichert L{entry.phase} "
                        "— Auto-Apply übersprungen (manuelle Bestätigung nötig)",
                        "warn",
                    )
                else:
                    log_step(
                        f"  Verifikation OK: erkannt L{winner_phase} stimmt mit gespeicherter Phase überein"
                    )
            else:
                entry.phase = winner_phase
                entry.phase_explicit = True
                try:
                    from pv_inverter_master.config import save_config
                    save_config(config_path, config)
                    applied_to_config = True
                    log_step(
                        f"  Phase L{winner_phase} automatisch in Konfiguration übernommen "
                        "(phase_explicit=True)"
                    )
                except Exception as exc:
                    log_step(f"  WARN: auto-apply save failed: {exc}", "warn")

        # Use the first successful probe for the "primary" display fields
        # (baseline, peaks, verdict). The probes[] array carries all runs
        # for the UI to render the full picture.
        first_ok = next((p for p in probes if p.get("ok")), None)
        state["result"] = {
            "detected_phase": winner_phase,
            "confidence": int(100 * consensus["consensus_count"] / max(1, n_successful)) if winner_phase else 0,
            "consensus_phase": consensus["consensus_phase"],
            "consensus_count": consensus["consensus_count"],
            "consensus_strong": consensus["consensus_strong"],
            "n_runs": n_runs,
            "n_successful": n_successful,
            "mode": "verify" if entry.phase_explicit else "initial",
            "ats_state_at_run": ats_state_at_start,
            "probes": probes,
            # Backwards-compat fields for the UI: surface first probe's
            # primary signals at the top level too.
            "baseline_grid": first_ok["baseline_grid"] if first_ok else None,
            "baseline_inverter_w": first_ok["baseline_inverter_w"] if first_ok else None,
            "ramp_down_time_s": first_ok.get("ramp_down_time_s") if first_ok else None,
            "windows": first_ok["windows"] if first_ok else [],
            "medians": first_ok["peaks"] if first_ok else [],
            "verdict": first_ok["verdict"] if first_ok else {},
            "applied_to_config": applied_to_config,
            "verify_mismatch_phase": verify_mismatch,
            "stored_phase_before_run": entry.phase if winner_phase is not None else None,
        }
        state["status"] = "complete"
    except Exception as exc:
        log_step(f"INTERNAL ERROR: {exc}", "error")
        state["status"] = "error"
        state["error"] = str(exc)
    finally:
        # Final restore (defensive — _run_one_probe also restores at its
        # tail, but if we crashed mid-probe the device might still be
        # parked).
        state["current_step"] = "restoring"
        await _restore(ds, entry, log_step)
        app_ctx.release_device(device_id)
        log_step("Reservierung freigegeben — Distributor wieder aktiv")


async def phase_detect_start_handler(request: web.Request) -> web.Response:
    """Kick off a Phasen-Detective run for a single-phase device."""
    device_id = request.match_info["id"]
    app_ctx = request.app["app_ctx"]
    config = request.app["config"]
    ds = app_ctx.devices.get(device_id)
    if not ds or not ds.plugin:
        return web.json_response({"success": False, "error": "Device not found"}, status=404)

    entry = next((e for e in config.inverters if e.id == device_id), None)
    if entry is None:
        return web.json_response({"success": False, "error": "Inverter not in config"}, status=404)

    # Only single-phase types make sense here.
    if entry.type not in ("hoymiles", "opendtu", "shelly"):
        return web.json_response(
            {"success": False, "error": "Phasen-Detective only supports single-phase inverters"},
            status=400,
        )

    # Cross-location guard: the grid meter we throttle against belongs to
    # the Venus we're aggregating into, which only sees inverters at the
    # virtual-aggregate's locations. A probe on an inverter at a different
    # Standort just stirs up Multiplus noise without ever changing the
    # grid meter we're watching — refuse cleanly so the UI doesn't waste
    # 25 s on a guaranteed null result.
    from .payloads import _phase_detect_supported
    if not _phase_detect_supported(config, entry):
        return web.json_response(
            {
                "success": False,
                "error": (
                    "Phasen-Detective braucht denselben Standort wie das Grid-Meter "
                    "(virtual_inverter.location_ids). Dieser Inverter sitzt woanders."
                ),
            },
            status=400,
        )

    existing = _TASKS.get(device_id)
    if existing and existing["status"] == "running":
        return web.json_response(
            {"success": False, "error": "Detection already running"}, status=409
        )

    preconds = _check_preconditions(app_ctx, ds, entry)
    if not all(p["ok"] for p in preconds.values()):
        return web.json_response(
            {
                "success": False,
                "error": "Preconditions not met",
                "preconditions": preconds,
            },
            status=400,
        )

    _TASKS[device_id] = {
        "device_id": device_id,
        "status": "running",
        "started_ts": time.time(),
        "current_step": "starting",
        "logs": [],
        "result": None,
        "error": None,
    }
    config_path = request.app.get("config_path", "") if hasattr(request.app, "get") else ""
    asyncio.create_task(
        _run_detection(app_ctx, device_id, ds, entry, config=config, config_path=config_path)
    )
    return web.json_response({"success": True, "task_id": device_id, "status": "running"})


async def phase_detect_status_handler(request: web.Request) -> web.Response:
    """Poll the Phasen-Detective task state for a device."""
    device_id = request.match_info["id"]
    app_ctx = request.app["app_ctx"]
    config = request.app["config"]
    ds = app_ctx.devices.get(device_id)
    entry = next((e for e in config.inverters if e.id == device_id), None)

    from pv_inverter_master.ats_mqtt import ats_label
    body: dict[str, Any] = {
        "preconditions": _check_preconditions(app_ctx, ds, entry) if ds and entry else {},
        # Surface the live ATS state so the UI can show a badge next
        # to the Phasen-Detective header — relevant before kicking off
        # a probe, and lets a verify-result card flag a topology change.
        "ats": {
            "state": app_ctx.ats_state,
            "label": ats_label(app_ctx.ats_state),
            "ts": app_ctx.ats_state_ts or None,
        },
    }
    task = _TASKS.get(device_id)
    if task:
        body.update(task)
    else:
        body["status"] = "idle"
    return web.json_response(body)
