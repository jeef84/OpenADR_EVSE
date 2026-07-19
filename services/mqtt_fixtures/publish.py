"""Lab MQTT fixture publisher: mimics HA sensors for offline demos."""

from __future__ import annotations

import argparse
import json
import logging
import os
import time

import paho.mqtt.client as mqtt

from home_ev_flex import mqtt_topics as topics

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("mqtt_fixtures")

SCENARIOS = {
    "worked_stack": {
        topics.SOLAR_KW: "5.0",
        topics.HOUSE_LOAD_KW: "2.0",  # surplus 3.0 kW
        topics.GRID_IMPORT_KW: "0.0",
        topics.GRID_EXPORT_KW: "3.0",
        topics.VOLTAGE_V: "240.0",
        topics.MODE: "economic",
        topics.BID_PRICE: "0.16",
        topics.USER_AMP_LIMIT: "32",
    },
    "charge_now": {
        topics.SOLAR_KW: "0.0",
        topics.HOUSE_LOAD_KW: "1.0",
        topics.GRID_IMPORT_KW: "1.0",
        topics.GRID_EXPORT_KW: "0.0",
        topics.VOLTAGE_V: "240.0",
        topics.MODE: "charge_now",
        topics.BID_PRICE: "0.05",
        topics.USER_AMP_LIMIT: "24",
    },
    "below_imin": {
        topics.SOLAR_KW: "2.0",
        topics.HOUSE_LOAD_KW: "1.2",  # surplus 0.8 kW -> ~3.3 A at 240 V -> stop
        topics.GRID_IMPORT_KW: "0.0",
        topics.GRID_EXPORT_KW: "0.8",
        topics.VOLTAGE_V: "240.0",
        topics.MODE: "economic",
        topics.BID_PRICE: "0.16",
        topics.USER_AMP_LIMIT: "32",
    },
}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Publish HA-like MQTT fixtures for HOME_EV_FLEX")
    parser.add_argument("--host", default=os.environ.get("MQTT_HOST", "localhost"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MQTT_PORT", "1883")))
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), default=os.environ.get("FIXTURE_SCENARIO", "worked_stack"))
    parser.add_argument("--once", action="store_true", help="Publish once and exit")
    parser.add_argument("--interval", type=float, default=5.0)
    args = parser.parse_args(argv)

    payload = SCENARIOS[args.scenario]
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="mqtt-fixtures")
    client.connect(args.host, args.port, 60)
    client.loop_start()
    logger.info("Publishing scenario=%s to %s:%s", args.scenario, args.host, args.port)

    try:
        while True:
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
