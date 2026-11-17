# Phasen-Detective

Auto-detection of which AC phase (L1 / L2 / L3) a single-phase inverter
is wired to, by briefly throttling it and watching for the per-phase
grid-power Δ. Lets the user assign phases without crawling into the
fuse box.

## Why this is hard (the noise floor)

The naive algorithm — throttle inverter, measure grid Δ per phase, take
the biggest one — fails for the systems this proxy is actually deployed
on. Three real-world effects fight us:

1. **Multiplus 3-phase rebalancing.** ESS-mode Multiplus units chase
   grid power to zero within ~10 s. When PV on (say) L2 drops, the
   Multiplus pushes from its battery into L2 to compensate. The grid
   meter sees a smaller net Δ on L2 — and a *common-mode* drift across
   all three phases as the rebalancer reshapes the AC.Out side.
2. **Hardware silent-rejection.** Hoymiles HMS micros (whether driven
   directly or via OpenDTU) store every power-limit write to EEPROM
   and silently reject any write within ~300 s of the previous one.
   Second and third "throttle" commands return success but the device
   never ramps. The grid signal is then just noise.
3. **Polling-cache latency.** The proxy polls Hoymiles every 20-30 s
   to respect the device's rate limit. `ds.last_poll_data` doesn't
   refresh during a 20 s probe window, so poll-based "wait for
   rampdown" loops see the pre-throttle wattage the whole time and
   report failure even when the throttle actually worked.

The algorithm below is built around these three constraints.

## Algorithm

### Single probe

```
1. Reserve the device against the distributor's WMaxLimPct keepalive
   (atomic — app_ctx.reserve_device).
2. Baseline: average grid per-phase power over ~3 s.
3. Throttle the inverter:
     - Hoymiles / OpenDTU → write_power_limit(True, 0.0, force=True)
     - Shelly             → switch(False)
4. Wait for rampdown:
     - Hoymiles → fixed 8 s sleep (poll cache too slow to observe)
     - else     → poll ds.last_poll_data until power < 10 % of baseline,
                  max 20 s. If poll-based and no rampdown observed →
                  fail-fast (hardware ignored the command).
5. Measurement: 5 windows × 1.5 s grid average, 1 s gap between.
6. For each phase, take the MAX Δ across windows (peak-of-windows).
   Median would smooth the post-throttle transient — Multiplus
   catches up within ~10 s, so the inverter's signal exists only
   as a brief peak right after the throttle.
7. Common-mode subtraction: take the per-probe median of the three
   peak Δs (= the Multiplus drift component) and subtract it. The
   residual per phase is the differential effect of the throttled
   inverter, not the household noise floor.
8. Pick the winner phase = max residual. Accept iff:
     - winner_residual ≥ max(30 W, 5 % × baseline_inverter_w)
       (absolute floor + proportional floor)
     - winner_residual ≥ 2 × second-best residual (dominance)
9. Restore: write_power_limit(False, 0.0) or switch(True).
```

The throttle-score loop closes here too: the measured rampdown time is
written into `DeviceLimitState.measured_response_time_s`, and the
distributor's `_effective_score` prefers the measured value over the
plugin's static declaration.

### Multi-run consensus (verification mode)

A single probe on a small inverter (100-200 W) is fragile — observed
live: Shed flipped L2 → L1 between sessions; Balcony L1 → L2 with
the Multiplus drifting -1716 W common-mode. The algorithm answers the
right phase *most* of the time, so multi-probe voting closes the gap.

```
Detection mode is picked from the entry's phase_explicit state:
  - phase_explicit=False (first encounter) → N=1, auto-apply
  - phase_explicit=True  (verification)    → N=3, never auto-apply

Verification N=3 runs the single-probe sequence above 3 times. The
gap between probes respects the plugin's declared cooldown_s so the
hardware actually accepts each probe (Hoymiles needs 300 s; Shelly
uses 30 s since its 300 s is a relay-wear advisory, not silent-
rejection). Total verify time: ~12 min for HMS, ~2 min for Shelly.

Consensus = phase that wins ≥ majority of successful probes. If a
verification's consensus disagrees with the stored phase, the UI
surfaces a mismatch card with an explicit "Überschreiben"-Button —
never silently overrides.
```

