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
| `ready_by.*` | Deadline overlay sticky defaults (battery, target SOC, daily clock) |
| `limits.*` | Site / EVSE hard limits, `peak_demand_limit_kw`, amp hysteresis |

Include variable per-kWh surcharges in the fully loaded import prices. Exclude fixed monthly charges.

### Carbon price overlay

Optional. When enabled, HA publishes Electricity Maps sensors and the tariff engine
raises the **grid import** block price (solar blocks stay at export credit):

```yaml
carbon_price:
  enabled: true
  unavailable_behavior: max_adder  # or zero
  co2_intensity:
    threshold_g_per_kwh: 580       # YAML ceiling (permit at or below)
    min_threshold_g_per_kwh: 350   # how clean adaptive may target
    max_adder_per_kwh: 0.50        # full adder when above effective threshold
  fossil_fuel_pct:
    threshold_pct: 80
    min_threshold_pct: 50
    max_adder_per_kwh: 0.50
  adaptive:
    enabled: true
    lookback_days: 14              # off-peak samples only
    sample_interval_sec: 300
    percentile: 25                 # learned clean floor
    min_samples: 288               # cold start until enough history
    slack_high_hours: 4.0          # urgency=0 above this slack
```

Each signal is a **hard gate**: `value <= effective_threshold` → adder $0;
`value > effective_threshold` → `max_adder_per_kwh`. Final carbon adder is the **max**
of available signal adders, so either dirty CO2 or dirty fossil can block grid import.
Solar blocks are unchanged.

**Adaptive learning (optional):** YAML `threshold_*` is the **ceiling**. The tariff
engine stores downsampled **off-peak-only** readings for `lookback_days` (default 14),
takes the configured percentile (default p25) as a learned floor (clamped by
`min_threshold_*`), then lerps toward the YAML ceiling as ready-by `slack_hours`
falls from `slack_high_hours` down to `ready_by.cushion_hours`. Missing slack MQTT or
fewer than `min_samples` keeps the static YAML gate (fail closed, not over-strict).

Persisted under `data/carbon_adaptive/` (compose bind mount): `carbon_history.json`
plus `carbon_history.html` (chart view, refreshed on save, browser auto-reload 60s).
Survives `docker compose stop` / `down`. Reset by deleting that directory.

Example with a MISO-like baseline (580 g / 80%): at 580/80 adder is **$0** (TOU vs bid
decides). At 592 g (above) adder is **$0.50**, so off-peak $0.14 becomes $0.64 and fails
a $0.16 bid. With adaptive and high slack, a learned floor near 480 g can block a
514 g night that the static 580 gate would have permitted.

### Peak demand limit (price gate)

Optional. When measured demand (`solar + import − export`) is **above**
`peak_demand_limit_kw`, the tariff engine adds `peak_demand_adder_per_kwh` to the
**grid import** block price (same hard-gate idea as carbon). A typical bid then
rejects import until demand drops; solar export-credit blocks are unchanged.

```yaml
limits:
  panel_service_headroom_kw: 7.68
  peak_demand_limit_kw: 12.0          # 0 disables
  peak_demand_adder_per_kwh: 0.50     # inflate import when over
```

### Ready-by-departure defaults

Sticky site settings (HA helpers may override over MQTT). Missing/zero parked SOC uses
`assumed_soc_pct` so the overlay can still force-charge before departure:

```yaml
ready_by:
  enabled: true
  cushion_hours: 0.25
  assumed_soc_pct: 40
  battery_capacity_kwh: 74.7
  target_soc_pct: 85
  ready_by_time: "07:00"
```

## Examples

| File | Utility |
| --- | --- |
| `config/examples/dte.yaml` | DTE Energy (Michigan) TOU + Rider 18-style export credit |
| `config/tariff.yaml` | Generic starter (same numbers as the M1 worked example) |

Add new files under `config/examples/` when you contribute another utility's baseline.

## Future: real-time published prices

Most utilities still do not publish machine-readable real-time residential prices. When they do, `price_source` will select a provider plugin (ISO LMP, utility API, Green Button Connect, etc.) while keeping the same supply-curve and OpenADR path. See the Milestone 1 plan "Future work" section.
