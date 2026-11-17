# SENEC.Home Integration

Monitoring plugin for SENEC.Home battery / PV appliances. SENEC is read
into the proxy as a PV inverter (SunSpec Model 103) so its production is
visible on the dashboard and counted in the Fronius aggregate served to
Venus OS.

## Local API — `lala.cgi`

SENEC exposes an undocumented local JSON API — the same endpoint its own
web UI uses:

```
POST https://<senec-ip>/lala.cgi
Content-Type: application/json

{"SECTION": {"FIELD": ""}}
```

- HTTPS only (older HTTP access was disabled by SENEC firmware).
- Self-signed certificate — TLS verification is disabled (`ssl=False`).
- An empty-string value asks the box to fill that field in its response.
- A list field (e.g. per-phase, per-string) is requested the same way and
  comes back as a JSON array.

### Value encoding

Every value is a type-tagged string. `decode_senec_value()` handles:

| Prefix | Meaning | Decode |
|--------|---------|--------|
| `fl_`  | IEEE-754 float32 | `struct.unpack(">f", bytes.fromhex(payload))` |
| `u1_` `u3_` `u6_` `u8_` | unsigned integer | `int(payload, 16)` |
| `i1_` `i3_` `i6_` `i8_` | signed integer | `int(payload, 16)` + two's complement |
| `st_` `ch_` | string | payload as-is |
| `VARIABLE_NOT_FOUND` | field absent on this firmware | `None` |

## Fields polled

| Section / field | Use |
|-----------------|-----|
| `ENERGY.GUI_INVERTER_POWER` | PV production (W) → SunSpec AC power |
| `ENERGY.GUI_HOUSE_POW` | House consumption (W) |
| `ENERGY.GUI_GRID_POW` | Grid power (W; negative = export) |
| `ENERGY.GUI_BAT_DATA_POWER/_FUEL_CHARGE/_VOLTAGE/_CURRENT` | Battery power / SoC / V / A |
| `ENERGY.STAT_STATE` | System-state code (see `SENEC_STATE_NAMES`) |
| `ENERGY.STAT_HOURS_OF_OPERATION` | Operating hours |
| `PV1.MPP_POWER/_VOL/_CUR` | Per-string DC power / voltage / current (3 strings) |
| `PM1OBJ1.U_AC/_FREQ` | Grid voltage / frequency |
| `TEMPMEASURE.CASE_TEMP` | Case temperature → SunSpec cabinet temp |
| `TEMPMEASURE.MCU_TEMP` | Controller / power-electronics temperature → SunSpec heat-sink temp |
| `TEMPMEASURE.BATTERY_TEMP` | Battery temperature |
| `FACTORY.DEVICE_ID` | Serial |
| `SYS_UPDATE.NPU_VER/_UPDATE_AVAILABLE` | Firmware version / update flag |

`BATTERY_TEMP` reports the sentinel `-126 °C` when no battery is present
or the storage subsystem is shut down — surfaced as "offline".

## No STATISTIC section — energy is integrated locally

Recent SENEC firmware **removed the `STATISTIC` section** entirely, so
there is no lifetime energy counter (`LIVE_PV_GEN` etc. return
`VARIABLE_NOT_FOUND`). The plugin therefore **integrates AC power over
time** and persists the running total to `senec_energy.json` in the
config directory, keyed by device id — so the SunSpec energy register
survives a proxy restart. Poll gaps longer than 10 min are skipped to
avoid booking a bogus increment after an outage.

## Monitoring only

SENEC exposes no PV-curtailment control over the local API, so the
plugin declares `throttle mode = none`: it never joins the curtailment
waterfall and `write_power_limit()` is a no-op.

`PV1.POWER_RATIO` (and `_L1/_L2/_L3`) was investigated as a curtailment
setpoint and **ruled out** (researched 2026-05-21 against pysenec,
marq24/ha-senec-v3 and evcc):

- `POWER_RATIO` reads a constant `100 %` — it is inverter telemetry,
  not a writable setpoint. `ha-senec-v3`, the most complete SENEC
  integration, explicitly does not use `POWER_RATIO` at all.
- The `lala.cgi` write surface (`set_string_value` / `set_number_value`)
  only covers controllable sockets, the wallbox and safe-charge — there
  is no PV power-limit field.
- SENEC export limiting ("peak shaving" / `einspeisebegrenzungKwpInPercent`)
  exists **only via the SENEC cloud web API** at daily granularity —
  unusable as a real-time curtailment control.

Conclusion: SENEC remains monitoring-only.

## Discovery & polling

- `POST /api/senec/probe` — confirm a SENEC at a given host.
- `POST /api/senec/discover` — sweep a subnet (TCP 443) then confirm
  each open host via `lala.cgi`. Accepts a `subnet` override for
  remote / VPN networks.
- Poll interval is the global default unless the per-device
  `poll_interval` field overrides it. SENEC tolerates 1 s polling.
