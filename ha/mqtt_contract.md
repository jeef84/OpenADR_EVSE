# Home Assistant MQTT topic contract (Milestone 1)

## Topology

HOME EV FLEX controls **OpenEVSE only** via the Linux stack:

```text
HA telemetry/controls -> Mosquitto -> tariff engine -> VTN -> VEN
                                                      |
                                                      v
                                         openevse/cmd/current_limit
                                                      |
                                                      v
                                         openevse_bridge (claim/override MQTT; optional RAPI)
                                                      |
                                                      v
                                                   OpenEVSE
```

HA (`ha/packages/home_ev_flex.yaml`) publishes site telemetry and user controls.
It does **not** write EVSE amp setpoints. Edit the package `SITE CONFIG` placeholders
(`sensor.YOUR_SOLAR_PRODUCTION_KW`, grid import/export entity IDs) for your site.

If you also run a separate solar-divert controller on another charger, keep those
control paths isolated so two automations do not fight over the same EVSE.

## Modes

| Mode | VEN behavior |
| --- | --- |
| `economic` | Charge from supply-curve / OpenADR price + import limit; ready-by overlay may force |
| `solar_only` | Excess solar only via VEN `export − import + EV` (EMA-smoothed; ignores IMPORT_POWER_LIMIT); ready-by overlay may force |
| `charge_now` | Integer user amp limit (ignores price) |
| `stopped` | Always command **0 A** |

### Ready-by-departure overlay

On `economic` / `solar_only`, when remaining energy cannot finish in the remaining **off-peak** hours before the **next** daily ready-by clock (tomorrow if today’s time has already passed), VEN force-charges at `user_amp_limit` (ignores bid/carbon). Never forces weekday on-peak grid import (`deadline_reason=force_wait_off_peak`); solar/economic continue until off-peak. Hard amp limits still apply. Once effective SOC reaches the sticky target, those modes command **0 A** (`charge_now` still bypasses the target ceiling).

Sticky site defaults (not per plug-in): battery **74.7 kWh**, target **85%**, ready-by **07:00** local. Per plug-in: parked SOC on `telemetry/soc_pct` (or future OEM). Missing/zero SOC uses assumed **40%** so the overlay stays active.

While charging, VEN raises `status/effective_soc_pct` by integrating OpenEVSE power (`ev_kw × dt`), not the parked helper and not the often-stale OpenEVSE `/wh` meter. The parked slider stays at what you set; watch **Effective SOC** for live progress. Accrual is retained on `status/soc_tracking` (including a `target_met` latch) so a VEN rebuild does not forget progress and restart charging.

On re-plug (`openevse/status/connected` false→true), HA adjusts parked SOC from absence length:

| Gone | Parked SOC |
| --- | --- |
| ≤ 30 min | keep previous |
| 30–45 min | subtract 5 kWh (as % of battery capacity) |
| 45–60 min | subtract 10 kWh |
| > 60 min | set to **40%** |

| Topic | Direction | Meaning |
| --- | --- | --- |
| `home_ev_flex/telemetry/soc_pct` | HA → VEN | Per-session SOC % |
| `home_ev_flex/control/target_soc_pct` | HA → VEN | Sticky target % |
| `home_ev_flex/control/battery_capacity_kwh` | HA → VEN | Sticky pack kWh |
| `home_ev_flex/control/ready_by_time` | HA → VEN | Sticky daily `HH:MM` |
| `home_ev_flex/control/ready_by_enabled` | HA → VEN | Overlay master (`true`/`false`) |
| `home_ev_flex/status/effective_soc_pct` | VEN → HA | Tracked / assumed SOC |
| `home_ev_flex/status/energy_needed_kwh` | VEN → HA | Remaining energy |
| `home_ev_flex/status/slack_hours` | VEN → HA | Slack before ready-by |
| `home_ev_flex/status/deadline_force_active` | VEN → HA | `true` when forcing |
| `home_ev_flex/status/deadline_reason` | VEN → HA | `ok` / `force` / `force_wait_off_peak` / `soc_assumed` / `inactive` |

## Abstract command contract

| Topic | Direction | Meaning |
| --- | --- | --- |
| `openevse/cmd/current_limit` | VEN → bridge | Integer amps. `0` = stop. `1-5` must resolve to stop. |
| `openevse/status/power_kw` | bridge → HA/VEN | Actual EV power |
| `openevse/status/energy_kwh` | bridge → HA/VEN | Delivered energy |
| `openevse/status/applied_current_limit` | bridge → HA | Last applied setpoint |
| `openevse/status/connected` | bridge → HA | Vehicle/EVSE session connected |

Bridge hardware mapping (`OPENEVSE_CONTROL`, default `claim`; base topic default `openevse`):

