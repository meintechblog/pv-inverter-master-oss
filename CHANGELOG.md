# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- **One Modbus gateway per inverter host (`modbus_gateway.py`).** Every Modbus
  access to an inverter — poll, write, legacy commit, DPC init/verify, the
  config "test connection" button, the Sungrow probe, the network scanner —
  now runs through one `ModbusGateway` per host: one TCP session, one
  transaction at a time (asyncio lock), 150 ms minimum gap between
  transactions, writes coalesced (newest limit wins), connect retries
  throttled, and all traffic refused while the device is in QUIESCE. Plugins
  no longer own a client, so safety no longer depends on every caller holding
  `poll_lock`. The legacy `se30k-commit` fire-and-forget task is gone (the
  commit is written inside the write block). The scanner never touches hosts
  owned by a configured device. The Sungrow probe passes `device_id` (pymodbus
  3.x) instead of the removed `slave=`.
- **Single-instance lock** (`/run/pv-inverter-master/instance.lock`): a second
  proxy process exits immediately; `scripts/validate_se30k.py` refuses to run
  next to the service (`--force-parallel` overrides).

## [8.9.0] - 2026-07-18

### Added

- **`poll_gap_s` telemetry on the `inverter` Influx measurement.** Monotonic
  seconds between consecutive successful polls, per device.

  Both staleness floors in this codebase — the aggregation gate at 120s and the
  detector at 300s — were chosen without a measured distribution of how far
  apart real successes actually land. A feasibility check showed the existing
  Influx history cannot answer it: `inverter` carries only physical fields
  (ac_*, dc_*, energy, status), no poll timing at all. This is the missing
  instrumentation, not the calibration itself.

  It records the GAP rather than the age on purpose: at the instant of a
  successful poll the age is ~0 by construction, so sampling it there would
  measure nothing. The gap bounds the age — a device whose successes are X
  apart is at most X old between them — which is exactly what a floor has to
  clear. It is intentionally not derived from `poll_interval`: real spacing is
  driven by connection backoff (5s to 60s) and multi-read cycles, the same
  decoupling that put the aggregation floor at 120s.

  Additive and diagnostic: `None` until a device has succeeded twice, carried
  on the Influx queue message rather than the snapshot so it never enters the
  MQTT payload or HA discovery, appended only to a line that already has a real
  reading, and rejected if negative or non-numeric so a bad value cannot poison
  the series the calibration will read. Nothing in the control path reads it.

## [8.8.2] - 2026-07-18

### Fixed

- **Staleness thresholds now derive from the cadence a device actually runs at.**
  `_data_is_fresh()` and the `stale_threshold` in `classify_anomalies()` computed
  `max(5 * poll_interval, 300s)` from the RAW config value, where `0.0` means
  "use the default" — which is what almost every device carries. The expression
  therefore said "five missed polls" while always evaluating to the bare 300s
  floor. Both call sites now go through `_poll_cadence_s()`, which prefers the
  registry-resolved `effective_poll_interval` and falls back to the raw value
  for reduced payloads (tests, pseudo rows) that lack the newer field.

  **This changes no alert on the current fleet, by measurement, not by
  assumption.** All 11 live devices were compared before and after: the largest
  effective cadence is 20s (hoymiles), so `5x` it stays under the 300s floor and
  every threshold is 300s in both versions. A regression test pins that
  equivalence for the whole measured fleet, plus the case where the fix does
  bite (120s cadence -> 600s instead of 300s). It is a latent-correctness fix,
  not a recalibration.

  Whether 300s is the right floor at all remains open and intentionally untouched:
  the real inter-poll age is driven by connection backoff (up to 60s), not by the
  configured cadence — the same decoupling that put the aggregation freshness
  floor at 120s. Answering it needs a measured distribution per device type, and
  it would genuinely shift alert classification.

## [8.8.1] - 2026-07-18

### Fixed

