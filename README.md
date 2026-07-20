# Local OpenADR 3.1 EVSE testbed (HOME_EV_FLEX)

Turns editable utility tariff config plus Home Assistant MQTT telemetry into OpenEVSE integer amp limits via a marginal-cost supply curve, OpenLEADR VTN (OpenADR 3.1), and a Python VEN adapter.

**Milestone 1 modes:** `economic`, `solar_only`, `charge_now`, `stopped`. No forecasting, SOC, or ready-by-departure yet.

## Table of contents

1. [Architecture](#architecture)
2. [How it works](#how-it-works)
3. [Prerequisites](#prerequisites)
4. [Clone and configure](#1-clone-and-configure)
5. [Lab stack (no Home Assistant)](#2-lab-stack-no-home-assistant)
6. [Real OpenEVSE on the same broker](#3-real-openevse-on-the-same-broker)
7. [Home Assistant (production telemetry)](#4-home-assistant-production-telemetry)
8. [Modes](#modes)
9. [Tariff configuration](#tariff-configuration)
10. [Carbon-priced import (optional)](#carbon-priced-import-optional)
11. [MQTT topic contract](#mqtt-topic-contract)
12. [OpenEVSE bridge](#openevse-bridge)
13. [Environment variables](#environment-variables)
14. [Day-2 ops](#5-day-2-ops)
15. [Testing](#testing)
16. [Safety](#safety)
17. [Repository layout](#repository-layout)
18. [Further documentation](#further-documentation)
19. [Scope and non-goals](#scope-and-non-goals)

---

## Architecture

```text
HA/MQTT fixtures → tariff engine → OpenLEADR VTN → VEN adapter → OpenEVSE amps
```

| Component | Role |
| --- | --- |
| **Home Assistant** (or fixtures) | Publishes site telemetry and user controls over MQTT. Does **not** write EVSE amp setpoints. |
| **Tariff engine** | Resolves TOU + export opportunity cost, builds supply curve, upserts `PRICE` + import power limit on program `HOME_EV_FLEX`. |
| **OpenLEADR VTN** | OpenADR 3.1 server (HTTP REST + Postgres). Standards boundary only; optimization stays outside. |
| **VEN adapter** | Polls events, maps price + local surplus/mode to integer amps (floor quantization, EMA surplus smoothing, amp hysteresis). Never commands 1-5 A. |
| **openevse_bridge** | Turns `openevse/cmd/current_limit` into OpenEVSE claim/override MQTT (default `claim`; optional legacy RAPI). |
| **Config** | `config/tariff.yaml` (copy/adapt examples under `config/examples/`). |

Deploy topology for production: Home Assistant for sensors and dashboard; a separate Linux host/VM for VTN, Postgres, Mosquitto, tariff engine, VEN, and bridge.

---

## How it works

Economics stay in **$/kWh**, not $/kW. Power (kW) is only the instantaneous operating constraint.

For each incremental watt of EV charging:

| Situation | Marginal cost |
| --- | --- |
| Consuming otherwise-exported solar | Export credit / net-metering credit from config |
| Importing from the utility | Current TOU import rate (+ optional carbon adder) |

**Worked lab example** (`worked_stack` fixtures):

| Input | Value |
| --- | ---: |
| Solar surplus | 3 kW |
| Export credit | $0.07/kWh |
| Import price | $0.18/kWh |
| Bid | $0.16/kWh |
| Voltage | 240 V |

Only the first 3 kW block clears the bid → accepted **3 kW** → **12 A** at 240 V.

---

## Prerequisites

- Docker + Docker Compose
- Python 3.11+ (unit tests and lab helpers)
- For hardware: OpenEVSE on the same MQTT broker, plus Home Assistant (or any publisher) for site telemetry

---

## 1. Clone and configure

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

Lab OAuth credentials (loaded from `compose/openleadr/users.sql`):

| Role | client_id | client_secret |
| --- | --- | --- |
| Business logic (tariff engine) | `bl-client` | `bl-client` |
| VEN | `ven-client-client-id` | `ven-client` |

VTN HTTP: `http://localhost:3000`

---

## 2. Lab stack (no Home Assistant)

```bash
cd compose
docker compose up --build
```

Starts Postgres, VTN, Mosquitto, tariff engine, VEN, openevse_bridge, and **mqtt-fixtures** (fake HA telemetry).

In another terminal:

```bash
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

Or one-shot from the host:

```bash
mqtt-fixtures --host localhost --scenario below_imin --once
```

| Scenario | Intent |
| --- | --- |
| `worked_stack` | Economic accept of 3 kW surplus → 12 A |
| `solar_only` | Surplus-only charge (~8 A); ignores cheap import |
| `charge_now` | User amp limit (24 A); ignores price |
| `stopped` | Always 0 A |
| `below_imin` | Surplus maps below 6 A → stop |

---

## 3. Real OpenEVSE on the same broker

1. Point the OpenEVSE WiFi gateway at the compose Mosquitto host (or point compose Mosquitto at your existing broker).
2. Set `OPENEVSE_MQTT_BASE` in `compose/.env` to the gateway base topic.
3. Leave OpenEVSE UI on **Auto**. Confirm bridge logs show `claim/set` (or `override/set`) when the VEN commands amps; stop should log dual disable (not a bare Auto yield).
4. Keep gateway divert on **Normal / Fast**, not Eco (Eco can reclaim at priority 1100 and leave SETPOINT ~6 A).
5. Keep `mqtt-fixtures` running only for lab demos. For live HA telemetry:

```bash
cd compose
docker compose stop mqtt-fixtures
```

---

## 4. Home Assistant (production telemetry)

1. Ensure HA can publish/subscribe to the same Mosquitto instance (MQTT integration).
2. Copy `ha/packages/home_ev_flex.yaml` into your HA `packages/` directory (enable packages in `configuration.yaml` if needed).
3. **Edit SITE CONFIG** in that file before reload:
   - Replace every `sensor.YOUR_SOLAR_PRODUCTION_KW` with your solar production entity (**kW**).
   - Point `sensor.grid_import_power` / `sensor.grid_export_power` at your grid CT sensors (**watts** in the shipped templates; drop `/ 1000` if yours are already kW).
4. Reload automations / restart HA so helpers and template sensors appear.
5. On the Linux host: fixtures stopped, stack up (`docker compose up -d --build`).
6. In HA: set mode / bid / amp limit, then turn **HOME EV FLEX Enabled** on.

Full topic contract: [ha/mqtt_contract.md](ha/mqtt_contract.md).

### HA helpers

| Helper | Purpose |
| --- | --- |
| `input_boolean.home_ev_flex_enabled` | Master enable |
| `input_number.home_ev_flex_bid_price` | Max $/kWh willing to pay |
| `input_number.home_ev_flex_user_amp_limit` | Charge Now amp setpoint (6-48) |
| `input_number.home_ev_flex_voltage_v` | Nominal voltage for kW↔A |
| `input_select.home_ev_flex_mode` | `economic` / `solar_only` / `charge_now` / `stopped` |

---

## Modes

| Mode | Behavior |
| --- | --- |
| `economic` | Accept supply-curve blocks at or below your bid; may import when TOU (+ optional carbon adder) is cheap enough |
| `solar_only` | Excess solar only (`export - import + EV`, EMA-smoothed); ignores cheap grid import and IMPORT_POWER_LIMIT |
| `charge_now` | User amp limit; ignores price |
| `stopped` | Always 0 A |

VEN never commands 1-5 A: anything below `i_min_amps` (default 6) becomes a stop.

---

## Tariff configuration

Tariff economics live in YAML. Nothing in the engine hard-codes a specific utility.

```bash
cp config/examples/dte.yaml config/tariff.yaml
# edit rates from your utility rate card
# then: docker compose restart tariff-engine ven-adapter
```

| Field | Meaning |
| --- | --- |
| `utility` | Display / log label only |
| `timezone` | IANA zone for TOU windows |
| `rate_schedule` | Optional human label (e.g. `D1.2`) |
| `price_source` | `static_yaml` today; future realtime providers later |
| `import_rates.weekday.on_peak` | Local HH:MM window + $/kWh |
| `import_rates.weekday.off_peak` | $/kWh outside on-peak on weekdays |
| `import_rates.weekend.all_day` | $/kWh Sat/Sun |
| `export.credit_per_kwh` | Opportunity cost of consuming otherwise-exported solar |
| `carbon_price` | Optional: inflate grid import $/kWh from Electricity Maps |
| `limits.*` | Site / EVSE hard limits and amp hysteresis |

Include variable per-kWh surcharges in fully loaded import prices. Exclude fixed monthly charges.

Shipped baselines:

| File | Notes |
| --- | --- |
| `config/tariff.yaml` | Active site file (generic starter / M1 worked example) |
| `config/examples/dte.yaml` | DTE Energy (Michigan) TOU + Rider 18-style export credit |

Full schema guide: [docs/tariff-config.md](docs/tariff-config.md).

---

## Carbon-priced import (optional)

When `carbon_price.enabled` is true, HA publishes Electricity Maps sensors and the tariff engine raises the **grid import** block price only (solar blocks stay at export credit):

```yaml
carbon_price:
  enabled: true
  unavailable_behavior: max_adder  # or zero
  co2_intensity:
    threshold_g_per_kwh: 580
    max_adder_per_kwh: 0.50
  fossil_fuel_pct:
    threshold_pct: 80
    max_adder_per_kwh: 0.50
```

Each signal is a hard gate: at or below threshold → adder $0; above → `max_adder_per_kwh`. Final adder is the **max** of available signals. If enabled and no MQTT reading has arrived, `unavailable_behavior: max_adder` (default) applies the max so the stack does not silently import on a dirty or unknown grid.

Status topics: `home_ev_flex/status/carbon_adder_per_kwh`, `home_ev_flex/status/effective_import_price_per_kwh`.

---

## MQTT topic contract

Prefix: `home_ev_flex/`.

| Topic | Direction | Meaning |
| --- | --- |
| `telemetry/solar_kw` | HA → engine | Site solar production (kW) |
| `telemetry/house_load_kw` | HA → engine | House load (kW) |
| `telemetry/grid_*_kw` | HA → engine | Grid import/export |
| `telemetry/voltage_v` | HA → VEN | Nominal volts |
| `telemetry/co2_intensity_g_per_kwh` | HA → engine | Optional carbon signal |
| `telemetry/fossil_fuel_pct` | HA → engine | Optional carbon signal |
| `control/mode` | HA → VEN | Mode string |
| `control/bid_price_per_kwh` | HA → engine/VEN | Bid |
| `control/user_amp_limit` | HA → VEN | Charge Now amps |
| `status/*` | services → HA | Target amps, accepted kW, prices, mode |
| `openevse/cmd/current_limit` | VEN → bridge | Integer amps (`0` = stop; `1-5` must stop) |
| `openevse/status/*` | bridge → HA/VEN | Power, energy, applied limit, connected |

VEN derives solar-only surplus as `export - import + EV` (not a raw solar-house MQTT pair).

Authoritative detail: [ha/mqtt_contract.md](ha/mqtt_contract.md). Topic constants: `src/home_ev_flex/mqtt_topics.py`.

---

## OpenEVSE bridge

Maps abstract amp commands to OpenEVSE WiFi MQTT. Set `OPENEVSE_MQTT_BASE` to match the gateway.

| Commanded amps | `claim` (default) | `override` | `rapi` (legacy) |
| --- | --- | --- | --- |
| 0 (or 1-5 / invalid) | Disable **both** `{base}/claim/set` and `{base}/override/set` | same dual disable | `{base}/rapi/in/$FS` |
| ≥ 6 | Clear override, then claim `active` + amps | Release claim, then override `active` + amps | `$FC` then `$SC {n}` |

| Env | Default | Notes |
| --- | --- | --- |
| `OPENEVSE_CONTROL` | `claim` | Prefer claim so UI Manual can interrupt FLEX |
| `OPENEVSE_STOP_MODE` | `disabled` | Force stop while keeping ownership; `release`/`clear` can leave a 6 A floor |
| `OPENEVSE_AUTO_RELEASE` | `true` | Auto-release behavior on stop path |
| `OPENEVSE_OFFLINE_SEC` | `60` | Mark gateway offline / clear stale power if silent |

The bridge ignores **retained** `openevse/cmd/current_limit` so a reconnect does not pulse a stale high amp command. FLEX also publishes `{base}/divertmode/set` → `1` (Normal) on charge/stop.

HOME EV FLEX drives OpenEVSE only. Do not point a second automation at the same charger.

---

## Environment variables

Copy `compose/.env.example` → `compose/.env` (gitignored). Key knobs:

| Variable | Purpose |
| --- | --- |
| `OAUTH_BASE64_SECRET` | VTN internal OAuth HMAC (`openssl rand -base64 32`) |
| `PG_*` | Postgres user/db/password/ports |
| `VTN_PORT` / `MQTT_PORT` | Host port mappings |
| `BL_CLIENT_*` / `VEN_CLIENT_*` | OpenADR client credentials |
| `OPENEVSE_MQTT_BASE` | Gateway MQTT base topic |
| `OPENEVSE_CONTROL` | `claim` \| `override` \| `rapi` |
| `OPENEVSE_STOP_MODE` | `disabled` \| `release` \| `clear` |
| `ENGINE_INTERVAL_SEC` | Tariff engine loop (default 5) |
| `VEN_INTERVAL_SEC` | VEN loop (default 3) |
| `VEN_SURPLUS_EMA_ALPHA` | Surplus EMA weight (default 0.2; lower = smoother) |
| `FIXTURE_SCENARIO` | Lab fixture name |

Amp hysteresis lives in config: `limits.amp_hysteresis_amps` in `config/tariff.yaml` (default 2.5 A).

---

## 5. Day-2 ops

Long-running services use `restart: unless-stopped` (crash and reboot recovery).
`vtn-init` stays one-shot. If you `docker compose stop mqtt-fixtures` for live HA,
it stays stopped across reboots until you start it again.

```bash
# Tariff / limit changes
# edit config/tariff.yaml, then:
cd compose
docker compose restart tariff-engine ven-adapter

# Watch VEN decisions (surplus, target kW, commanded amps)
docker compose logs -f ven-adapter

# Bridge claim/release chatter
docker compose logs -f openevse-bridge

# Calibrate surplus smoothing without rebuild
# VEN_SURPLUS_EMA_ALPHA in compose/.env (lower = smoother; default 0.2)
# amp_hysteresis_amps in config/tariff.yaml (default 2.5 A)
```

More ops notes: [docs/runbook.md](docs/runbook.md).

---

## Testing

```bash
# Unit tests (no Docker)
pytest

# Closed-loop lab check (stack must be up with worked_stack fixtures)
lab-e2e --host localhost --port 1883
# optional: --expect-amps 12 --timeout 60
```

CLI entry points (from `pyproject.toml` after `pip install -e ".[dev]"`):

| Command | Module |
| --- | --- |
| `tariff-engine` | `services.tariff_engine.main` |
| `ven-adapter` | `services.ven_adapter.main` |
| `openevse-bridge` | `services.openevse_bridge.main` |
| `mqtt-fixtures` | `services.mqtt_fixtures.publish` |
| `lab-e2e` | `scripts.lab_e2e` |

---

## Safety

Charge Now bypasses **price** only. Hard amp limits in `config/tariff.yaml` (`evse_max_amps`, `branch_max_amps`, `i_min_amps`) still apply. OpenEVSE keeps GFCI / thermal / contactor safety locally.

---

## Repository layout

```text
compose/                 # Docker Compose: VTN, Postgres, Mosquitto, services
compose/.env.example     # Copy to compose/.env (gitignored)
compose/openleadr/       # Lab VTN users SQL + init wait script
compose/mosquitto/       # Broker config
config/tariff.yaml       # Active site tariff (utility-agnostic schema)
config/examples/         # Shipped baselines (e.g. DTE) to copy and edit
src/home_ev_flex/        # Supply curve, amps, OpenADR helpers, surplus smoothing
services/tariff_engine/  # BL client (PRICE + import limit upserts)
services/ven_adapter/    # VEN + amp command
services/openevse_bridge/# OpenEVSE claim/override (or legacy RAPI) hardware adapter
services/mqtt_fixtures/  # Lab HA stand-in
ha/                      # Topic contract
tests/                   # Intent-focused unit tests
scripts/lab_e2e.py       # Closed-loop lab check
docs/                    # Runbook + tariff config guide
mdlib/                   # Design / milestone plans
```

Core library modules (`src/home_ev_flex/`):

| Module | Responsibility |
| --- | --- |
| `tariff.py` | YAML load, TOU resolve, carbon adder, surplus helpers |
| `supply_curve.py` | Marginal blocks + bid dispatch |
| `amperage.py` | Floor quantization, hysteresis, 1-5 A ban |
| `smoothing.py` | EMA filter for surplus |
| `openadr.py` | Program `HOME_EV_FLEX`, event upsert/read |
| `mqtt_topics.py` | Shared topic constants |

---

## Further documentation

| Doc | Contents |
| --- | --- |
| [docs/runbook.md](docs/runbook.md) | What runs where, credentials, fixture switching |
| [docs/tariff-config.md](docs/tariff-config.md) | Full tariff schema, carbon overlay, future price sources |
| [ha/mqtt_contract.md](ha/mqtt_contract.md) | HA helpers, topics, bridge mapping, divert notes |
| [mdlib/milestone1-plan.md](mdlib/milestone1-plan.md) | Milestone 1 design invariants and future work |

---

## Scope and non-goals

**In scope (Milestone 1):** local OpenADR 3.1 loop, static YAML tariffs, four charge modes, lab fixtures, real OpenEVSE MQTT control, optional carbon overlay on grid import.

**Not yet:** forecasting, vehicle SOC, ready-by-departure bidding, live utility OpenADR feeds, realtime published residential prices (`price_source` plugins), multi-EVSE coordination.
