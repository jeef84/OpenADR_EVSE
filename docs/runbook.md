# Runbook: Local OpenADR 3.1 EVSE testbed

## What runs where

| Host | Components |
| --- | --- |
| Linux VM / PC | Postgres, OpenLEADR VTN, Mosquitto, tariff engine, VEN adapter, MQTT fixtures |
| Home Assistant Green | Sensors, helpers, dashboard (production path) |

## Quick start (lab)

```bash
cd compose
docker compose up --build
```

Then in another shell:

```bash
cd ..
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
lab-e2e --host localhost --port 1883
```

Expected for default `worked_stack` fixtures: accepted ~3.0 kW, OpenEVSE command **12 A** at 240 V.

## Credentials (lab fixtures)

Loaded from `compose/openleadr/users.sql` after VTN migrations:

| Role | client_id | client_secret |
| --- | --- | --- |
| Business logic | `bl-client` | `bl-client` |
| VEN | `ven-client-client-id` | `ven-client` |

VTN HTTP: `http://localhost:3000`

## Unit tests (no Docker)

```bash
pytest
```

## Tariff edits

Edit `config/tariff.yaml` (or copy from `config/examples/`) and restart `tariff-engine` / `ven-adapter`. See [tariff-config.md](tariff-config.md). Rates are placeholders; replace from your utility rate card.

## Switching fixture scenarios

```bash
FIXTURE_SCENARIO=charge_now docker compose up mqtt-fixtures
# or
mqtt-fixtures --host localhost --scenario below_imin --once
```

## Safety note

Charge Now bypasses price only. Hard amp limits in `config/tariff.yaml` (`evse_max_amps`, `branch_max_amps`, `i_min_amps`) still apply. Hardware GFCI / temperature protection remain with OpenEVSE.