- **Night-mode synthetic registers were off by two.**
  `build_night_mode_inverter_registers()` wrote the energy acc32 to offsets
  26-27 and the status to 40. Per SunSpec Model 103 — and per this repo's own
  decoder (`decode_model_103_to_physical`: WH at 24-25, WH_SF at 26, St at 38)
  — energy belongs at 24-25 and status at 38. The old layout put the energy
  HIGH WORD into the scale factor and the status into the event field, so the
  block decoded to energy 0 and status 0 instead of SLEEPING.

  Latent rather than live: the helper has no caller in `src/`. It was invisible
  because its tests asserted the SAME wrong offsets — they agreed with the bug
  instead of with the decoder. The tests now assert the correct layout, and a
  new test round-trips the builder's output through the real decoder rather
  than hand-counted indices, so this class of self-confirming test cannot
  recur here.

## [8.8.0] - 2026-07-18

### Added

- **Register view states what is actually polled, and how often.** The register
  page showed values but never said which blocks the proxy reads periodically —
  the one thing that matters on a single-connection inverter. It now renders a
  read-policy panel plus the device's EFFECTIVE poll interval.

  The policy text is reported by the plugin (`get_register_read_policy()`,
  concrete on the base class with an empty default) rather than hardcoded in
  the frontend, so it cannot drift from the code that does the reading —
  precisely the failure mode this release cycle uncovered elsewhere. Plugins
  that synthesise their blocks (shelly, senec, sungrow) return nothing and the
  section is omitted instead of showing something misleading.

- **`effective_poll_interval` in the device payload.** `poll_interval` is the
  raw config value where `0.0` means "use the default", so any consumer
  deriving a cadence from it was reasoning about an interval the device does
  not run at. The resolved value (per-device override > gateway > proxy
  default, floored by the plugin minimum) is now surfaced alongside it.

### Note

- Per-device poll interval was already user-configurable in the device config
  panel; no new control was added there. Per-FIELD cadence is intentionally not
  offered: AC power, voltage, frequency, temperature and DC all live in the
  same 52-register block, so a per-field rate cannot reduce Modbus traffic and
  a control implying otherwise would be misleading.

## [8.7.0] - 2026-07-18

### Changed

- **SE30K: the static Common Model is no longer re-read on every poll.**
  `poll()` issued two Modbus reads per cycle — Common (40002/67) and
  Inverter 103 (40069/52). The Common block is static identity (manufacturer,
  model, serial) and cannot change within a session, so re-reading it every
  cycle doubled the periodic transaction count for no information gain. On a
  SINGLE-CONNECTION inverter that matters: session pressure is exactly what
  wedges this device (July incident).

  It is now read once per `connect()` and replayed from a session cache, so
  the periodic poll issues a single read. The `PollResult` contract is
  unchanged — consumers still receive a full 67-register list — and each
  result gets its own copy so a consumer cannot mutate the cache.

  Cache lifetime is intentionally tied to the SESSION rather than a timer: the
  plugin instance is reused across quiesce-close and reconfigure, so a
  time-based cache could replay the identity of a device that is no longer on
  the other end (wrong serial after a hardware swap). `connect()` clears it
  before re-reading, so a stale identity cannot outlive its session.

  A failed one-shot read is non-fatal and leaves the cache empty: identity has
  no bearing on power regulation, and blank identity fields (consumers gate on
  `len >= 66`) beat a confidently wrong serial.

  Side effect, intentional: dropping the read also removes ~2 s of timeout
  latency that was previously spent before the Inverter-103 error path could
  be reached on a wedged inverter, so a wedge now surfaces sooner.

  Inverter 103 stays at full cadence — it carries AC power, which feeds the
  distributor and the Venus-facing aggregate.

## [8.6.2] - 2026-07-18

### Fixed

