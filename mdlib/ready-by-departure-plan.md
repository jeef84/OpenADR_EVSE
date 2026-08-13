# Ready-by-departure deadline overlay

Deadline overlay on `economic` / `solar_only`: prefer cheap/clean charging while slack exists; force-charge (ignore price and carbon) when remaining energy cannot finish by the daily ready-by clock.

## Locked decisions

- **SOC source:** HA parked SOC helper today; same MQTT topic (`telemetry/soc_pct`) can accept an OEM sensor later. Parked SOC is the only per-plug-in input.
- **Policy:** Overlay force at `user_amp_limit` when **off-peak** slack ≤ cushion. Never force grid import during weekday on-peak; wait (`force_wait_off_peak`) and keep solar/economic. `charge_now` / `stopped` unchanged.
- **Assumed SOC:** missing/zero SOC uses `assumed_soc_pct` (default 40%) so the overlay never stays inactive for lack of a reading. Status publishes `deadline_reason=soc_assumed` (or `force` / `force_wait_off_peak`).
- **Sticky defaults:** battery **74.7 kWh**, target **85%**, ready-by **07:00** local (HA + `config/tariff.yaml` `ready_by:`). Not entered each plug-in.
- **Daily clock:** `HH:MM` in site timezone. Hours-until aims at the **next** occurrence (tomorrow if today’s clock has passed) so daytime plug-ins keep economic/solar slack.
- **TOU gate:** Slack counts only off-peak hours until ready-by (weekday on-peak from tariff YAML excluded). Force runs only while currently off-peak.

## Math

```text
soc_for_math = soc_pct if soc_pct > 0 else assumed_soc_pct
effective_soc = soc_for_math + energy_delta_pct   # delta only after real SOC snapshot
energy_needed = max(0, (target - effective) / 100 * battery_kwh)
off_peak_hours = off-peak hours until next ready_by
slack_hours = off_peak_hours - hours_needed
need_force = ready_by_enabled and energy_needed > 0 and slack_hours <= cushion_hours
force = need_force and not weekday_on_peak
```

## Key files

- `src/home_ev_flex/deadline.py`
- `services/ven_adapter/main.py`
- `ha/packages/home_ev_flex.yaml`
- `tests/test_deadline.py`
