# Home Assistant MQTT topic contract (Milestone 1)

## Topology

- HA Green publishes telemetry and user helpers to Mosquitto.
- Linux host runs VTN, tariff engine, and VEN (see `compose/`).
- VEN publishes OpenEVSE current limit and status topics HA can display.

## Required MQTT topics

| Topic | Direction | Payload | Notes |
| --- | --- | --- | --- |
| `home_ev_flex/telemetry/solar_kw` | HA → broker | float | Site solar production (kW) |
| `home_ev_flex/telemetry/house_load_kw` | HA → broker | float | House load excluding EV (kW) |
| `home_ev_flex/telemetry/grid_import_kw` | HA → broker | float | Optional diagnostic |
| `home_ev_flex/telemetry/grid_export_kw` | HA → broker | float | Optional diagnostic |
| `home_ev_flex/telemetry/voltage_v` | HA → broker | float | Measured EVSE voltage; default 240 |
| `home_ev_flex/control/mode` | HA → broker | `economic` or `charge_now` | Mode selector |
| `home_ev_flex/control/bid_price_per_kwh` | HA → broker | float | Max energy price ($/kWh) |
| `home_ev_flex/control/user_amp_limit` | HA → broker | integer | User amp ceiling |
| `home_ev_flex/status/accepted_power_kw` | tariff → HA | float | Accepted continuous kW |
| `home_ev_flex/status/marginal_price` | tariff → HA | float | Highest accepted block $/kWh |
| `home_ev_flex/status/import_power_limit_kw` | tariff → HA | float | Grid-import portion for EV |
| `home_ev_flex/status/target_amps` | VEN → HA | integer | Commanded CP amps (0 = stop) |
| `home_ev_flex/status/override_active` | VEN → HA | `true`/`false` | Charge Now active |
| `home_ev_flex/status/mode` | VEN → HA | string | Echo of active mode |
| `home_ev_flex/status/event_accepted` | VEN → HA | `true`/`false` | Economic event accepted |
| `openevse/cmd/current_limit` | VEN → EVSE | integer | OpenEVSE current limit; 0 stops |
| `openevse/status/power_kw` | EVSE → broker | float | Actual EV power |
| `openevse/status/energy_kwh` | EVSE → broker | float | Delivered energy |

## HA helpers (suggested)

- `input_number.ev_bid_price` ($/kWh), default `0.16`
- `input_number.ev_user_amp_limit` (A), default `32`
- `input_select.ev_charge_mode`: `economic`, `charge_now`

Publish helper state changes to the control topics above (MQTT statestream, automations, or Node-RED).

## OpenADR mapping

Program name: `HOME_EV_FLEX`

| Plan signal | OpenADR 3.1 payload type | Units |
| --- | --- | --- |
| PRICE | `PRICE` | USD per `KWH` |
| IMPORT_POWER_LIMIT | `IMPORT_CAPACITY_LIMIT` | `KW` |

## Lab fixtures

Without live HA hardware:

```bash
cd compose
docker compose up
# mqtt-fixtures publishes the worked_stack scenario by default
```

Scenarios: `worked_stack`, `charge_now`, `below_imin` (see `services/mqtt_fixtures/publish.py`).
