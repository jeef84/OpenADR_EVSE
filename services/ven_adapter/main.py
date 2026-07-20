"""VEN / OpenEVSE adapter: poll VTN events, map to integer amps, report status."""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time as time_module
from dataclasses import dataclass, field
from datetime import datetime, time, timezone

import paho.mqtt.client as mqtt

from home_ev_flex import mqtt_topics as topics
from home_ev_flex.amperage import AmpCommand, AmpController
from home_ev_flex.deadline import (
    effective_soc_pct,
    energy_needed_kwh,
    evaluate_deadline,
    parse_ready_by_hhmm,
)
from home_ev_flex.openadr import create_ven_client, read_active_flex_signals
from home_ev_flex.smoothing import EmaFilter
from home_ev_flex.tariff import (
    grid_net_surplus_kw,
    load_tariff_config,
    solar_only_target_kw,
)

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
    grid_import_kw: float = 0.0
    grid_export_kw: float = 0.0
    actual_power_kw: float = 0.0
    energy_kwh: float = 0.0
    soc_pct: float = 0.0
    target_soc_pct: float = 85.0
    battery_capacity_kwh: float = 74.7
    ready_by_time: time = field(default_factory=lambda: time(7, 0))
    ready_by_enabled: bool = True
    # kWh delivered since last parked-SOC change (integrated from EV power).
    energy_added_since_soc_kwh: float = 0.0
    soc_tracking_active: bool = False
    last_soc_for_snapshot: float = 0.0
    # True after retained restore applied or restore window sealed (avoid late overwrite).
    soc_tracking_restored: bool = False
    # Once effective SOC hits target, stay stopped until parked SOC baseline changes.
    target_met: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise RuntimeError(f"Missing required env var {name}")
    return value


