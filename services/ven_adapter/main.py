"""VEN / OpenEVSE adapter: poll VTN events, map to integer amps, report status."""

from __future__ import annotations

import logging
import os
import signal
import threading
from dataclasses import dataclass, field

import paho.mqtt.client as mqtt

from home_ev_flex import mqtt_topics as topics
from home_ev_flex.amperage import AmpController
from home_ev_flex.openadr import create_ven_client, read_active_flex_signals
from home_ev_flex.tariff import load_tariff_config, solar_surplus_kw

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("ven_adapter")

# Tariff engine publishes this sentinel PRICE when no supply blocks clear the bid.
UNECONOMIC_SENTINEL = 100.0


@dataclass
class LocalState:
    mode: str = "economic"
    bid_price_per_kwh: float = 0.16
    user_amp_limit: int = 32
    voltage_v: float = 240.0
    solar_kw: float = 0.0
    house_load_kw: float = 0.0
    actual_power_kw: float = 0.0
    energy_kwh: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise RuntimeError(f"Missing required env var {name}")
    return value


class VenAdapter:
    def __init__(self) -> None:
        self.tariff_path = _env("TARIFF_CONFIG", "/config/tariff.yaml")
        self.cfg = load_tariff_config(self.tariff_path)
        limits = self.cfg.limits
        hard_max = min(limits.evse_max_amps, limits.branch_max_amps)
        self.amps = AmpController(
            i_min_amps=limits.i_min_amps,
            i_max_amps=hard_max,
            hysteresis_amps=limits.amp_hysteresis_amps,
        )
        self.state = LocalState(voltage_v=limits.default_voltage_v)
        self.poll_seconds = float(_env("VEN_INTERVAL_SEC", "3"))
        self.mqtt_host = _env("MQTT_HOST", "mosquitto")
        self.mqtt_port = int(_env("MQTT_PORT", "1883"))
        self.vtn_url = _env("VTN_BASE_URL", "http://vtn:3000")
        self.token_url = _env("OAUTH_TOKEN_URL", f"{self.vtn_url}/auth/token")
        self.client_id = _env("VEN_CLIENT_ID", "ven-client-client-id")
        self.client_secret = _env("VEN_CLIENT_SECRET", "ven-client")
        self._stop = threading.Event()
        self._ven = None
        self._mqtt = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="ven-adapter")
        self._mqtt.on_connect = self._on_connect
        self._mqtt.on_message = self._on_message

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:  # noqa: ANN001
        logger.info("MQTT connected rc=%s", reason_code)
        for topic in (
            topics.MODE,
            topics.BID_PRICE,
            topics.USER_AMP_LIMIT,
            topics.VOLTAGE_V,
            topics.SOLAR_KW,
            topics.HOUSE_LOAD_KW,
            topics.OPENEVSE_POWER_KW,
            topics.OPENEVSE_ENERGY_KWH,
        ):
            client.subscribe(topic)

    def _on_message(self, client, userdata, msg) -> None:  # noqa: ANN001
        payload = msg.payload.decode("utf-8").strip()
        with self.state.lock:
            if msg.topic == topics.MODE:
                self.state.mode = payload.lower()
            elif msg.topic == topics.BID_PRICE:
                self.state.bid_price_per_kwh = float(payload)
            elif msg.topic == topics.USER_AMP_LIMIT:
                self.state.user_amp_limit = int(float(payload))
            elif msg.topic == topics.VOLTAGE_V:
                self.state.voltage_v = float(payload)
            elif msg.topic == topics.SOLAR_KW:
                self.state.solar_kw = float(payload)
            elif msg.topic == topics.HOUSE_LOAD_KW:
                self.state.house_load_kw = float(payload)
            elif msg.topic == topics.OPENEVSE_POWER_KW:
                self.state.actual_power_kw = float(payload)
            elif msg.topic == topics.OPENEVSE_ENERGY_KWH:
                self.state.energy_kwh = float(payload)

    def _ensure_ven(self) -> None:
        if self._ven is None:
            self._ven = create_ven_client(
                vtn_base_url=self.vtn_url,
                client_id=self.client_id,
                client_secret=self.client_secret,
                token_url=self.token_url,
            )

    def _economic_target_kw(self) -> tuple[float, float | None, bool]:
        """
        Map OpenADR PRICE + IMPORT_CAPACITY_LIMIT plus local surplus to kW.

        PRICE is the effective marginal of the highest accepted block (or a high
        sentinel when nothing clears the bid). Import limit is the grid-import
        portion only; solar-first power comes from measured surplus.
        """
        with self.state.lock:
            bid = self.state.bid_price_per_kwh
            voltage = self.state.voltage_v
            user_amps = self.state.user_amp_limit
            surplus = solar_surplus_kw(
                solar_kw=self.state.solar_kw,
                house_load_kw=self.state.house_load_kw,
            )

        event_price: float | None = None
        import_limit = 0.0
        try:
            self._ensure_ven()
            signals = read_active_flex_signals(self._ven)
            event_price = signals.get("price")
            import_limit = float(signals.get("import_power_limit_kw") or 0.0)
        except Exception:  # noqa: BLE001
            logger.exception("VTN poll failed")
            return 0.0, None, False

        if event_price is None or event_price > bid or event_price >= UNECONOMIC_SENTINEL:
            return 0.0, event_price, False

        # Solar-first: surplus at export opportunity cost, plus allowed import.
        target = surplus + max(0.0, import_limit)
        user_kw = (min(user_amps, self.amps.i_max_amps) * voltage) / 1000.0
        headroom = self.cfg.limits.panel_service_headroom_kw
        target = min(target, user_kw, headroom)
        return max(0.0, target), event_price, target > 0

    def _tick(self) -> None:
        with self.state.lock:
            mode = self.state.mode
            user_amps = self.state.user_amp_limit
            voltage = self.state.voltage_v

        override = mode == "charge_now"
        event_price: float | None = None
        event_accepted = False
        if override:
            cmd = self.amps.charge_now(user_amps)
        else:
            target_kw, event_price, event_accepted = self._economic_target_kw()
            cmd = self.amps.command_for_power(target_kw, voltage)

        self._mqtt.publish(topics.OPENEVSE_CURRENT_LIMIT, str(cmd.amps), qos=1, retain=True)
        self._mqtt.publish(topics.STATUS_TARGET_AMPS, str(cmd.amps), qos=0, retain=True)
        self._mqtt.publish(topics.STATUS_OVERRIDE, "true" if override else "false", qos=0, retain=True)
        self._mqtt.publish(topics.STATUS_MODE, mode, qos=0, retain=True)
        self._mqtt.publish(
            topics.STATUS_EVENT_ACCEPTED,
            "true" if event_accepted else "false",
            qos=0,
            retain=True,
        )

        logger.info(
            "mode=%s cmd=%sA reason=%s price=%s override=%s",
            mode,
            cmd.amps,
            cmd.reason,
            event_price,
            override,
        )

    def run(self) -> None:
        self._mqtt.connect(self.mqtt_host, self.mqtt_port, 60)
        self._mqtt.loop_start()
        logger.info("VEN adapter running interval=%ss", self.poll_seconds)
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:  # noqa: BLE001
                logger.exception("tick failed")
            self._stop.wait(self.poll_seconds)
        self._mqtt.loop_stop()
        self._mqtt.disconnect()

    def stop(self, *_args) -> None:
        self._stop.set()


def main() -> None:
    adapter = VenAdapter()
    signal.signal(signal.SIGTERM, adapter.stop)
    signal.signal(signal.SIGINT, adapter.stop)
    adapter.run()


if __name__ == "__main__":
    main()
