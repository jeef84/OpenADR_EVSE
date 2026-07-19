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
| `economic` | Charge from supply-curve / OpenADR price + import limit |
| `solar_only` | Excess solar only via VEN `export − import + EV` (EMA-smoothed; ignores IMPORT_POWER_LIMIT) |
| `charge_now` | Integer user amp limit (ignores price) |
| `stopped` | Always command **0 A** |

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
- `input_number.home_ev_flex_bid_price`
- `input_number.home_ev_flex_user_amp_limit` (6–48)
- `input_number.home_ev_flex_voltage_v`
- `input_select.home_ev_flex_mode`: `economic`, `solar_only`, `charge_now`, `stopped`

## Telemetry HA publishes

| Topic | Source |
| --- | --- |
| `home_ev_flex/telemetry/solar_kw` | Site solar production (kW) |
| `home_ev_flex/telemetry/house_load_kw` | solar + import − export − OpenEVSE kW |
| `home_ev_flex/telemetry/grid_*_kw` | Site grid CT / meter sensors |
| `home_ev_flex/telemetry/voltage_v` | `input_number.home_ev_flex_voltage_v` |
| `home_ev_flex/control/*` | helpers above |

VEN derives solar-only surplus as `export − import + EV` (not a raw solar−house MQTT pair).

## Lab fixtures

```bash
docker compose -f compose/docker-compose.yml stop mqtt-fixtures
```

Scenarios: `worked_stack`, `solar_only`, `charge_now`, `stopped`, `below_imin`.
