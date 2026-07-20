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
| `carbon_price` | Optional: inflate grid import $/kWh from Electricity Maps |
| `limits.*` | Site / EVSE hard limits and amp hysteresis |

Include variable per-kWh surcharges in the fully loaded import prices. Exclude fixed monthly charges.

### Carbon price overlay

Optional. When enabled, HA publishes Electricity Maps sensors and the tariff engine
raises the **grid import** block price (solar blocks stay at export credit):

```yaml
carbon_price:
  enabled: true
  unavailable_behavior: max_adder  # or zero
  co2_intensity:
    threshold_g_per_kwh: 580       # permit at or below (site-specific "good")
    max_adder_per_kwh: 0.50        # full adder when above threshold
  fossil_fuel_pct:
    threshold_pct: 80
    max_adder_per_kwh: 0.50
```

Each signal is a **hard gate**: `value <= threshold` → adder $0; `value > threshold` →
`max_adder_per_kwh`. Final carbon adder is the **max** of available signal adders, so
either dirty CO2 or dirty fossil can block grid import. Solar blocks are unchanged.

Example with a MISO-like baseline (580 g / 80%): at 580/80 adder is **$0** (TOU vs bid
decides). At 592 g (above) adder is **$0.50**, so off-peak $0.14 becomes $0.64 and fails
a $0.16 bid.

## Examples

| File | Utility |
| --- | --- |
| `config/examples/dte.yaml` | DTE Energy (Michigan) TOU + Rider 18-style export credit |
| `config/tariff.yaml` | Generic starter (same numbers as the M1 worked example) |

Add new files under `config/examples/` when you contribute another utility's baseline.

## Future: real-time published prices

Most utilities still do not publish machine-readable real-time residential prices. When they do, `price_source` will select a provider plugin (ISO LMP, utility API, Green Button Connect, etc.) while keeping the same supply-curve and OpenADR path. See the Milestone 1 plan "Future work" section.
