"""Lab MQTT fixture publisher: mimics HA sensors for offline demos."""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import paho.mqtt.client as mqtt

from home_ev_flex import mqtt_topics as topics

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("mqtt_fixtures")

# Clean-grid defaults so lab fixtures are not fail-closed when carbon_price.enabled.
_CLEAN_CARBON = {
    topics.CO2_INTENSITY: "120.0",
    topics.FOSSIL_FUEL_PCT: "25.0",
}

# Sticky ready-by defaults (match tariff.yaml); scenarios may override.
_READY_BY_DEFAULTS = {
    topics.SOC_PCT: "0",
    topics.TARGET_SOC_PCT: "85",
    topics.BATTERY_CAPACITY_KWH: "74.7",
    topics.READY_BY_TIME: "07:00",
    topics.READY_BY_ENABLED: "true",
}

# Dirty grid: carbon adder blocks typical bids so deadline force is the only path.
_DIRTY_CARBON = {
    topics.CO2_INTENSITY: "700.0",
    topics.FOSSIL_FUEL_PCT: "90.0",
}


def _base(**overrides: str) -> dict[str, str]:
    payload = {
        topics.SOLAR_KW: "0.0",
        topics.HOUSE_LOAD_KW: "1.0",
        topics.GRID_IMPORT_KW: "1.0",
        topics.GRID_EXPORT_KW: "0.0",
        topics.VOLTAGE_V: "240.0",
        topics.MODE: "economic",
        topics.BID_PRICE: "0.16",
        topics.USER_AMP_LIMIT: "32",
        **_CLEAN_CARBON,
        **_READY_BY_DEFAULTS,
    }
    payload.update(overrides)
    return payload


SCENARIOS = {
    "worked_stack": _base(
        **{
            topics.SOLAR_KW: "5.0",
            topics.HOUSE_LOAD_KW: "2.0",
            topics.GRID_IMPORT_KW: "0.0",
            topics.GRID_EXPORT_KW: "3.0",
        }
    ),
    "solar_only": _base(
        **{
            topics.SOLAR_KW: "5.0",
            topics.HOUSE_LOAD_KW: "3.0",
            topics.GRID_IMPORT_KW: "0.0",
            topics.GRID_EXPORT_KW: "2.0",
            topics.MODE: "solar_only",
            topics.BID_PRICE: "0.50",
        }
    ),
    "charge_now": _base(
        **{
            topics.MODE: "charge_now",
            topics.BID_PRICE: "0.05",
            topics.USER_AMP_LIMIT: "24",
        }
    ),
    "stopped": _base(
        **{
            topics.SOLAR_KW: "5.0",
            topics.HOUSE_LOAD_KW: "2.0",
            topics.GRID_IMPORT_KW: "0.0",
            topics.GRID_EXPORT_KW: "3.0",
            topics.MODE: "stopped",
            topics.BID_PRICE: "0.50",
        }
    ),
    "below_imin": _base(
        **{
            topics.SOLAR_KW: "2.0",
            topics.HOUSE_LOAD_KW: "1.2",
            topics.GRID_IMPORT_KW: "0.0",
            topics.GRID_EXPORT_KW: "0.8",
        }
    ),
    # Carbon/price would idle; ready-by overdue + assumed SOC → deadline_force.
    "deadline_force": _base(
        **{
            topics.MODE: "economic",
            topics.BID_PRICE: "0.16",
            topics.USER_AMP_LIMIT: "32",
            topics.SOC_PCT: "0",
            topics.READY_BY_ENABLED: "true",
            **_DIRTY_CARBON,
        }
    ),
}


def _deadline_force_ready_by_hhmm(*, timezone_name: str = "America/Detroit") -> str:
    """Publish a ready-by clock already past in site TZ so overlay is overdue."""
    local = datetime.now(ZoneInfo(timezone_name)) - timedelta(hours=1)
    return local.strftime("%H:%M")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Publish HA-like MQTT fixtures for HOME_EV_FLEX")
    parser.add_argument("--host", default=os.environ.get("MQTT_HOST", "localhost"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MQTT_PORT", "1883")))
    parser.add_argument(
        "--scenario",
        choices=sorted(SCENARIOS),
        default=os.environ.get("FIXTURE_SCENARIO", "worked_stack"),
    )
    parser.add_argument("--once", action="store_true", help="Publish once and exit")
    parser.add_argument("--interval", type=float, default=5.0)
    args = parser.parse_args(argv)

    payload = dict(SCENARIOS[args.scenario])
    if args.scenario == "deadline_force":
        payload[topics.READY_BY_TIME] = _deadline_force_ready_by_hhmm()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="mqtt-fixtures")
    client.connect(args.host, args.port, 60)
    client.loop_start()
    logger.info("Publishing scenario=%s to %s:%s", args.scenario, args.host, args.port)

    try:
        while True:
            if args.scenario == "deadline_force":
                payload[topics.READY_BY_TIME] = _deadline_force_ready_by_hhmm()
            for topic, value in payload.items():
                client.publish(topic, value, qos=0, retain=True)
            # Simulate OpenEVSE telemetry for reports/dashboards.
            client.publish(topics.OPENEVSE_POWER_KW, "0.0", qos=0, retain=True)
            client.publish(topics.OPENEVSE_ENERGY_KWH, "0.0", qos=0, retain=True)
            logger.info("published %s", json.dumps(payload))
            if args.once:
                break
            time.sleep(args.interval)
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
