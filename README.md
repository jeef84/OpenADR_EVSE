# Local OpenADR 3.1 EVSE testbed (HOME_EV_FLEX)

Turns editable utility tariff config plus Home Assistant MQTT telemetry into OpenEVSE integer amp limits via a marginal-cost supply curve, OpenLEADR VTN (OpenADR 3.1), and a Python VEN adapter.

Milestone 1 modes: **economic**, **solar_only**, **charge_now**, **stopped**. No forecasting, SOC, or ready-by-departure yet.

## Architecture

```text
HA or MQTT fixtures → Mosquitto → tariff engine → OpenLEADR VTN → VEN → openevse_bridge → OpenEVSE
```

- **Tariff engine**: utility-agnostic TOU + export opportunity cost, supply curve, upserts `PRICE` + import power limit on program `HOME_EV_FLEX`.
- **VEN**: polls events, maps price + local surplus to integer amps (floor quantization, EMA surplus smoothing, amp hysteresis). Never commands 1–5 A.
- **openevse_bridge**: turns `openevse/cmd/current_limit` into MQTT RAPI (`$FS` / `$FC` / `$SC`).
- **Config**: `config/tariff.yaml` (copy/adapt examples under `config/examples/`).

## Prerequisites

- Docker + Docker Compose
- Python 3.11+ (for unit tests / lab helpers)
- For hardware: OpenEVSE on the same MQTT broker, plus Home Assistant (or any publisher) for site telemetry

## 1. Clone and configure env

```bash
git clone <this-repo> OpenADR_EVSE
cd OpenADR_EVSE

cp compose/.env.example compose/.env
# Edit compose/.env:
#   - OAUTH_BASE64_SECRET  → openssl rand -base64 32
#   - OPENEVSE_MQTT_BASE   → your OpenEVSE WiFi MQTT topic prefix (often openevse)
#   - PG_PASSWORD / client secrets if this is more than a throwaway lab

cp config/examples/dte.yaml config/tariff.yaml   # or keep the starter tariff.yaml
# Edit config/tariff.yaml: timezone, TOU windows, import prices, export credit, amp limits
```

## 2. Lab stack (no Home Assistant required)

```bash
cd compose
docker compose up --build
```

This starts Postgres, VTN, Mosquitto, tariff engine, VEN, openevse_bridge, and **mqtt-fixtures** (fake HA telemetry).

In another terminal:

```bash
cd /path/to/OpenADR_EVSE
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
lab-e2e --host localhost
```

Default fixture `worked_stack`: solar surplus 3 kW @ $0.07, import $0.18, bid $0.16 → accepted 3 kW → **12 A** at 240 V.

Switch fixture scenarios:

```bash
cd compose
FIXTURE_SCENARIO=solar_only docker compose up mqtt-fixtures
# other scenarios: charge_now | stopped | below_imin
```

## 3. Real OpenEVSE on the same broker

1. Point the OpenEVSE WiFi gateway at the compose Mosquitto host (or point compose Mosquitto at your existing broker).
2. Set `OPENEVSE_MQTT_BASE` in `compose/.env` to the gateway base topic.
3. Confirm bridge logs show `$SC` / `$FS` when the VEN commands amps.
4. Keep `mqtt-fixtures` running only for lab demos. For live HA telemetry:

```bash
cd compose
docker compose stop mqtt-fixtures
```

## 4. Home Assistant (production telemetry path)

1. Ensure HA can publish/subscribe to the same Mosquitto instance (MQTT integration).
2. Copy `ha/packages/home_ev_flex.yaml` into your HA `packages/` directory (enable packages in `configuration.yaml` if needed).
3. **Edit SITE CONFIG** in that file before reload:
   - Replace every `sensor.YOUR_SOLAR_PRODUCTION_KW` with your solar production entity (**kW**).
   - Point `sensor.grid_import_power` / `sensor.grid_export_power` at your grid CT sensors (**watts** in the shipped templates; drop `/ 1000` if yours are already kW).
4. Reload automations / restart HA so helpers and template sensors appear.
5. On the Linux host: fixtures stopped, stack up (`docker compose up -d --build`).
6. In HA: set mode / bid / amp limit, then turn **HOME EV FLEX Enabled** on.

Topic contract: [ha/mqtt_contract.md](ha/mqtt_contract.md).

### Modes (quick reference)

| Mode | Behavior |
| --- | --- |
| `economic` | Accept supply-curve blocks at or below your bid; may import when TOU is cheap enough |
| `solar_only` | Excess solar only (`export − import + EV`); ignores cheap grid import |
| `charge_now` | User amp limit; ignores price |
| `stopped` | Always 0 A |

## 5. Day-2 ops

```bash
# Tariff / limit changes
# edit config/tariff.yaml, then:
cd compose
docker compose restart tariff-engine ven-adapter

# Watch VEN decisions (surplus, target kW, commanded amps)
docker compose logs -f ven-adapter

# Calibrate surplus smoothing without rebuild
# VEN_SURPLUS_EMA_ALPHA in compose/.env (lower = smoother; default 0.2)
# amp_hysteresis_amps in config/tariff.yaml (default 2.5 A)
```

## Safety

Charge Now bypasses price only. Hard amp limits in `config/tariff.yaml` (`evse_max_amps`, `branch_max_amps`, `i_min_amps`) still apply. OpenEVSE keeps GFCI / thermal / contactor safety locally.

## Docs

- [Runbook](docs/runbook.md)
- [Tariff config (any utility)](docs/tariff-config.md)
- [HA MQTT contract](ha/mqtt_contract.md)
- [Milestone 1 plan](mdlib/milestone1-plan.md)

## Layout

```text
compose/                 # Docker Compose: VTN, Postgres, Mosquitto, services
compose/.env.example     # Copy to compose/.env (gitignored)
config/tariff.yaml       # Active site tariff (utility-agnostic schema)
config/examples/         # Shipped baselines (e.g. DTE) to copy and edit
src/home_ev_flex/        # Supply curve, amps, OpenADR helpers, surplus smoothing
services/tariff_engine/  # BL client
services/ven_adapter/    # VEN + amp command
services/openevse_bridge/# OpenEVSE MQTT RAPI hardware adapter
services/mqtt_fixtures/  # Lab HA stand-in
ha/                      # Topic contract + HA package (edit SITE CONFIG)
tests/                   # Intent-focused unit tests
scripts/lab_e2e.py       # Closed-loop lab check
```
