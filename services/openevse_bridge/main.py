"""OpenEVSE MQTT bridge: abstract current_limit -> claim/override (or legacy RAPI)."""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time

import paho.mqtt.client as mqtt

from home_ev_flex import mqtt_topics as topics

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("openevse_bridge")

# Preferred: claim (automation client). override mimics UI Manual. rapi is legacy.
CONTROL_MODES = frozenset({"claim", "override", "rapi"})


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


def gateway_state_from_announce(payload: str) -> bool | None:
    """Parse OpenEVSE announce JSON. True/False online; None if unusable."""
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    state = str(data.get("state", "")).lower()
    if state == "connected":
        return True
    if state == "disconnected":
        return False
    return None


def session_connected(*, status: str, vehicle: str | None, state: str | None) -> bool:
    """
    Vehicle/EVSE session connected from OpenEVSE MQTT fields.

    Prefer vehicle=1 when present. Fall back to status/state used by OpenEVSE
    WiFi (status=active while enabled/charging; state>=1 while session alive).
    """
    if vehicle is not None:
        return vehicle.strip() == "1"
    low = status.lower().strip()
    if low in {"disabled", "sleeping", "unknown", ""}:
        return False
    if low in {"active", "connected"} or "charg" in low or "connect" in low:
        return True
    if state is not None and state.strip().isdigit():
        return int(state.strip()) >= 1
    return False


def _stop_payload(stop_mode: str, *, auto_release: bool) -> str:
    """
    How FLEX holds a stop.

    disabled (default): keep MQTT ownership and force sleep. Needed so Auto/Eco
    cannot resume at I_min after we give up the claim.
    release: drop claim/override and return control to the OpenEVSE UI mode.
    """
    mode = stop_mode.lower().strip()
    if mode == "release":
        return "release"
    if mode == "clear":
        return "clear"
    if mode != "disabled":
        raise ValueError(f"unknown stop_mode {stop_mode!r}; use disabled|release|clear")
    return json.dumps(
        {"state": "disabled", "auto_release": auto_release},
        separators=(",", ":"),
    )


def control_command(
    mode: str,
    amps: int,
    *,
    base_topic: str,
    auto_release: bool = True,
    stop_mode: str = "disabled",
) -> list[tuple[str, str]]:
    """
    Build MQTT (topic, payload) pairs for the requested amp setpoint.

    claim/override: OpenEVSE WiFi Claims / Manual Override API over MQTT.
    On stop, both claim and override are forced quiet so a leftover MQTT claim
    cannot keep the 6 A floor while override is cleared (or vice versa).
    rapi: legacy $FS / $FC / $SC (fights Manual UI; keep for old firmware).
    """
    mode = mode.lower().strip()
    if mode not in CONTROL_MODES:
        raise ValueError(f"unknown OPENEVSE_CONTROL {mode!r}; expected one of {sorted(CONTROL_MODES)}")
    base = base_topic.rstrip("/")

    if mode == "rapi":
        if amps <= 0:
            return [(f"{base}/rapi/in/$FS", "")]
        return [
            (f"{base}/rapi/in/$FC", ""),
            (f"{base}/rapi/in/$SC {amps}", ""),
        ]

    claim_topic = f"{base}/claim/set"
    override_topic = f"{base}/override/set"
    divert_topic = f"{base}/divertmode/set"
    active = json.dumps(
        {
            "state": "active",
            "charge_current": amps,
            "max_current": amps,
            "auto_release": auto_release,
        },
        separators=(",", ":"),
    )
    disabled = _stop_payload("disabled", auto_release=auto_release)

    if amps <= 0:
        if stop_mode == "release" or stop_mode == "clear":
            return [(claim_topic, "release"), (override_topic, "clear")]
        # Hold both channels disabled so neither Auto/Eco nor a stale claim/override
        # can resume at I_min. Also force divert Normal so Eco cannot re-claim at 6A.
        return [
            (divert_topic, "1"),
            (claim_topic, disabled),
            (override_topic, disabled),
        ]

    if mode == "claim":
        # Normal divert (1): Eco divert can claim at Priority_Limit (1100) and beat
        # MQTT (500), leaving SETPOINT stuck at ~6A despite Max Current 32.
        return [
            (divert_topic, "1"),
            (override_topic, "clear"),
            (claim_topic, active),
        ]

    # override: drop MQTT claim so the UI mqtt badge cannot stick at 6 A.
    return [
        (divert_topic, "1"),
        (claim_topic, "release"),
        (override_topic, active),
    ]