def _optional_float(payload: str) -> float | None:
    text = payload.strip()
    if text.lower() in ("", "unavailable", "unknown", "none", "null"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _optional_bool(payload: str) -> bool | None:
    text = payload.strip().lower()
    if text in ("true", "1", "on", "yes"):
        return True
    if text in ("false", "0", "off", "no"):
        return False
    return None


class VenAdapter:
    def __init__(self) -> None:
        self.tariff_path = _env("TARIFF_CONFIG", "/config/tariff.yaml")
        self.cfg = load_tariff_config(self.tariff_path)
        limits = self.cfg.limits
        rb = self.cfg.ready_by
        hard_max = min(limits.evse_max_amps, limits.branch_max_amps)
        self.amps = AmpController(
            i_min_amps=limits.i_min_amps,
            i_max_amps=hard_max,
            hysteresis_amps=limits.amp_hysteresis_amps,
        )
        self.state = LocalState(
            voltage_v=limits.default_voltage_v,
            target_soc_pct=rb.target_soc_pct,
            battery_capacity_kwh=rb.battery_capacity_kwh,
            ready_by_time=rb.ready_by_time,
            ready_by_enabled=rb.enabled_default,
        )
        self.poll_seconds = float(_env("VEN_INTERVAL_SEC", "3"))
        # ~0.2 at 3s tick tracks on ~15s; override with VEN_SURPLUS_EMA_ALPHA.
        self.surplus_ema = EmaFilter(alpha=float(_env("VEN_SURPLUS_EMA_ALPHA", "0.2")))
        self.mqtt_host = _env("MQTT_HOST", "mosquitto")
        self.mqtt_port = int(_env("MQTT_PORT", "1883"))
        self.vtn_url = _env("VTN_BASE_URL", "http://vtn:3000")
        self.token_url = _env("OAUTH_TOKEN_URL", f"{self.vtn_url}/auth/token")
        self.client_id = _env("VEN_CLIENT_ID", "ven-client-client-id")
        self.client_secret = _env("VEN_CLIENT_SECRET", "ven-client")
        self._stop = threading.Event()
        self._ven = None
        # Do not publish retained tracking until startup restore has finished.
        self._allow_soc_tracking_publish = False
        self._mqtt = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="ven-adapter")
        self._mqtt.on_connect = self._on_connect
        self._mqtt.on_message = self._on_message

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:  # noqa: ANN001
        logger.info("MQTT connected rc=%s", reason_code)
        # Restore accrual before other retained topics can start a fresh snapshot.
        client.subscribe(topics.STATUS_SOC_TRACKING)
        for topic in (
            topics.MODE,
            topics.BID_PRICE,
            topics.USER_AMP_LIMIT,
            topics.VOLTAGE_V,
            topics.SOLAR_KW,
            topics.HOUSE_LOAD_KW,
            topics.GRID_IMPORT_KW,
            topics.GRID_EXPORT_KW,
            topics.OPENEVSE_POWER_KW,
            topics.OPENEVSE_ENERGY_KWH,
            topics.SOC_PCT,
            topics.TARGET_SOC_PCT,
            topics.BATTERY_CAPACITY_KWH,
            topics.READY_BY_TIME,
            topics.READY_BY_ENABLED,
        ):
            client.subscribe(topic)

    def _min_energy_for_target(self, baseline_soc: float, target_soc: float, battery_kwh: float) -> float:
        if battery_kwh <= 0:
            return 0.0
        return max(0.0, (float(target_soc) - float(baseline_soc)) / 100.0 * float(battery_kwh))

    def _restore_soc_tracking(self, payload: str) -> None:
        """Apply retained accrual from a prior VEN process (survives rebuild/restart)."""
        if self.state.soc_tracking_restored:
            return
        self.state.soc_tracking_restored = True
        try:
            data = json.loads(payload)
            baseline = float(data.get("baseline_soc_pct", 0))
            added = float(data.get("energy_added_kwh", 0))
            target_met = bool(data.get("target_met", False))
        except (TypeError, ValueError, json.JSONDecodeError):
            logger.warning("ignoring bad soc_tracking payload=%r", payload)
            return
        if baseline <= 0 or added < 0:
            return
        # If HA already published a different parked SOC, user changed it while down.
        if self.state.soc_pct > 0 and abs(self.state.soc_pct - baseline) >= 0.05:
            logger.info(
                "SOC tracking restore ignored parked=%.1f%% != baseline=%.1f%%",
                self.state.soc_pct,
                baseline,
            )
            return
        self.state.energy_added_since_soc_kwh = added
        self.state.last_soc_for_snapshot = baseline
        self.state.soc_tracking_active = True
        self.state.target_met = target_met
        if target_met:
            floor = self._min_energy_for_target(
                baseline, self.state.target_soc_pct, self.state.battery_capacity_kwh
            )
            self.state.energy_added_since_soc_kwh = max(added, floor)
        logger.info(
            "SOC tracking restored baseline=%.1f%% added=%.3fkWh target_met=%s",
            baseline,
            self.state.energy_added_since_soc_kwh,
            target_met,
        )

    def _publish_soc_tracking(self) -> None:
        if not self._allow_soc_tracking_publish:
            return
        with self.state.lock:
            if self.state.last_soc_for_snapshot <= 0:
                payload = json.dumps(
                    {
                        "baseline_soc_pct": 0.0,
                        "energy_added_kwh": 0.0,
                        "target_met": False,
                    }
                )
            else:
                payload = json.dumps(
                    {
                        "baseline_soc_pct": round(self.state.last_soc_for_snapshot, 2),
                        "energy_added_kwh": round(self.state.energy_added_since_soc_kwh, 4),
                        "target_met": bool(self.state.target_met),
                    }
                )
        self._mqtt.publish(topics.STATUS_SOC_TRACKING, payload, qos=1, retain=True)

    def _maybe_snapshot_soc(self, new_soc: float) -> None:
        """When parked/OEM SOC becomes a new positive value, reset energy accrual."""
        if new_soc <= 0:
            self.state.soc_tracking_active = False
            self.state.energy_added_since_soc_kwh = 0.0
            self.state.last_soc_for_snapshot = 0.0
            self.state.target_met = False
            return
        # Same baseline as restore or prior snapshot (HA republishes every 15s).
        if abs(new_soc - self.state.last_soc_for_snapshot) < 0.05:
            self.state.soc_tracking_active = True
            return
        self.state.energy_added_since_soc_kwh = 0.0
        self.state.soc_tracking_active = True
        self.state.last_soc_for_snapshot = new_soc
        self.state.target_met = False
        logger.info(
            "SOC tracking start soc=%.1f%% (power-integrate until next SOC change)",
            new_soc,
        )

    def _accrue_charge_energy(self, ev_kw: float) -> None:
        """Accumulate delivered kWh from measured EV power (OpenEVSE /wh is often stale)."""
        with self.state.lock:
            if not self.state.soc_tracking_active or self.state.soc_pct <= 0:
                return
            self.state.energy_added_since_soc_kwh += max(0.0, float(ev_kw)) * (
                self.poll_seconds / 3600.0
            )

    def _on_message(self, client, userdata, msg) -> None:  # noqa: ANN001
        # Keep last good value when HA publishes "unavailable"; never crash paho.
        try:
            payload = msg.payload.decode("utf-8").strip()
            with self.state.lock:
                if msg.topic == topics.MODE:
                    self.state.mode = payload.lower()
                    return
                if msg.topic == topics.READY_BY_TIME:
                    try:
                        self.state.ready_by_time = parse_ready_by_hhmm(payload)
                    except (ValueError, IndexError):
                        logger.warning("ignoring bad ready_by_time payload=%r", payload)
                    return
                if msg.topic == topics.READY_BY_ENABLED:
                    flag = _optional_bool(payload)
                    if flag is None:
                        logger.warning("ignoring bad ready_by_enabled payload=%r", payload)
                    else:
                        self.state.ready_by_enabled = flag
                    return
                if msg.topic == topics.STATUS_SOC_TRACKING:
                    self._restore_soc_tracking(payload)
                    return
                value = _optional_float(payload)
                if value is None:
                    logger.warning("ignoring non-numeric MQTT %s payload=%r", msg.topic, payload)
                    return
                if msg.topic == topics.BID_PRICE:
                    self.state.bid_price_per_kwh = value
                elif msg.topic == topics.USER_AMP_LIMIT:
                    self.state.user_amp_limit = int(value)
                elif msg.topic == topics.VOLTAGE_V:
                    self.state.voltage_v = value
                elif msg.topic == topics.SOLAR_KW:
                    self.state.solar_kw = value
                elif msg.topic == topics.HOUSE_LOAD_KW:
                    self.state.house_load_kw = value
                elif msg.topic == topics.GRID_IMPORT_KW:
                    self.state.grid_import_kw = value
                elif msg.topic == topics.GRID_EXPORT_KW:
                    self.state.grid_export_kw = value
                elif msg.topic == topics.OPENEVSE_POWER_KW:
                    self.state.actual_power_kw = value
                elif msg.topic == topics.OPENEVSE_ENERGY_KWH:
                    self.state.energy_kwh = value
                elif msg.topic == topics.SOC_PCT:
                    prev = self.state.soc_pct
                    self.state.soc_pct = value
                    if value != prev:
                        logger.info("SOC MQTT update %.1f%% -> %.1f%%", prev, value)
                    self._maybe_snapshot_soc(value)
                elif msg.topic == topics.TARGET_SOC_PCT:
                    self.state.target_soc_pct = value
                elif msg.topic == topics.BATTERY_CAPACITY_KWH:
                    self.state.battery_capacity_kwh = value
        except Exception:  # noqa: BLE001
            logger.exception("MQTT handler failed topic=%s", getattr(msg, "topic", "?"))

    def _ensure_ven(self) -> None:
        if self._ven is None:
            self._ven = create_ven_client(
                vtn_base_url=self.vtn_url,
                client_id=self.client_id,
                client_secret=self.client_secret,
                token_url=self.token_url,
            )

    def _snapshot_site(self) -> dict[str, float | str | bool | time | None]:
        with self.state.lock:
            return {
                "mode": self.state.mode,
                "bid": self.state.bid_price_per_kwh,
                "user_amps": float(self.state.user_amp_limit),
                "voltage": self.state.voltage_v,
                "solar_kw": self.state.solar_kw,
                "house_kw": self.state.house_load_kw,
                "import_kw": self.state.grid_import_kw,
                "export_kw": self.state.grid_export_kw,
                "ev_kw": self.state.actual_power_kw,
                "energy_kwh": self.state.energy_kwh,
                "soc_pct": self.state.soc_pct,
                "target_soc_pct": self.state.target_soc_pct,
                "battery_capacity_kwh": self.state.battery_capacity_kwh,
                "ready_by_time": self.state.ready_by_time,
                "ready_by_enabled": self.state.ready_by_enabled,
                "energy_added_since_soc_kwh": self.state.energy_added_since_soc_kwh,
                "soc_tracking_active": self.state.soc_tracking_active,
            }

    def _raw_surplus_kw(self, site: dict[str, float | str | bool | time | None]) -> float:
        return grid_net_surplus_kw(
            export_kw=float(site["export_kw"]),
            import_kw=float(site["import_kw"]),
            ev_charge_kw=float(site["ev_kw"]),
        )

    def _smoothed_surplus_kw(
        self, site: dict[str, float | str | bool | time | None]
    ) -> tuple[float, float]:
        raw = self._raw_surplus_kw(site)
        # Do not seed the EMA with the all-zero MQTT cold-start frame, or amps
        # crawl up from 0 for ~15s after every VEN restart.
        if self.surplus_ema.value is None and raw <= 0.0:
            return raw, 0.0
        return raw, self.surplus_ema.update(raw)

    def _economic_target_kw(
        self, site: dict[str, float | str | bool | time | None], surplus_kw: float
    ) -> tuple[float, float | None, bool]:
        """
        Map OpenADR PRICE + IMPORT_CAPACITY_LIMIT plus local surplus to kW.

        PRICE is the effective marginal of the highest accepted block (or a high
        sentinel when nothing clears the bid). Import limit is the grid-import
        portion only; solar-first power comes from measured surplus.
        """
        bid = float(site["bid"])
        voltage = float(site["voltage"])
        user_amps = int(site["user_amps"])

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
        target = surplus_kw + max(0.0, import_limit)
        user_kw = (min(user_amps, self.amps.i_max_amps) * voltage) / 1000.0
        headroom = self.cfg.limits.panel_service_headroom_kw
        target = min(target, user_kw, headroom)
        return max(0.0, target), event_price, target > 0

    def _solar_only_target_kw(
        self, site: dict[str, float | str | bool | time | None], surplus_kw: float
    ) -> float:
        """Measured excess solar only; ignore OpenADR import allowance."""
        return solar_only_target_kw(
            surplus_kw=surplus_kw,
            user_amp_limit=int(site["user_amps"]),
            voltage_v=float(site["voltage"]),
            i_max_amps=self.amps.i_max_amps,
            panel_service_headroom_kw=self.cfg.limits.panel_service_headroom_kw,
        )

    def _deadline_decision(self, site: dict[str, float | str | bool | time | None]):
        rb = self.cfg.ready_by
        return evaluate_deadline(
            ready_by_enabled=bool(site["ready_by_enabled"]),
            soc_pct=float(site["soc_pct"]),
            target_soc_pct=float(site["target_soc_pct"]),
            battery_capacity_kwh=float(site["battery_capacity_kwh"]),
            assumed_soc_pct=rb.assumed_soc_pct,
            cushion_hours=rb.cushion_hours,
            ready_by_time=site["ready_by_time"],  # type: ignore[arg-type]
            timezone=self.cfg.timezone,
            now=datetime.now(timezone.utc),
            energy_added_kwh=float(site["energy_added_since_soc_kwh"]),
            soc_tracking_active=bool(site["soc_tracking_active"]),
            user_amps=int(site["user_amps"]),
            i_max_amps=self.amps.i_max_amps,
            voltage_v=float(site["voltage"]),
        )

    def _target_reached(self, site: dict[str, float | str | bool | time | None]) -> bool:
        """Stop automatic modes once effective SOC meets sticky target (charge_now bypasses)."""
        if float(site["soc_pct"]) <= 0:
            return False
        with self.state.lock:
            if self.state.target_met:
                return True
        rb = self.cfg.ready_by
        eff, _ = effective_soc_pct(
            float(site["soc_pct"]),
            assumed_soc_pct=rb.assumed_soc_pct,
            energy_added_kwh=float(site["energy_added_since_soc_kwh"]),
            battery_capacity_kwh=float(site["battery_capacity_kwh"]),
            tracking_active=bool(site["soc_tracking_active"]),
        )
        reached = (
            energy_needed_kwh(
                effective_soc=eff,
                target_soc_pct=float(site["target_soc_pct"]),
                battery_capacity_kwh=float(site["battery_capacity_kwh"]),
            )
            <= 0.0
        )
        if not reached:
            return False
        with self.state.lock:
            # Latch so a restart/rebuild cannot reopen charging for this parked SOC.
            floor = self._min_energy_for_target(
                self.state.last_soc_for_snapshot or float(site["soc_pct"]),
                float(site["target_soc_pct"]),
                float(site["battery_capacity_kwh"]),
            )
            self.state.energy_added_since_soc_kwh = max(
                self.state.energy_added_since_soc_kwh, floor
            )
            self.state.target_met = True
        return True

    def _publish_deadline_status(self, decision) -> None:  # noqa: ANN001
        self._mqtt.publish(
            topics.STATUS_EFFECTIVE_SOC_PCT,
            f"{decision.effective_soc_pct:.1f}",
            qos=0,
            retain=True,
        )
        self._mqtt.publish(
            topics.STATUS_ENERGY_NEEDED_KWH,
            f"{decision.energy_needed_kwh:.3f}",
            qos=0,
            retain=True,
        )
        self._mqtt.publish(
            topics.STATUS_SLACK_HOURS,
            f"{decision.slack_hours:.3f}",
            qos=0,
            retain=True,
        )
        self._mqtt.publish(
            topics.STATUS_DEADLINE_FORCE,
            "true" if decision.force else "false",
            qos=0,
            retain=True,
        )
        self._mqtt.publish(
            topics.STATUS_DEADLINE_REASON,
            decision.reason,
            qos=0,
            retain=True,
        )

    def _tick(self) -> None:
        site = self._snapshot_site()
        # Accrue from live EV power before deadline math so soc_eff rises while charging.
        self._accrue_charge_energy(float(site["ev_kw"]))
        site = self._snapshot_site()
        mode = str(site["mode"])
        user_amps = int(site["user_amps"])
        voltage = float(site["voltage"])

        override = mode == "charge_now"
        stopped = mode == "stopped"
        solar_only = mode == "solar_only"
        event_price: float | None = None
        event_accepted = False
        raw_surplus = 0.0
        smooth_surplus = 0.0
        target_kw = 0.0
        deadline = self._deadline_decision(site)

        if stopped:
            self.amps.reset()
            self.surplus_ema.reset()
            cmd = AmpCommand(amps=0, reason="stopped")
        elif override:
            cmd = self.amps.charge_now(user_amps)
        elif self._target_reached(site):
            # Target is a charge ceiling for economic/solar_only (and deadline force).
            self.amps.reset()
            self.surplus_ema.reset()
            cmd = AmpCommand(amps=0, reason="target_reached")
        else:
            raw_surplus, smooth_surplus = self._smoothed_surplus_kw(site)
            if solar_only:
                target_kw = self._solar_only_target_kw(site, smooth_surplus)
                event_accepted = target_kw > 0
            else:
                target_kw, event_price, event_accepted = self._economic_target_kw(
                    site, smooth_surplus
                )
            if deadline.force:
                forced = self.amps.charge_now(user_amps)
                cmd = AmpCommand(amps=forced.amps, reason="deadline_force")
                event_accepted = cmd.amps > 0
            else:
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
        self._publish_deadline_status(deadline)
        self._publish_soc_tracking()

        logger.info(
            "mode=%s cmd=%sA reason=%s target_kw=%.3f surplus_raw=%.3f surplus_ema=%.3f "
            "solar=%.3f house=%.3f export=%.3f import=%.3f ev=%.3f price=%s override=%s "
            "deadline=%s force=%s slack=%.2fh soc_mqtt=%.1f%% soc_eff=%.1f%% "
            "added=%.3fkWh needed=%.2fkWh",
            mode,
            cmd.amps,
            cmd.reason,
            target_kw,
            raw_surplus,
            smooth_surplus,
            float(site["solar_kw"]),
            float(site["house_kw"]),
            float(site["export_kw"]),
            float(site["import_kw"]),
            float(site["ev_kw"]),
            event_price,
            override,
            deadline.reason,
            deadline.force,
            deadline.slack_hours,
            float(site["soc_pct"]),
            deadline.effective_soc_pct,
            float(site["energy_added_since_soc_kwh"]),
            deadline.energy_needed_kwh,
        )

    def run(self) -> None:
        self._mqtt.connect(self.mqtt_host, self.mqtt_port, 60)
        self._mqtt.loop_start()
        # Let retained SOC tracking + telemetry arrive before the first amp decision.
        time_module.sleep(1.5)
        with self.state.lock:
            if not self.state.soc_tracking_restored:
                self.state.soc_tracking_restored = True
                logger.info("SOC tracking: no retained state on startup")
        self._allow_soc_tracking_publish = True
        logger.info(
            "VEN adapter running interval=%ss surplus_ema_alpha=%.3f hysteresis=%.2fA "
            "ready_by=%s assumed_soc=%.0f%% target_met=%s added=%.3fkWh",
            self.poll_seconds,
            self.surplus_ema.alpha,
            self.amps.hysteresis_amps,
            self.cfg.ready_by.ready_by_time.strftime("%H:%M"),
            self.cfg.ready_by.assumed_soc_pct,
            self.state.target_met,
            self.state.energy_added_since_soc_kwh,
        )
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
