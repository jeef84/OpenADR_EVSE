"""Tariff / business-logic service: MQTT in, supply curve, OpenADR BL upserts."""

from __future__ import annotations

import logging
import os
import signal
import threading
from dataclasses import dataclass, field

import paho.mqtt.client as mqtt

from home_ev_flex import mqtt_topics as topics
from home_ev_flex.openadr import create_bl_client, ensure_program, upsert_flex_event
from home_ev_flex.supply_curve import build_supply_curve, dispatch
from home_ev_flex.tariff import load_tariff_config, resolve_import_price, solar_surplus_kw

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("tariff_engine")


@dataclass
class TelemetryState:
    solar_kw: float = 0.0
    house_load_kw: float = 0.0
    grid_import_kw: float = 0.0
    grid_export_kw: float = 0.0
    voltage_v: float = 240.0
    mode: str = "economic"
    bid_price_per_kwh: float = 0.16
    user_amp_limit: int = 32
    lock: threading.Lock = field(default_factory=threading.Lock)


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise RuntimeError(f"Missing required env var {name}")
    return value


class TariffEngine:
    def __init__(self) -> None:
        self.tariff_path = _env("TARIFF_CONFIG", "/config/tariff.yaml")
        self.cfg = load_tariff_config(self.tariff_path)
        self.state = TelemetryState(voltage_v=self.cfg.limits.default_voltage_v)
        self.poll_seconds = float(_env("ENGINE_INTERVAL_SEC", "5"))
        self.mqtt_host = _env("MQTT_HOST", "mosquitto")
        self.mqtt_port = int(_env("MQTT_PORT", "1883"))
        self.vtn_url = _env("VTN_BASE_URL", "http://vtn:3000")
        self.token_url = _env("OAUTH_TOKEN_URL", f"{self.vtn_url}/auth/token")
        self.client_id = _env("BL_CLIENT_ID", "bl-client")
        self.client_secret = _env("BL_CLIENT_SECRET", "bl-client")
        self._stop = threading.Event()
        self._event_id: str | None = None
        self._program_id: str | None = None
        self._bl = None
        self._mqtt = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="tariff-engine")
        self._mqtt.on_connect = self._on_connect
        self._mqtt.on_message = self._on_message

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:  # noqa: ANN001
        logger.info("MQTT connected rc=%s", reason_code)
        for topic in (
            topics.SOLAR_KW,
            topics.HOUSE_LOAD_KW,
            topics.GRID_IMPORT_KW,
            topics.GRID_EXPORT_KW,
            topics.VOLTAGE_V,
            topics.MODE,
            topics.BID_PRICE,
            topics.USER_AMP_LIMIT,
        ):
            client.subscribe(topic)

    def _on_message(self, client, userdata, msg) -> None:  # noqa: ANN001
        payload = msg.payload.decode("utf-8").strip()
        with self.state.lock:
            if msg.topic == topics.SOLAR_KW:
                self.state.solar_kw = float(payload)
            elif msg.topic == topics.HOUSE_LOAD_KW:
                self.state.house_load_kw = float(payload)
            elif msg.topic == topics.GRID_IMPORT_KW:
                self.state.grid_import_kw = float(payload)
            elif msg.topic == topics.GRID_EXPORT_KW:
                self.state.grid_export_kw = float(payload)
            elif msg.topic == topics.VOLTAGE_V:
                self.state.voltage_v = float(payload)
            elif msg.topic == topics.MODE:
                self.state.mode = payload.lower()
            elif msg.topic == topics.BID_PRICE:
                self.state.bid_price_per_kwh = float(payload)
            elif msg.topic == topics.USER_AMP_LIMIT:
                self.state.user_amp_limit = int(float(payload))

    def _ensure_vtn(self) -> None:
        if self._bl is None:
            self._bl = create_bl_client(
                vtn_base_url=self.vtn_url,
                client_id=self.client_id,
                client_secret=self.client_secret,
                token_url=self.token_url,
            )
        if self._program_id is None:
            self._program_id = ensure_program(self._bl)

    def _tick(self) -> None:
        from datetime import datetime

        with self.state.lock:
            solar = self.state.solar_kw
            house = self.state.house_load_kw
            bid = self.state.bid_price_per_kwh
            user_amps = self.state.user_amp_limit
            voltage = self.state.voltage_v

        now = datetime.now().astimezone()
        import_price = resolve_import_price(self.cfg, now)
        surplus = solar_surplus_kw(solar_kw=solar, house_load_kw=house)
        limits = self.cfg.limits
        user_kw = (user_amps * voltage) / 1000.0
        evse_kw = (min(limits.evse_max_amps, limits.branch_max_amps) * voltage) / 1000.0

        curve = build_supply_curve(
            solar_surplus_kw=surplus,
            export_credit_per_kwh=self.cfg.export_credit_per_kwh,
            import_price_per_kwh=import_price,
            panel_service_headroom_kw=limits.panel_service_headroom_kw,
        )
        result = dispatch(
            curve,
            bid_price_per_kwh=bid,
            evse_maximum_kw=evse_kw,
            vehicle_maximum_kw=evse_kw,
            panel_service_headroom_kw=limits.panel_service_headroom_kw,
            user_charging_limit_kw=user_kw,
        )

        try:
            self._ensure_vtn()
            self._event_id = upsert_flex_event(
                self._bl,
                program_id=self._program_id,
                marginal_price=result.effective_marginal_price,
                import_power_limit_kw=result.import_power_limit_kw,
                existing_event_id=self._event_id,
            )
        except Exception:  # noqa: BLE001
            logger.exception("VTN upsert failed; publishing MQTT diagnostics only")

        status = {
            topics.STATUS_ACCEPTED_KW: f"{result.accepted_power_kw:.4f}",
            topics.STATUS_MARGINAL_PRICE: (
                "" if result.effective_marginal_price is None else f"{result.effective_marginal_price:.6f}"
            ),
            topics.STATUS_IMPORT_LIMIT_KW: f"{result.import_power_limit_kw:.4f}",
        }
        for topic, value in status.items():
            self._mqtt.publish(topic, value, qos=0, retain=True)

        logger.info(
            "dispatch accepted=%.3f kW price=%s import_limit=%.3f surplus=%.3f import_tou=%.3f",
            result.accepted_power_kw,
            result.effective_marginal_price,
            result.import_power_limit_kw,
            surplus,
            import_price,
        )

    def run(self) -> None:
        self._mqtt.connect(self.mqtt_host, self.mqtt_port, 60)
        self._mqtt.loop_start()
        logger.info("Tariff engine running interval=%ss", self.poll_seconds)
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
    engine = TariffEngine()
    signal.signal(signal.SIGTERM, engine.stop)
    signal.signal(signal.SIGINT, engine.stop)
    engine.run()


if __name__ == "__main__":
    main()
