# Local OpenADR 3.1 EVSE testbed (HOME_EV_FLEX)

Turns editable utility tariff config plus Home Assistant MQTT telemetry into OpenEVSE integer amp limits via a marginal-cost supply curve, OpenLEADR VTN (OpenADR 3.1), and a Python VEN adapter.

Milestone 1: economic mode + Charge Now. No forecasting, SOC, or ready-by-departure yet.

## Architecture

```text
HA/MQTT fixtures → tariff engine → OpenLEADR VTN → VEN adapter → OpenEVSE amps
```

- **Tariff engine**: utility-agnostic TOU + export opportunity cost, supply curve, upserts `PRICE` + `IMPORT_CAPACITY_LIMIT` on program `HOME_EV_FLEX`.
- **VEN**: polls events, owns floor quantization + amp hysteresis, commands integer amps (never 1–5 A).
- **Config**: `config/tariff.yaml` (copy/adapt examples under `config/examples/`).

## Quick start

```bash
cd compose
docker compose up --build
```

In another terminal:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
lab-e2e --host localhost
```

Default fixtures (`worked_stack`): solar surplus 3 kW @ $0.07, import $0.18, bid $0.16 → accepted 3 kW → **12 A** at 240 V.

## Docs

- [Runbook](docs/runbook.md)
- [Tariff config (any utility)](docs/tariff-config.md)
- [HA MQTT contract](ha/mqtt_contract.md)
- [Milestone 1 plan](mdlib/milestone1-plan.md)

## Layout

```text
compose/                 # Docker Compose: VTN, Postgres, Mosquitto, services
config/tariff.yaml       # Active site tariff (utility-agnostic schema)
config/examples/         # Shipped baselines (e.g. DTE) to copy and edit
src/home_ev_flex/        # Supply curve, amps, OpenADR helpers
services/tariff_engine/  # BL client
services/ven_adapter/    # VEN + OpenEVSE command
services/mqtt_fixtures/  # Lab HA stand-in
ha/                      # Topic contract
tests/                   # Intent-focused unit tests
scripts/lab_e2e.py       # Closed-loop lab check
```