The first probe failing on a verify run aborts the remaining probes
(saves the user 10+ min of guaranteed-failing time when e.g. the
cooldown hasn't actually elapsed from a previous session).

## Preconditions

Before kicking off a probe, six gates must all pass:

| Gate | Condition |
|------|-----------|
| `grid_meter_present` | Venus published `grid/+` topics and all three `grid_lN_w` are non-null |
| `pv_producing_enough` | Inverter is producing at least the per-device floor (Shelly 50 W, OpenDTU 50 W with rated/panel unset, otherwise `max(100, min(200, 0.5 × max(rated, panel_wp)))`) |
| `device_reachable` | ConnectionManager reports `connected` |
| `throttle_enabled` | The entry has throttle commands enabled |
| `plugin_supports_throttle` | For Shelly: `switch()` exists. For others: `write_power_limit()` exists. (Shelly's `write_power_limit` is a no-op, so type-aware routing matters.) |
| `cooldown_ready` | Time since the plugin's `_last_limit_ts` ≥ `caps.cooldown_s`. Protects against silent-rejection of back-to-back probes. |

And one architectural gate, `_phase_detect_supported(config, inv)`:
the inverter must sit at a location in `virtual_inverter.location_ids`.
The grid meter we throttle against only sees inverters at the locations
that flow into our Venus aggregate — a probe on an inverter at a
different Standort just stirs up Multiplus noise. The UI hides the
Phasen-Detective panel entirely for those entries.

## ATS (Auto-Transfer-Switch) awareness

Installations with a central transfer switch flip the entire house
topology between grid-parallel (AC.In) and island (AC.Out) modes. The
detected phase doesn't depend on the topology per se, but the *signal
strength* and even the *Δ direction* can shift dramatically in island
mode (Multiplus carries the full load instead of the grid).

`ats_mqtt.py` subscribes to a configurable MQTT topic
(default `home/ats/state`, override via `PV_INVERTER_ATS_TOPIC`) and caches the
state on `AppContext.ats_state`:

| Value | Meaning |
|-------|---------|
| `1` | Netzparallel — AC.In, normal grid-parallel operation |
| `2` | ESS — AC.Out, Inselbetrieb, Multiplus is the source |
| `-1` | Loxone reports an error / unknown |
| `None` | No MQTT publish observed (different install, broker down, etc.) |

The state is surfaced as a coloured badge in the Phasen-Detective
header and recorded in every result payload as `ats_state_at_run`.
A future verification that runs in a *different* ATS state than the
original detection should flag it to the user — the algorithm can't
guarantee the same phase assignment holds across topology changes.

Loxone publishes values in a metadata envelope
(`{"value": 1, "name": ..., "type": ..., "uuid": ..., "room": ...}`).
The parser handles both that envelope and a plain integer payload so
other bridges work too.

## API

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/devices/<id>/detect-phase` | POST | Kick off a probe (returns 202 + task_id; 400 with preconditions if any fail) |
| `/api/devices/<id>/detect-phase` | GET | Poll task state — returns `status`, `current_step`, `current_run`/`total_runs`, `logs[]`, `preconditions`, `ats`, and (when complete) `result` |

### Result schema

```jsonc
{
  "detected_phase": 2,           // null = no consensus / not accepted
  "confidence": 100,             // % of successful probes that voted for consensus
  "consensus_phase": 2,
  "consensus_count": 3,
  "consensus_strong": true,      // true = all probes agreed; false = majority only
  "n_runs": 3,
  "n_successful": 3,
  "mode": "verify",              // "initial" | "verify"
  "ats_state_at_run": 1,         // ATS state at probe start (null if no MQTT)
  "applied_to_config": true,     // server wrote phase + phase_explicit
  "verify_mismatch_phase": null, // set on a verify run when consensus ≠ stored
  "stored_phase_before_run": 2,
  "probes": [/* per-probe details */],
  "baseline_grid": [-3230, -730, -3227],
  "baseline_inverter_w": 728,
  "ramp_down_time_s": 6.0,
  "windows": [/* per-window grid + deltas of the first probe */],
  "medians": [5, 13, 75],        // peak Δs of the first probe (named "medians" for back-compat)
  "verdict": {/* first probe's verdict */}
}
```

## Known limitations

- **Inverters on Multiplus AC.Out are partially invisible.** The
  rebalancer's compensation cancels most of the grid-side Δ. Common-
  mode + peak-of-windows recover *some* signal, but residuals can drop
  to single-digit Watts on small AC.Out inverters. A second data
  channel (Multiplus's per-phase `vebus/+/Ac/Out/L[123]/P` topics)
  is the obvious next step but isn't implemented yet.
- **Cross-location detection is impossible by design.** Inverters at
  Standorte outside `virtual_inverter.location_ids` are on a different
  physical grid; we never see their throttle. The UI hides the
  Phasen-Detective panel for those — the only way to assign a phase is
  manual, ideally cross-checked against the fuse box.
- **Cloudy / fading-light reproducibility.** Below ~150 W output the
  signal-to-noise on small inverters gets dicey. Run probes during
  full sun (midday) for the highest-quality result. The verify mode's
  consensus exists precisely to filter noisy single-run outliers.
- **HMS hardware reorders silently.** A throttle within the 300 s
  cooldown window returns API success but the micro never ramps. The
  cooldown precondition catches this for direct Hoymiles + OpenDTU
  alike, but if you bypass it (e.g. external scripted POST) you'll
  see "success" responses with no actual effect.

## Touch-points in the codebase

- Algorithm: `src/pv_inverter_master/webapp/routes_phase_detect.py`
- ATS subscriber: `src/pv_inverter_master/ats_mqtt.py`
- Throttle-score persistence: `_persist_measured_response_time` ↔
  `DeviceLimitState.measured_response_time_s` (in `distributor.py`)
- UI: `src/pv_inverter_master/static/app-inverter-config.js`
  (`_wirePhaseDetective`, `_phaseOptionsHtml`, `renderResult`)
- Config field: `InverterEntry.phase_explicit` (default False, set on
  any save that includes a phase value; legacy `phase ≠ 1` migrated to
  True on load)