- **Freshness bound widened so a healthy device cannot flap out of the
  aggregate.** Caught by a pre-deploy measurement against the live system
  before 8.6.1 ever shipped. The 8.6.1 bound was floored at
  `proxy.staleness_timeout` (30 s), on the assumption that a device's
  configured `poll_interval` approximates its real cadence. It does not: the
  SE30K's configured interval is `0.0` (the 1 s proxy default), while its
  measured poll age sawtooths 0.4 s → 17.1 s (60 s window, n=24) with a 32 s
  sample observed the same morning. A slow Modbus device issues several reads
  per cycle, and on failure the poll loop backs off via `conn_mgr`
  (`sleep_duration`, up to 60 s) rather than honouring `poll_interval`. The
  main PV source would therefore have dropped in and out of the sum Venus
  regulates on — intermittently losing it is worse than briefly carrying a
  dead device.

  The bound is now `max(AGGREGATE_STALE_FLOOR_S = 120 s, poll_interval * 3)`,
  decoupled from `staleness_timeout` (the two answer different questions:
  cache-to-Venus liveness vs. per-device fitness to contribute). 120 s still
  catches the failure this gate exists for — the July wedge froze data for an
  8 min quiesce window.

## [8.6.1] - 2026-07-18

### Fixed

- **Phantom PV: a dead device no longer contributes frozen registers to the
  Venus-facing aggregate.** `AggregationLayer.recalculate()` collected
  `DeviceState.last_poll_data` with no age check at all. That field is only
  written on a successful poll and is never cleared on failure, so a device
  that stopped polling kept its last register set in the sum Venus regulates
  on — indefinitely. The register cache's own `is_stale` could not catch it:
  it tracks a single global timestamp refreshed by *any* device's successful
  poll, so one healthy sibling masked every dead one. During the July SE30K
  wedge this meant Venus saw phantom solar power for the whole quiesce window.

  A device now contributes only if it passes a per-device freshness gate:
  `0 <= age <= max(proxy.staleness_timeout, poll_interval * 3)` **and** it is
  not flagged `source_stale`. Both conditions are load-bearing — the HTTP
  plugins (hoymiles, opendtu) intentionally report timeouts as `success=True`
  and set `source_stale`, so their success stamp keeps advancing and the age
  bound alone would never exclude them. Age is measured on a new monotonic
  twin stamp (`DeviceState.last_successful_poll_monotonic`) so a backward NTP
  step cannot yield a negative age that silently marks every device fresh;
  `last_successful_poll_at` stays wall-clock because the UI and the anomaly
  detector consume it as such. If every device is stale the aggregate is left
  untouched, the cache ages out and Venus is correctly told the device is
  unavailable (fail-safe rather than fail-sideways).

## [8.6.0] - 2026-07-17

### Added

- **Escalation `dedup_key` for receiver-side deduplication.** The hourly
  detector's `/api/escalate` webhook carries a stable per-anomaly
  `dedup_key` (`pv-inverter-master:anomaly:<device_id>:<cooldown_type>`,
  mirroring the push notification key; MULTI_CLIENT aliases to MODBUS_HANG
  onto one shared incident stream). This lets the receiver coalesce repeated
  escalations for a chronic, unchanged anomaly. The detector's own alert
  path is unchanged: the 6 h POST still fires and no critical is masked.
  Detector-side severity/signature damping could hide a real large-inverter
  outage whose only signal is warning-severity STALE_POLL, so deduplication
  remains the receiver's responsibility.

## [8.5.0] - 2026-07-16

### Added

- **MULTI_CLIENT anomaly type.** A session-limited inverter (SolarEdge)
  blocked by a foreign Modbus client looks exactly like a firmware wedge
  from the poller's side — TCP up, Modbus silent (2026-07-16 incident: two
  zombie legacy instances held 22+27 leaked connections for weeks and the
  outage read as a wedge). The hourly detector now runs an evidence pass on
  corroborated MODBUS_HANG anomalies: one bare TCP accept probe (no Modbus
  bytes) plus a conntrack query against the fleet's `modbus_conntrack`
  Influx measurement. Foreign established sessions found → the anomaly is
  retyped MULTI_CLIENT/critical naming the source IPs (remedy: kill the
  client, NOT a power-cycle); otherwise the MODBUS_HANG detail carries a
  check-clients-first runbook hint (an empty conntrack result is NOT proof
  of coverage). MULTI_CLIENT shares the MODBUS_HANG cooldown/dedup identity
  (one incident stream — no false recovery on deploy, no alert storm on
  flapping coverage; a diagnosis upgrade re-alerts exactly once). Probes are
  opt-in seams wired only by the production entrypoint; host values are
  pattern-validated before probe and Flux interpolation. Influx settings via
  `PV_ANOMALY_INFLUX_*` env or the proxy's config.yaml.

