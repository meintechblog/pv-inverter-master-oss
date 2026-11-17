# Hoymiles HMS Integration

Plugin for Hoymiles HMS micro-inverters with **built-in WiFi DTU** —
no separate gateway needed. Talks directly to the inverter on TCP
`10081` using its protobuf protocol (`hoymiles-wifi` library).

Covers HMS-300/350/400/500/600/700/800/1000/1500/1600/1800/2000 (single-
or two-MPPT). For Hoymiles micro-inverters that need a DTU gateway
(HM-series, plus HMS over OpenDTU), use the `opendtu` plugin instead.

## Protocol

| Request | Returns |
|---------|---------|
| `async_get_real_data_new()` | Live frame: `sgs_data` (AC), `pv_data` per MPPT, energy counters |
| `async_app_information_data()` | DTU firmware, WiFi signal, grid-profile metadata |
| `async_set_power_limit(pct)` | Set inverter power limit (writes EEPROM!) |
| `async_turn_on/off/reboot_inverter(serial)` | Soft on/off/restart |

The library handles CRC16, multi-packet stitching and the 2 s minimum
inter-call gap.

## Poll-rate floor — DO NOT poll fast

HMS built-in DTUs are tiny embedded modules. The vendor cloud polls
them every ~15 min; the Home Assistant `hoymiles-wifi` integration
every ~40 s. Polling much faster **overwhelms the DTU**: it stays
reachable on TCP but answers with degraded frames — `0 W AC`,
collapsed DC voltage — while the inverter is actually feeding in fine.

**Live-confirmed 2026-05-22:** at a ~2 s poll cycle two HMS units
(Cabin, Cottage) reported `0 W` for hours; dropping the cycle to
~45 s restored 771 W / 510 W within one poll. A 6-hour 30-s soak
across 3 units showed zero polling-related degradation.

**Empirical history on the live fleet** (two FW 267 units, one
FW 4097):

- **2 s** — instant freeze on all units (the original outage).
- **15 s** — froze all three within minutes (1 distinct `(ac, dc)`
  value across 26 samples; the DTU stops refreshing its measurement
  frame).
- **20 s** — passed a 6 h afternoon soak (2026-05-23) cleanly. BUT
  2026-05-24 the two FW 267 units **froze ~5 h after the dawn
  wake-up** while the FW 4097 unit stayed fine. The afternoon-only
  soak missed the full day → night → day cycle. **20 s is not safe
  for the older firmware long-term.**
- **30 s** — rock-solid across multiple full day/night cycles. This
  is the live setting.

Once frozen, a DTU stays frozen even when the poll rate is reduced;
a `restart` command (the proxy's `POST /api/devices/{id}/hoymiles/power`
with `{"action":"restart"}`) un-sticks it. Worst case a unit
self-recovers overnight when the DC drops away and it cold-boots
at dawn.

The plugin enforces a per-plugin floor:

- `plugin.py` — `InverterPlugin.min_poll_interval` (base property,
  default `0.0`).
- `device_registry.start_device` — clamps the configured / global poll
  interval up to that floor and logs `poll_interval_floored`.
- `plugins/hoymiles.py` — `MIN_POLL_INTERVAL_S = 30.0` (the live
  safe floor). A future enhancement could probe the DTU's firmware
  version and use a tighter 20 s floor on FW ≥ 4097 units; the
  current code is firmware-uniform.

When debugging a Hoymiles at 0 W, **always check the poll rate
before trusting the inverter's self-reported status** — the data
path can be fine while the DTU is overwhelmed.

## Night-mode backoff

At night the inverter produces nothing and the HMS DTU **keeps its TCP
port open but answers `None`** (a port check is therefore not a usable
wake signal — only real data is).

After `NIGHT_MODE_AFTER_POLLS` (3) polls with no usable data (timeout
*or* `None` *or* empty `sgs/pv`), the plugin enters night-mode:

- Most poll ticks become an **instant no-op** (zero network traffic),
  emitting an empty `PollResult` so the registry counter still ticks
  but the device is never queried.
- Every `NIGHT_PROBE_INTERVAL_S` (600 s) **one real poll** runs as a
  probe — if it returns production data the plugin logs
  `hoymiles_woke_up` and resumes normal polling; otherwise it stays
  asleep.

Net effect at night: ~6 queries/hour instead of ~120/hour. Dawn
detection latency is up to 10 min.

## Power limiting — EEPROM rate-limit

`set_power_limit` writes the inverter's **EEPROM** on every call (the
upstream README warns about wear). Two guards in the plugin:

| Guard | Constant | Effect |
|-------|----------|--------|
| Dead-time | `DEAD_TIME_S = 300` | No more than one limit write per 5 min |
| Min-delta | `MIN_LIMIT_DELTA_PCT = 2.0` | Skip nudges < 2 % from the last value |

`force=True` bypasses dead-time only (still honours bounds). The
plugin declares `throttle mode = proportional` but with a high
cooldown — `compute_throttle_score()` puts it at 5.0, below
radio-Hoymiles-via-OpenDTU (7.0) and SolarEdge (9.7) but above
binary Shelly (2.8) — the distributor reaches for it only after the
freely-modulatable inverters.

## Discovery & control

- `POST /api/hoymiles/probe` — confirm an HMS at a given host (TCP
  10081 + protobuf probe).
- `POST /api/hoymiles/discover` — sweep a subnet for open 10081
  ports, then protobuf-probe each. Accepts a `subnet` override for
  remote / VPN networks. Already-configured hosts are flagged
  `already_added` so the UI can grey them out.
- `POST /api/devices/{id}/hoymiles/power` with `{"action": "restart" | "on" | "off"}`
  — soft commands to the inverter. `restart` requires the DTU serial
  (auto-adopted on the first successful poll).

## Gotchas

**`pv_data.error_code` is NOT a fault indicator.** On firmware 267
this field is a constant (`0x03090000 / 0x03000000`) and is present
even at full production. Firmware 4097 reports it as zero. Building
fault logic on this field gives false alarms (tried and reverted).

**Frozen-data condition.** An HMS unit can hang and serve bit-identical
*frozen* frames while still accepting reads. Symptoms: identical
`ac_power_w` / `dc_voltage_v` across many minutes (real production
fluctuates every minute). A `restart` command un-sticks the firmware
even though it returns no ack — "failed" / `resp=None` can be a
false negative; check the data afterwards. As a worst case, the unit
power-cycles itself at dusk (DC drops, DTU loses power) and
cold-boots fresh at dawn.

**Restart commands return no ack.** `async_reboot_inverter` /
`async_turn_off_inverter` send a command but the device frequently
does not respond with an ack — the library returns `None`. This
shows up as `success=False, error=""` in the API. The command often
*did* take effect; verify by observing the data, not the ack.
