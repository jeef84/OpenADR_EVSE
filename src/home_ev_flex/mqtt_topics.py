"""MQTT topic contract between Home Assistant, tariff engine, VEN, and OpenEVSE."""

PREFIX = "home_ev_flex"

# Telemetry published by HA (or lab fixtures)
SOLAR_KW = f"{PREFIX}/telemetry/solar_kw"
HOUSE_LOAD_KW = f"{PREFIX}/telemetry/house_load_kw"
GRID_IMPORT_KW = f"{PREFIX}/telemetry/grid_import_kw"
GRID_EXPORT_KW = f"{PREFIX}/telemetry/grid_export_kw"
VOLTAGE_V = f"{PREFIX}/telemetry/voltage_v"
# Electricity Maps (or equivalent) grid dirtiness for carbon-priced import
CO2_INTENSITY = f"{PREFIX}/telemetry/co2_intensity_g_per_kwh"
FOSSIL_FUEL_PCT = f"{PREFIX}/telemetry/fossil_fuel_pct"

# User controls from HA helpers
# Mode published by HA: economic | solar_only | charge_now | stopped
MODE = f"{PREFIX}/control/mode"
BID_PRICE = f"{PREFIX}/control/bid_price_per_kwh"
USER_AMP_LIMIT = f"{PREFIX}/control/user_amp_limit"

# Status mirrored by services for HA dashboards
STATUS_TARGET_AMPS = f"{PREFIX}/status/target_amps"
STATUS_ACCEPTED_KW = f"{PREFIX}/status/accepted_power_kw"
STATUS_MARGINAL_PRICE = f"{PREFIX}/status/marginal_price"
STATUS_IMPORT_LIMIT_KW = f"{PREFIX}/status/import_power_limit_kw"
STATUS_OVERRIDE = f"{PREFIX}/status/override_active"
STATUS_MODE = f"{PREFIX}/status/mode"
STATUS_EVENT_ACCEPTED = f"{PREFIX}/status/event_accepted"
STATUS_CARBON_ADDER = f"{PREFIX}/status/carbon_adder_per_kwh"
STATUS_EFFECTIVE_IMPORT_PRICE = f"{PREFIX}/status/effective_import_price_per_kwh"

# OpenEVSE command / telemetry (lab fixture or real broker bridge)
OPENEVSE_CURRENT_LIMIT = "openevse/cmd/current_limit"  # integer amps; 0 = stop
OPENEVSE_POWER_KW = "openevse/status/power_kw"
OPENEVSE_ENERGY_KWH = "openevse/status/energy_kwh"
OPENEVSE_APPLIED_AMPS = "openevse/status/applied_current_limit"
OPENEVSE_CONNECTED = "openevse/status/connected"