class OpenEvseBridge:
    def __init__(self) -> None:
        self.mqtt_host = _env("MQTT_HOST", "mosquitto")
        self.mqtt_port = int(_env("MQTT_PORT", "1883"))
        self.base_topic = _env("OPENEVSE_MQTT_BASE", "openevse").rstrip("/")
        self.i_min = int(_env("OPENEVSE_I_MIN", "6"))
        self.i_max = int(_env("OPENEVSE_I_MAX", "48"))
        # claim = automation client (default); override = Manual path; rapi = legacy.
        self.control = _env("OPENEVSE_CONTROL", "claim").lower().strip()
        if self.control not in CONTROL_MODES:
            raise RuntimeError(
                f"OPENEVSE_CONTROL={self.control!r} invalid; use one of {sorted(CONTROL_MODES)}"
            )
        self.auto_release = _env("OPENEVSE_AUTO_RELEASE", "true").lower() in {"1", "true", "yes"}
        # disabled (default): force stop while keeping claim. release/clear: yield to Auto/Eco.
        self.stop_mode = _env("OPENEVSE_STOP_MODE", "disabled").lower().strip()
        if self.stop_mode not in {"disabled", "release", "clear"}:
            raise RuntimeError(
                f"OPENEVSE_STOP_MODE={self.stop_mode!r} invalid; use disabled|release|clear"
            )
        # No device MQTT for this long => gateway offline; clear stale power.
        self.offline_sec = float(_env("OPENEVSE_OFFLINE_SEC", "60"))
        self._desired = 0
        self._last_sent: int | None = None
        self._applied = 0
        self._gateway_online = False
        self._last_device_seen = 0.0
        self._evse_status = ""
        self._evse_state: str | None = None
        self._vehicle: str | None = None
        self._stop = threading.Event()
        self._mqtt = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="openevse-bridge")
        self._mqtt.on_connect = self._on_connect
        self._mqtt.on_message = self._on_message

    def _device_topics(self) -> set[str]:
        base = self.base_topic
        return {
            f"{base}/amp",
            f"{base}/power",
            f"{base}/wh",
            f"{base}/status",
            f"{base}/state",
            f"{base}/vehicle",
            f"{base}/time",
            f"{base}/rapi/out",
            f"{base}/pilot",
            f"{base}/claim",
            f"{base}/override",
        }

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:  # noqa: ANN001
        logger.info(
            "MQTT connected rc=%s base=%s control=%s",
            reason_code,
            self.base_topic,
            self.control,
        )
        client.subscribe(topics.OPENEVSE_CURRENT_LIMIT)
        for topic in (*sorted(self._device_topics()), f"{self.base_topic}/announce/#"):
            client.subscribe(topic)

    def _publish_status(self, *, applied: int | None = None) -> None:
        if applied is not None:
            self._applied = applied
        self._mqtt.publish(topics.OPENEVSE_APPLIED_AMPS, str(self._applied), qos=0, retain=True)

    def _publish_session_connected(self) -> None:
        connected = self._gateway_online and session_connected(
            status=self._evse_status,
            vehicle=self._vehicle,
            state=self._evse_state,
        )
        self._mqtt.publish(
            topics.OPENEVSE_CONNECTED,
            "true" if connected else "false",
            qos=0,
            retain=True,
        )

    def _note_device_seen(self) -> None:
        self._last_device_seen = time.monotonic()
        if not self._gateway_online:
            self._set_gateway_online(True)

    def _set_gateway_online(self, online: bool) -> None:
        if online == self._gateway_online:
            return
        self._gateway_online = online
        if online:
            logger.info("OpenEVSE gateway online; re-applying %sA", self._desired)
            self._apply(self._desired, force=True)
            self._publish_session_connected()
        else:
            logger.warning("OpenEVSE gateway offline; clearing stale power")
            self._mqtt.publish(topics.OPENEVSE_CONNECTED, "false", qos=0, retain=True)
            self._mqtt.publish(topics.OPENEVSE_POWER_KW, "0.0000", qos=0, retain=True)

    def _check_liveness(self) -> None:
        if not self._gateway_online or self._last_device_seen <= 0.0:
            return
        age = time.monotonic() - self._last_device_seen
        if age > self.offline_sec:
            self._set_gateway_online(False)

    def _apply(self, amps: int, *, force: bool = False) -> None:
        self._desired = amps
        if not self._gateway_online:
            logger.debug("OpenEVSE gateway offline; deferring %sA", amps)
            return
        if not force and amps == self._last_sent:
            return

        commands = control_command(
            self.control,
            amps,
            base_topic=self.base_topic,
            auto_release=self.auto_release,
            stop_mode=self.stop_mode,
        )
        for topic, payload in commands:
            self._mqtt.publish(topic, payload, qos=1, retain=False)
        if amps <= 0:
            logger.info(
                "OpenEVSE stop via %s control=%s stop_mode=%s payloads=%s",
                [c[0] for c in commands],
                self.control,
                self.stop_mode,
                [c[1] for c in commands],
            )
            self._last_sent = 0
            self._publish_status(applied=0)
            return

        logger.info(
            "OpenEVSE set %s A via %s control=%s payloads=%s",
            amps,
            [c[0] for c in commands],
            self.control,
            [c[1] for c in commands],
        )
        self._last_sent = amps
        self._publish_status(applied=amps)

    def _on_message(self, client, userdata, msg) -> None:  # noqa: ANN001
        payload = msg.payload.decode("utf-8", errors="replace").strip()
        if msg.topic == topics.OPENEVSE_CURRENT_LIMIT:
            # VEN retains current_limit for HA; a stale retained 32 A on bridge
            # reconnect briefly re-enables charging. Act on live publishes only.
            if getattr(msg, "retain", False):
                logger.info("ignoring retained current_limit %r", payload)
                return
            amps = normalize_amps(payload, i_min=self.i_min, i_max=self.i_max)
            self._apply(amps)
            return

        if msg.topic.startswith(f"{self.base_topic}/announce/"):
            online = gateway_state_from_announce(payload)
            if online is False:
                self._set_gateway_online(False)
            elif online is True:
                self._note_device_seen()
            return

        if msg.topic in self._device_topics():
            self._note_device_seen()

        if msg.topic == f"{self.base_topic}/power" and payload:
            if not self._gateway_online:
                return
            try:
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
        elif msg.topic == f"{self.base_topic}/status" and payload:
            self._evse_status = payload
            self._publish_session_connected()
        elif msg.topic == f"{self.base_topic}/state" and payload:
            self._evse_state = payload
            self._publish_session_connected()
        elif msg.topic == f"{self.base_topic}/vehicle" and payload:
            self._vehicle = payload
            self._publish_session_connected()

    def run(self) -> None:
        self._mqtt.connect(self.mqtt_host, self.mqtt_port, 60)
        self._mqtt.loop_start()
        logger.info(
            "OpenEVSE bridge running host=%s:%s base=%s control=%s limits=%s-%sA offline_sec=%s",
            self.mqtt_host,
            self.mqtt_port,
            self.base_topic,
            self.control,
            self.i_min,
            self.i_max,
            self.offline_sec,
        )
        while not self._stop.wait(2.0):
            self._check_liveness()
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
