"""OpenEVSE MQTT RAPI bridge: abstract current_limit -> hardware commands."""

from __future__ import annotations

import logging
import os
import signal
import threading

import paho.mqtt.client as mqtt

from home_ev_flex import mqtt_topics as topics

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("openevse_bridge")


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise RuntimeError(f"Missing required env var {name}")
    return value


def normalize_amps(raw: str, *, i_min: int, i_max: int) -> int:
    """
    Map VEN command to a safe OpenEVSE amp setpoint.

    0           -> stop
    1..i_min-1  -> stop (never send 1-5 A for J1772)
    invalid     -> stop
    else        -> clamp to [i_min, i_max]
    """
    try:
        amps = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        logger.warning("invalid current_limit payload %r; failing safe to 0 A", raw)
        return 0
    if amps <= 0:
        return 0
    if amps < i_min:
        return 0
    return min(amps, i_max)


class OpenEvseBridge:
    def __init__(self) -> None:
        self.mqtt_host = _env("MQTT_HOST", "mosquitto")
        self.mqtt_port = int(_env("MQTT_PORT", "1883"))
        self.base_topic = _env("OPENEVSE_MQTT_BASE", "openevse").rstrip("/")
        self.i_min = int(_env("OPENEVSE_I_MIN", "6"))
        self.i_max = int(_env("OPENEVSE_I_MAX", "48"))
        self._applied = 0
        self._stop = threading.Event()
        self._mqtt = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="openevse-bridge")
        self._mqtt.on_connect = self._on_connect
        self._mqtt.on_message = self._on_message

    def _rapi_in(self, command: str) -> str:
        # OpenEVSE expects the full RAPI token in the topic, e.g. .../rapi/in/$SC 16
        return f"{self.base_topic}/rapi/in/{command}"

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:  # noqa: ANN001
        logger.info("MQTT connected rc=%s base=%s", reason_code, self.base_topic)
        client.subscribe(topics.OPENEVSE_CURRENT_LIMIT)
        # Optional hardware status feeds (OpenEVSE WiFi gateway defaults).
        client.subscribe(f"{self.base_topic}/amp")
        client.subscribe(f"{self.base_topic}/power")
        client.subscribe(f"{self.base_topic}/wh")
        client.subscribe(f"{self.base_topic}/status")
        client.subscribe(f"{self.base_topic}/pilot")

    def _publish_status(self, *, applied: int | None = None) -> None:
        if applied is not None:
            self._applied = applied
        self._mqtt.publish(topics.OPENEVSE_APPLIED_AMPS, str(self._applied), qos=0, retain=True)

    def _apply(self, amps: int) -> None:
        if amps <= 0:
            # Sleep / disable charging. Hardware retains GFCI and safety logic.
            topic = self._rapi_in("$FS")
            self._mqtt.publish(topic, "", qos=1, retain=False)
            logger.info("OpenEVSE stop via %s", topic)
            self._publish_status(applied=0)
            return

        enable = self._rapi_in("$FC")
        set_cur = self._rapi_in(f"$SC {amps}")
        self._mqtt.publish(enable, "", qos=1, retain=False)
        self._mqtt.publish(set_cur, "", qos=1, retain=False)
        logger.info("OpenEVSE enable + set %s A via %s", amps, set_cur)
        self._publish_status(applied=amps)

    def _on_message(self, client, userdata, msg) -> None:  # noqa: ANN001
        payload = msg.payload.decode("utf-8", errors="replace").strip()
        if msg.topic == topics.OPENEVSE_CURRENT_LIMIT:
            amps = normalize_amps(payload, i_min=self.i_min, i_max=self.i_max)
            self._apply(amps)
            return

        # Mirror hardware telemetry into the abstract status contract when present.
        if msg.topic == f"{self.base_topic}/power" and payload:
            try:
                # OpenEVSE often reports watts.
                watts = float(payload)
                self._mqtt.publish(topics.OPENEVSE_POWER_KW, f"{watts / 1000.0:.4f}", qos=0, retain=True)
            except ValueError:
                logger.warning("bad power payload %r", payload)
        elif msg.topic == f"{self.base_topic}/wh" and payload:
            try:
                wh = float(payload)
                self._mqtt.publish(topics.OPENEVSE_ENERGY_KWH, f"{wh / 1000.0:.4f}", qos=0, retain=True)
            except ValueError:
                logger.warning("bad wh payload %r", payload)
        elif msg.topic == f"{self.base_topic}/amp" and payload:
            try:
                self._publish_status(applied=int(float(payload)))
            except ValueError:
                logger.warning("bad amp payload %r", payload)
        elif msg.topic == f"{self.base_topic}/status" and payload:
            # Common states include Connected / Charging / Sleeping / etc.
            connected = payload.lower() not in {"ready", "sleeping", "disabled", "unknown", ""}
            # More precise: treat "Connected" and "Charging*" as connected.
            low = payload.lower()
            connected = ("connect" in low) or ("charg" in low)
            self._mqtt.publish(
                topics.OPENEVSE_CONNECTED,
                "true" if connected else "false",
                qos=0,
                retain=True,
            )

    def run(self) -> None:
        self._mqtt.connect(self.mqtt_host, self.mqtt_port, 60)
        self._mqtt.loop_start()
        logger.info(
            "OpenEVSE bridge running host=%s:%s base=%s limits=%s-%sA",
            self.mqtt_host,
            self.mqtt_port,
            self.base_topic,
            self.i_min,
            self.i_max,
        )
        self._stop.wait()
        self._mqtt.loop_stop()
        self._mqtt.disconnect()

    def stop(self, *_args) -> None:
        self._stop.set()


def main() -> None:
    bridge = OpenEvseBridge()
    signal.signal(signal.SIGTERM, bridge.stop)
    signal.signal(signal.SIGINT, bridge.stop)
    bridge.run()


if __name__ == "__main__":
    main()
