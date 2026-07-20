# Ready-by-departure deadline overlay

Deadline overlay on `economic` / `solar_only`: prefer cheap/clean charging while slack exists; force-charge (ignore price and carbon) when remaining energy cannot finish by the daily ready-by clock.

## Locked decisions

- **SOC source:** HA parked SOC helper today; same MQTT topic (`telemetry/soc_pct`) can accept an OEM sensor later. Parked SOC is the only per-plug-in input.
- **Policy:** Overlay force at `user_amp_limit` when slack ≤ cushion. `charge_now` / `stopped` unchanged.
- **Assumed SOC:** missing/zero SOC uses `assumed_soc_pct` (default 40%) so the overlay never stays inactive for lack of a reading. Status publishes `deadline_reason=soc_assumed` (or `force` when forcing).
- **Sticky defaults:** battery **74.7 kWh**, target **85%**, ready-by **07:00** local (HA + `config/tariff.yaml` `ready_by:`). Not entered each plug-in.
- **Daily clock:** `HH:MM` in site timezone. Past today’s ready-by with energy still needed → overdue (hours_until = 0 → force).

## Math

```text
soc_for_math = soc_pct if soc_pct > 0 else assumed_soc_pct
effective_soc = soc_for_math + energy_delta_pct   # delta only after real SOC snapshot
energy_needed = max(0, (target - effective) / 100 * battery_kwh)
slack_hours = hours_until_ready_by - hours_needed
force = ready_by_enabled and energy_needed > 0 and slack_hours <= cushion_hours
```

## Key files

- `src/home_ev_flex/deadline.py`
- `services/ven_adapter/main.py`
- `ha/packages/home_ev_flex.yaml`
- `tests/test_deadline.py`