## [8.4.3] - 2026-07-16

### Fixed

- **/api/status honesty.** The endpoint reported `"solaredge": "connected"`
  as soon as ANY device's connection manager was connected — a dead SE30K
  among ten healthy devices was invisible, and a bare TCP connect without a
  single successful poll also read as connected (2026-07-16 incident: weeks
  of session-exhaustion by zombie legacy instances went unnoticed). A device
  now counts as healthy only when its connection state is `connected` AND it
  has at least one successful poll; the aggregate reports `connected` (all
  healthy), `degraded` (some healthy) or the first device's raw state
  (`connected` mapped to `connecting` when no poll ever succeeded). Additive
  fields: `devices_ok` (int) and `devices_unhealthy` (device ids).

## [8.4.2] - 2026-06-12

### Fixed

- **Anomaly detector: one webhook escalation per device per run.** A wedged
  device raises several anomaly types for the same root cause (MODBUS_HANG
  critical + STALE_POLL warning) and each escalated separately — two requests
  per hourly run for one incident. The escalation seam now picks only the
  highest-severity alert per device; push notifications stay
  per-type (their dedup/recovery keys are unchanged).

## [8.4.1] - 2026-06-12

### Fixed

- **`/api/dashboard` no longer 503s while healthy devices exist.** The
  endpoint served the FIRST configured device's snapshot (Phase-24 TODO);
  with the SE30K wedged and snapshotless since a restart, the dashboard
  reported "no data" despite 10+ healthy devices. It now prefers the first
  device (legacy behavior unchanged while healthy) but falls back to the
  first device that has a snapshot, and the response carries `device_id`
  for explicit provenance. 503 now means: no device has any data at all.

## [8.4.0] - 2026-06-12

### Added

- **SolarEdge limit watchdog via Enhanced Dynamic Power Control (0xF300).**
  Limit writes move from the latchable simple-dynamic register 61441 to the
  dynamic limit 0xF322 with a Command Timeout (0xF310, default 120 s) and a
  Fallback Active Power Limit (0xF312, default 100 %): when our writes stop
  (Modbus wedge, proxy crash, network loss), the inverter reverts to full
  power BY ITSELF instead of latching the last clamp — the structural fix
  for the 2026-06-10/11 incidents where a 5 % = 1500 W morning clamp
  latched through a day-long Modbus service crash (~60 kWh loss each).
  Design choices:
  - Idempotent init on every connect: word-order probe on read-only
    0xF304 (the Technical Note does not specify 32-bit endianness),
    read-first/write-on-mismatch, read-back verify, enable written last.
  - Grid-relevant registers (0xF142, 0xF104) are gated READ-ONLY — never
    written autonomously; gate failure degrades to the legacy path.
  - Legacy register 61441 is normalized to 100 during init so a residual
    pre-watchdog clamp cannot survive underneath the dynamic path.
  - Writes are refused during the init window (no latchable legacy write
    can race the arming), and an armed-check every 60 writes re-inits
    after an inverter-side reset — a failed re-arm fails the write loudly.
  - pymodbus auto-reconnect disabled (`reconnect_delay=0`) so every new
    session goes through the managed reconnect path including re-init.
  - A failed dynamic write never falls back to a legacy static write
    (a static limit written into flaky comms is exactly the latch).
  - Per-device config: `dynamic_power_control` (default on),
    `dpc_command_timeout_s`, `dpc_fallback_pct`.
  Limit changes now cost 1 Modbus transaction instead of 2–3 (relevant to
  the suspected write-load wedge correlation). Review findings on races,
  residual limits, stale re-initialization and silent no-ops were fixed and
  re-verified. HIL verification of the
  revert-to-fallback behavior is pending the SE30K's comms recovery.

