# Configuring your utility tariff

Tariff economics live in YAML. Nothing in the engine hard-codes a specific utility.

## Active config

1. Copy an example (or start from `config/tariff.yaml`).
2. Edit `utility`, `timezone`, TOU windows, import prices, and export credit.
3. Point the stack at it with `TARIFF_CONFIG` (Compose default: `/config/tariff.yaml`).

```bash
cp config/examples/dte.yaml config/tariff.yaml
# edit rates from your utility rate card
```

## Schema (Milestone 1)

| Field | Meaning |
| --- | --- |
| `utility` | Display / log label only |
| `timezone` | IANA zone for TOU windows |
| `rate_schedule` | Optional human label (e.g. `D1.2`, `E-TOU-C`) |
| `price_source` | `static_yaml` today; future realtime providers later |
| `import_rates.weekday.on_peak` | Local HH:MM window + `$/kWh` |
| `import_rates.weekday.off_peak` | `$/kWh` outside on-peak on weekdays |
| `import_rates.weekend.all_day` | `$/kWh` Sat/Sun |
| `export.credit_per_kwh` | Opportunity cost of consuming otherwise-exported solar |
| `limits.*` | Site / EVSE hard limits and amp hysteresis |

Include variable per-kWh surcharges in the fully loaded import prices. Exclude fixed monthly charges.

## Examples

| File | Utility |
| --- | --- |
| `config/examples/dte.yaml` | DTE Energy (Michigan) TOU + Rider 18-style export credit |
| `config/tariff.yaml` | Generic starter (same numbers as the M1 worked example) |

Add new files under `config/examples/` when you contribute another utility's baseline.

## Future: real-time published prices

Most utilities still do not publish machine-readable real-time residential prices. When they do, `price_source` will select a provider plugin (ISO LMP, utility API, Green Button Connect, etc.) while keeping the same supply-curve and OpenADR path. See the Milestone 1 plan "Future work" section.