| Commanded amps | `claim` (default) | `override` | `rapi` (legacy) |
| --- | --- | --- | --- |
| 0 (or 1–5 / invalid) | disable **both** `{base}/claim/set` and `{base}/override/set` with `{"state":"disabled",…}` | same dual disable | `{base}/rapi/in/$FS` |
| ≥ 6 | clear override, then claim `active` + amps | release claim, then override `active` + amps | `$FC` then `$SC {n}` |

Stop always quiets **both** claim and override so a leftover MQTT claim cannot hold the 6 A floor after an override-only clear (the failure mode behind a persistent UI `mqtt` badge at 6 A).

FLEX also publishes `{base}/divertmode/set` → `1` (Normal) on charge/stop. OpenEVSE **Eco divert** can claim at priority 1100 and beat MQTT (500), which leaves **SETPOINT at ~6 A** while **Max Current** stays 32 A. This OpenEVSE is FLEX-owned; leave gateway divert on Normal / Fast, not Eco. Enphase Soleil can still do solar follow on its own charger.

The bridge ignores **retained** `openevse/cmd/current_limit` (HA convenience retain); only live VEN publishes change hardware. That avoids a brief stale 32 A pulse on bridge reconnect.

`OPENEVSE_STOP_MODE=disabled` (default) keeps FLEX ownership while forcing sleep. `release` / `clear` yields both channels to Auto/Eco and can leave the EVSE charging at the 6 A floor.

Leave the OpenEVSE UI on **Auto**. Prefer `claim` so the UI Manual button can still interrupt FLEX. Use `override` only if you want FLEX to own the Manual path. Avoid `rapi` on modern firmware; it fights Manual/Auto.

Set `OPENEVSE_MQTT_BASE` in `compose/.env` to match the OpenEVSE WiFi gateway.

## HA helpers

- `input_boolean.home_ev_flex_enabled`
- `input_boolean.home_ev_flex_ready_by_enabled` (default on)
- `input_number.home_ev_flex_bid_price`
- `input_number.home_ev_flex_user_amp_limit` (6–48)
- `input_number.home_ev_flex_voltage_v`
- `input_number.home_ev_flex_parked_soc` (per plug-in; 0 → VEN assumes 40%; auto-adjusted on re-plug by absence)
- `input_number.home_ev_flex_target_soc` (sticky; default 85)
- `input_number.home_ev_flex_battery_capacity_kwh` (sticky; default 74.7)
- `input_datetime.home_ev_flex_ready_by_time` (sticky daily clock; default 07:00)
- `input_datetime.home_ev_flex_unplugged_time` (set on disconnect for SOC adjust)
- `input_select.home_ev_flex_mode`: `economic`, `solar_only`, `charge_now`, `stopped`

## Telemetry HA publishes

| Topic | Source |
| --- | --- |
| `home_ev_flex/telemetry/solar_kw` | Site solar production (kW) |
| `home_ev_flex/telemetry/house_load_kw` | solar + import − export − OpenEVSE kW |
| `home_ev_flex/telemetry/grid_*_kw` | Site grid CT / meter sensors |
| `home_ev_flex/telemetry/voltage_v` | `input_number.home_ev_flex_voltage_v` |
| `home_ev_flex/telemetry/soc_pct` | Parked SOC helper (or future OEM entity) |
| `home_ev_flex/telemetry/co2_intensity_g_per_kwh` | Electricity Maps CO2 intensity (when available) |
| `home_ev_flex/telemetry/fossil_fuel_pct` | Electricity Maps fossil fuel % (when available) |
| `home_ev_flex/control/*` | helpers above |

### Carbon-priced import (optional)

When `carbon_price.enabled` is true in `config/tariff.yaml`, the tariff engine adds a
$/kWh overlay to the **grid_import** supply-curve block only:

`effective_import = TOU_import + max(co2_adder, fossil_adder)`

Each signal is a hard permit gate: at or below threshold → adder $0; above →
`max_adder_per_kwh`. Solar export-credit blocks are unchanged. Your bid still decides
acceptance when the gate permits.

Status topics (tariff engine → HA):

| Topic | Meaning |
| --- | --- |
| `home_ev_flex/status/carbon_adder_per_kwh` | Active carbon overlay ($/kWh) |
| `home_ev_flex/status/effective_import_price_per_kwh` | TOU + carbon adder |

If carbon is enabled and no MQTT reading has arrived, `unavailable_behavior: max_adder`
(default) applies the configured max adder so the stack does not silently import on a
dirty or unknown grid. HA only publishes Electricity Maps values when the sensors are
available (last retained value is kept).

VEN derives solar-only surplus as `export − import + EV` (not a raw solar−house MQTT pair).

## Lab fixtures

```bash
docker compose -f compose/docker-compose.yml stop mqtt-fixtures
```

Scenarios: `worked_stack`, `solar_only`, `charge_now`, `stopped`, `below_imin`, `deadline_force`.