## [8.3.8] - 2026-06-11

### Fixed

- **Anomaly detector: "Not connected" now matches MODBUS_HANG.** pymodbus
  wraps a refused TCP connect into `ConnectionException("Not connected")`,
  which matched no hang signature — so when the live SE30K wedge escalated
  from "TCP open, Modbus silent" to "port 1502 refusing" (the HARDEST
  failure mode), the detector's MODBUS_HANG entry flipped to resolved and
  the multi-hour daylight outage downgraded itself to STALE_POLL/warning.
  `"not connected"` added to `MODBUS_HANG_SIGNATURES`; the existing
  corroboration gate (non-connected state / stale threshold) keeps a lone
  transient from flapping critical alerts.

## [8.3.7] - 2026-06-11

### Fixed

- **Write path joins the poll-lock drain.** The
  v8.3.5 read-drain only covered the poll loop — webapp clamp writes and
  the distributor wrote `write_power_limit` LOCKLESS, so during
  stop_device's up-to-10s drain window a parallel write could land on
  the same client and get FIN'd mid-write (the SE30K single-connection
  wedge mechanism, again). All write_power_limit call sites now route
  through one choke point (`locked_write_power_limit`) that holds the
  device's poll lock; stop_device releases the drained lock only AFTER
  `close()`, so queued writers fail clean ("Not connected") instead of
  racing the FIN. The poll loop's deferred-restore `distribute()` lost
  its outer lock block — with per-write locking it would double-acquire
  the non-reentrant asyncio.Lock (deadlock). SolarEdge `close()`
  additionally sets a `_closing` flag so a parallel write can no longer
  re-arm a fresh se30k-commit task against the closed client (reset on
  `connect()`, quiesce cycles unaffected). Beyond the review findings,
  two further write sites found during execution were routed through
  the choke point as well: the Phasen-Detective throttle/restore
  writes (per-device lock, live) and the EDPC refresh loop (optional
  lock parameter — a prod-dead path, only exercised by tests, never
  started in production; parametrized prophylactically).
- **stop_device cleanup is cancel-proof.** Moving
  `lock.release()` behind `plugin.close()` made close() part of the
  lock's critical window — but close() is a cancellation point
  (solaredge re-raises CancelledError explicitly) and stop_device runs
  in aiohttp handlers (cancelled on client disconnect). A cancel landing
  in the close() window skipped `lock.release()` + `devices.pop` → the
  poll lock stayed held forever and every queued writer hung. Nested
  try/finally now makes release + pop unconditional; CancelledError
  still propagates afterwards.
- **Writer lock acquire is bounded (defense-in-depth).**
  `locked_write_power_limit` acquires the poll lock via
  `asyncio.wait_for(..., WRITE_LOCK_TIMEOUT_S=12s)` instead of
  unbounded — any future lock-leak path degrades to a logged
  `WriteResult(success=False, error="write_lock_timeout")` retry (the
  Venus control loop re-ticks every 5s) instead of hang-forever.

## [8.3.6] - 2026-06-11

### Fixed

