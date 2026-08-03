# Adaptive carbon learning state (runtime)

The tariff engine bind-mounts this directory and writes:

| File | Meaning |
| --- | --- |
| `carbon_history.json` | Off-peak CO2 / fossil samples (14-day lookback) |
| `carbon_history.html` | Historical chart view (regenerated on save, auto-refreshes every 60s) |

Open `carbon_history.html` in a browser (or via the IDE Simple Browser).

**Persistence:** Survives `docker compose stop` and `docker compose down`.
Deleted only if you remove this directory yourself.

JSON/HTML files here are gitignored (site-local runtime data).