- **Updater: release clones explicitly `git fetch origin <target-sha>`
  after dissociation** (SE30K-saga updater lesson #4). `clone --shared`
  inherits only the previous release's stale `refs/heads` — the target
  sha lived in the SOURCE's `refs/remotes/origin/main` and was
  unreachable from any cloned ref, so the reachability-based
  `repack -a -d` of the dissociate step dropped its objects entirely and
  the checkout failed (live deploy failure 2026-06-11 11:17Z on .191,
  run died late at pip dry-run with "neither setup.py nor pyproject.toml
  found"). Fetch-by-sha stays anchored by the earlier `is_sha_on_main`
  check; a failed fetch is FATAL (extract error + `rollback_failed`),
  unlike the warn-only set-origin/dissociate steps.
- **Updater: `git checkout --detach` result is now gated** like every
  other extract step (lesson #5) — it was the ONLY git step in the
  extract phase whose result was discarded. Failure now writes the
  extract phase error, finalizes `rollback_failed` and exits, instead of
  silently leaving an empty worktree with stale HEAD.

## [8.3.5] - 2026-06-11

### Fixed

- **Read-Drain-then-Close on device shutdown.** Live incident 2026-06-11
  05:22Z: a graceful SIGTERM restart still wedged the SE30K — the
  maintenance drain only covers WRITES; `stop_device` then cancelled the
  poll task mid-Modbus-read and sent FIN (at 1 Hz with two sequential
  2s-timeout reads the cancel almost always lands mid-transaction).
  `stop_device` now acquires the per-device poll lock (drains the in-flight
  read) before cancelling; on timeout it falls back to the hard
  cancel so shutdown never hangs. Structured events `read_drain_ok` /
  `read_drain_timeout` make provoked-restart A/B tests measurable.
- **Read-drain hardening:**
  - `stop_all` drains all devices in **parallel** (`asyncio.gather`) — the
    previous sequential loop made shutdown wall time the SUM of all drains,
    blowing past systemd's `TimeoutStopSec=15` with multiple wedged devices
    and ending in SIGKILL (exactly the unclean kill the drain exists to
    prevent). Parallel = wall time ≈ one drain. The unit file is NOT
    auto-deployed by the updater (pip install + restart only), so
    `TimeoutStopSec` stays at 15 — fine with max ≈ 10s.
  - `stop_device` cleanup is **try/finally-guaranteed**: it runs inside
    aiohttp handlers, which are cancelled on client disconnect. A cancel
    landing in the drain await used to orphan the popped-but-alive poll
    loop (invisible, holding the TCP session — re-enable opened a SECOND
    connection on the one-slot SE30K) and leak the acquired lock / skip
    `plugin.close()`. Now cancel+close+state-removal always run;
    CancelledError still propagates.
  - **Write path closed too:** the deferred boot limit-restore
    `distribute()` call (a Modbus WRITE on the same client) moved under the
    poll lock, and SolarEdge `close()` now waits up to 4s for an in-flight
    fire-and-forget se30k-commit write to finish (cancel only as fallback)
    instead of cancelling mid-write + FIN.
  - `READ_DRAIN_TIMEOUT_S` 4s → **10s**: worst case is 2 reads ×
    (retries+1) × timeout = 2 × 2 × 2s = 8s — the old 4s budget could
    expire mid-transaction and fall back to exactly the mid-read cancel
    the drain is meant to prevent.

## [8.3.4] - 2026-06-11

### Fixed

- **Anomaly detector: DEAD_INVERTER requires FRESH data.** Live misfire
  2026-06-11 09:00: DEAD_INVERTER critical fired on the Modbus-wedged SE30K
  whose `power_w=0` was a never-polled startup default. `_classify_dead` now
  gates on a fresh successful poll (same threshold as STALE_POLL), and the
  sibling "producing" anchor only counts siblings with fresh data (a wedged
  device's frozen power must not mark a weather-zero sibling dead). Test
  fixtures no longer default to an implicitly-stale 1970 poll timestamp.
- **MODBUS_HANG signatures extended with `connection refused` / `timed
  out`**: a powered-off Modbus-TCP inverter raises
  plain socket errors that matched no signature — with the new freshness
  gate it would have been stuck at STALE_POLL warning forever instead of
  escalating.

## [8.3.3] - 2026-06-11

### Changed

- **Quiesce probe burst:** after a quiesce window expires the device gets
  `QUIESCE_PROBE_ATTEMPTS` (3) quick retries (~1 s apart, fresh session
  each) before the next full window is armed — a single transient
  first-request timeout no longer costs another 8 minutes.
- **Wedge model update (live observation 2026-06-11):** two clean 8-min
  zero-client silence windows did NOT release the wedge — silence alone is
  not sufficient. Working model: the SE30K comm stack recovers on INTERNAL
  state transitions (standby/wake at dawn & dusk — matches the 04:07 and
  ~06:49 recoveries). Quiesce remains correct client behavior (no
  hammering, catches the recovery automatically whenever it comes).

## [8.3.2] - 2026-06-11

### Fixed

- **Updater: release clones are dissociated from the shared object store.**
  `clone --shared` chains each release's git objects to its parent dir via
  `objects/info/alternates`; release retention deletes old release dirs and
  thereby silently corrupts every child clone (live failure 2026-06-11:
  `fatal: unresolved deltas left after unpacking`, updates permanently
  wedged). New releases now get `git repack -a -d` + alternates removal
  right after the clone — self-contained by construction.
- **Updater: a failed `git fetch` before the ancestor check is now logged
  with stderr** instead of silently leaving frozen refs (a bare
  `sha_not_on_main` was a debugging dead end).

## [8.3.1] - 2026-06-11

### Added

- **Auto-quiesce self-healing for wedge-prone Modbus-TCP inverters
  (`solaredge`, `sungrow`).** The SE30K SunSpec server wedged twice in two
  days (TCP accepts, every Modbus request times out — 19h41m on 2026-06-10,
  again on the 2026-06-11 update restart). Live-verified: the wedge releases
  after ~5–7 min of TRUE zero-client silence. After 8 consecutive poll
  failures the device now enters `quiesce`: connection closed, ALL traffic
  (polls, writes, reconnects) suspended for 8 minutes, then one probe;
  failure re-arms the window. Would have healed the 19h hang in ~16 min.
  Night mode is suppressed for these device types (always a
  misclassification there — a healthy SE30K stays reachable at night).

### Changed

- **Boot limit-restore is now poll-first.** The persisted power limit is no
  longer written to the devices immediately at startup (the 2026-06-11 wedge
  hit on a restore write that was the very first request of the fresh Modbus
  session). The restore re-runs `distribute()` with the then-current
  ControlState value on the first successful poll; an earlier Venus write
  supersedes it.
- **Distributor skips limit writes (incl. force-release) for devices in
  quiesce** — a deaf device only gets re-poked by writes; it keeps its last
  committed limit either way.
- **SolarEdge commit writes (`0xF100`) are single-in-flight per device**:
  the latest commit replaces a still-running one instead of piling up
  parallel transactions on a slow session.

### Fixed

- **Updater: release clones now fetch from the real repo.** `git clone
  --shared` pointed each new release's `origin` at the LOCAL previous
  release dir, so `git fetch origin` never reached GitHub and the
  `is_sha_on_main` security anchor validated against a frozen ref — any
  freshly pushed SHA failed with `sha_not_on_main` (live failure 2026-06-11).
  The updater now rewires `origin` to the canonical repo URL on the current
  release before the fetch (heals pre-fix chains) and on every fresh clone.
  URL is baked in, overridable only via root-owned `updater.env`
  (`REPO_URL=`); token auth stays root-only in the git credential store.

## [8.3.0] - 2026-06-10

### Added

- **Anomaly detector classifies SolarEdge Modbus hangs as `MODBUS_HANG`
  (critical) instead of `STALE_POLL` (warning).** Driven by the 2026-06-10
  SE30K incident: the inverter's SunSpec/Modbus server hung for 13+ daylight
  hours but the detector only ever reported a soft `STALE_POLL` with the
  misleading detail `connection_state=night_mode`. The `MODBUS_HANG` rule now
  covers all Modbus-TCP inverter types (`sungrow`, `solaredge`), with two
  guards:
  - **Single-sample guard:** the Modbus error signature alone no longer
    classifies — it must be corroborated by a non-`connected` connection state
    or a last successful poll older than the staleness threshold. Prevents a
    lone transient timeout (coinciding with the hourly oneshot) from paging
    critical every run via the cooldown-bypassing resolved→active transition.
  - **Auto-fix gating on `supports_reboot`:** the reboot-POST auto-fix only
    fires for devices whose plugin implements `reboot()` (Sungrow WiNet-S).
    For solaredge the hang classifies + escalates but never POSTs a reboot.

### Changed

- `STALE_POLL` detail now carries the staleness age alongside the connection
  state (`connection_state=night_mode, no successful poll for 49213s`) — the
  state label alone said nothing about outage duration.
- Push notification severity for `MODBUS_HANG` bumped `warn` → `error`: a corroborated
  Modbus hang is a production outage, and devices without reboot support have
  no auto-fix softening it.

## [8.2.0] - 2026-06-08

### Added

- **Anomaly detector tells an OpenDTU radio gap apart from a dead inverter
  (kills `DEAD_INVERTER` alarm fatigue).** A transient OpenDTU↔micro NRF radio
  gap made the proxy report a producing micro as `connected`/`power_w=0`/no
  error — indistinguishable from a genuinely-dead inverter — so the hourly
  detector fired a false `DEAD_INVERTER` CRITICAL every clear midday (the
  "Garden" HMT-2250-6T did so 3× in 4 days). Fix, source-layer:
  - `PollResult.stale` (new, default `False`); the OpenDTU plugin sets it on
    its `reachable=false` / `data_age>60s` / empty-list returns (success polls
    with no fresh data). `device_registry` records it on the device and
    `/api/devices` surfaces `source_stale`.
  - The detector reclassifies a would-be `DEAD_INVERTER` micro that is
    `source_stale` as a soft, **informational-only** `RADIO_GAP` (logged, not a
    push notification) — UNTIL the gap fails to recover. A per-device persistence
    clock accumulates **daylight** staleness (anchored on a producing sibling,
    so night excludes itself); once it exceeds `radio_gap_escalate_s`
    (default 3 h, env `PV_ANOMALY_RADIO_GAP_ESCALATE_S`) the gap is re-promoted
    to `DEAD_INVERTER` CRITICAL — a real radio-silent death is never
    permanently masked. The clock resets only on **substantial** recovery
    (genuine production), not a reachable-but-0 W flap, and survives the night
    + a restart (persisted in the state file).
  - Fail-safe: a device without `source_stale` (any non-OpenDTU row, old proxy)
    keeps the exact previous `DEAD_INVERTER` behavior.
  - Only elapsed time distinguishes a transient gap from a hard
    death, so the time-escalation — not a recurring warning — is the
    masking-safety net.

## [8.1.3] - 2026-06-04

### Fixed

- **Auto-update check can authenticate with an optional token.** For a
  private fork, the unauthenticated `GET /releases/latest` poll returns 404
  and silently stops detecting releases. `GithubReleaseClient` now sends
  `Authorization: Bearer <token>` when a token is configured — via an
  explicit constructor arg or the `GITHUB_TOKEN` env var (resolved at
  construction, not as an import-time default). Absent token = unchanged
  legacy public behavior (no auth header), so public/dev runs are
  unaffected. The main service unit gains an optional
  `EnvironmentFile=-/etc/pv-inverter-master/updater.env` so an operator can
  drop `GITHUB_TOKEN=...` box-side without the secret ever entering git.
  NOTE (out of scope, box-side): the code *download* path (`git fetch
  origin` against the private HTTPS remote) authenticates via git's own
  credential layer, which must be provisioned separately (credential helper
  or token-in-remote). The release *check* fails closed — an auth failure
  (401/404) logs a warning and returns `None`, never crashing the scheduler
  or clearing a previously-detected update.

## [8.1.2] - 2026-06-02

### Fixed

- **Auto-updater now resolves the install target from the GitHub release tag,
  not `origin/main`.** A new `resolve_release_commit(tag_name, install_dir)`
  maps the release tag to its concrete commit SHA via
  `git rev-parse <tag>^{commit}`, and `_on_update_available` uses it in place
  of the old `resolve_remote_main_sha()`. This fixes a production regression
  where a deployed box's `origin/main` was a stale local mirror frozen at the
  bootstrap commit, causing the nightly updater to install an old commit and
  silently downgrade prod from v8.1.1 back to v8.0.0. Resolution now fails
  closed: an unresolvable tag returns `None`, no install target is written,
  and the existing `run_auto_update_once` guard refuses to trigger on a
  missing SHA — so the updater can never install a non-release commit. The
  dead `resolve_remote_main_sha` helper has been removed.
