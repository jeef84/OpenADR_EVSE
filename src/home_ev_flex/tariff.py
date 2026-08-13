"""Resolve utility TOU import price and export opportunity cost from YAML."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from home_ev_flex.carbon_adaptive import (
    AdaptiveCarbonConfig,
    AdaptiveThresholdResult,
    CarbonHistoryStore,
    resolve_adaptive_thresholds,
)
from home_ev_flex.deadline import ReadyByConfig, parse_ready_by_hhmm


@dataclass(frozen=True)
class TariffLimits:
    panel_service_headroom_kw: float
    evse_max_amps: int
    branch_max_amps: int
    i_min_amps: int
    amp_hysteresis_amps: float
    default_voltage_v: float
    # When measured demand (solar+import-export) is above this, inflate grid import
    # $/kWh by peak_demand_adder_per_kwh so the bid rejects (0 disables).
    peak_demand_limit_kw: float = 0.0
    peak_demand_adder_per_kwh: float = 0.50


@dataclass(frozen=True)
class CarbonSignalConfig:
    """Hard permit gate: at or below threshold → $0; above → full max_adder."""

    threshold: float
    max_adder_per_kwh: float
    # Optional legacy field; ignored by the step gate (kept for older YAML).
    dollars_per_unit: float = 0.0
    # Absolute floor when adaptive learning is enabled (how clean we may target).
    min_threshold: float | None = None


@dataclass(frozen=True)
class CarbonPriceConfig:
    """
    Optional carbon overlay on grid import price only.

    effective_import = TOU_import + max(configured signal adders).
    Each signal is a hard gate: value <= threshold permits (adder 0);
    value > threshold applies max_adder_per_kwh (blocks typical bids).
    Solar export-credit blocks are never inflated.

    When adaptive.enabled, YAML threshold is the ceiling; learned off-peak
    percentile + ready-by slack set the effective permit gate.
    """

    enabled: bool = False
    co2_intensity: CarbonSignalConfig | None = None
    fossil_fuel_pct: CarbonSignalConfig | None = None
    # max_adder: treat missing MQTT as dirty (do not silently import).
    # zero: ignore carbon until a reading arrives (logs should warn).
    unavailable_behavior: str = "max_adder"
    adaptive: AdaptiveCarbonConfig = field(default_factory=AdaptiveCarbonConfig)


@dataclass(frozen=True)
class TariffConfig:
    """Utility-agnostic static tariff. Values come entirely from YAML."""

    utility: str
    timezone: str
    rate_schedule: str
    price_source: str
    weekday_on_peak_start: time
    weekday_on_peak_end: time
    weekday_on_peak_price: float
    weekday_off_peak_price: float
    weekend_price: float
    export_credit_per_kwh: float
    limits: TariffLimits
    carbon_price: CarbonPriceConfig = field(default_factory=CarbonPriceConfig)
    ready_by: ReadyByConfig = field(default_factory=ReadyByConfig)


def _parse_hhmm(value: str) -> time:
    hour, minute = value.split(":")
    return time(hour=int(hour), minute=int(minute))


def _export_credit(raw: dict) -> float:
    export = raw.get("export") or {}
    if "credit_per_kwh" in export:
        return float(export["credit_per_kwh"])
    # Backward-compatible alias used in early DTE-shaped drafts.
    if "rider_18_credit_per_kwh" in export:
        return float(export["rider_18_credit_per_kwh"])
    raise KeyError("export.credit_per_kwh is required")


def _carbon_signal(
    raw: dict | None,
    *,
    threshold_key: str,
    min_threshold_key: str,
    default_min: float,
) -> CarbonSignalConfig | None:
    if not raw:
        return None
    min_raw = raw.get(min_threshold_key)
    return CarbonSignalConfig(
        threshold=float(raw[threshold_key]),
        max_adder_per_kwh=float(raw.get("max_adder_per_kwh", 0.50)),
        dollars_per_unit=float(raw.get("dollars_per_unit", 0.0)),
        min_threshold=float(default_min if min_raw is None else min_raw),
    )


def _load_adaptive_carbon(raw: dict) -> AdaptiveCarbonConfig:
    section = raw.get("adaptive") or {}
    if not section:
        return AdaptiveCarbonConfig()
    return AdaptiveCarbonConfig(
        enabled=bool(section.get("enabled", False)),
        lookback_days=int(section.get("lookback_days", 14)),
        sample_interval_sec=float(section.get("sample_interval_sec", 300)),
        percentile=float(section.get("percentile", 25)),
        min_samples=int(section.get("min_samples", 288)),
        slack_high_hours=float(section.get("slack_high_hours", 4.0)),
        state_path=str(
            section.get("state_path", "/data/carbon_adaptive/carbon_history.json")
        ),
    )


def _load_carbon_price(raw: dict) -> CarbonPriceConfig:
    section = raw.get("carbon_price") or {}
    if not section:
        return CarbonPriceConfig()
    behavior = str(section.get("unavailable_behavior", "max_adder")).lower()
    if behavior not in ("max_adder", "zero"):
        raise ValueError(
            f"carbon_price.unavailable_behavior must be 'max_adder' or 'zero', got {behavior!r}"
        )
    return CarbonPriceConfig(
        enabled=bool(section.get("enabled", False)),
        co2_intensity=_carbon_signal(
            section.get("co2_intensity"),
            threshold_key="threshold_g_per_kwh",
            min_threshold_key="min_threshold_g_per_kwh",
            default_min=350.0,
        ),
        fossil_fuel_pct=_carbon_signal(
            section.get("fossil_fuel_pct"),
            threshold_key="threshold_pct",
            min_threshold_key="min_threshold_pct",
            default_min=50.0,
        ),
        unavailable_behavior=behavior,
        adaptive=_load_adaptive_carbon(section),
    )


def _load_ready_by(raw: dict) -> ReadyByConfig:
    section = raw.get("ready_by") or {}
    if not section:
        return ReadyByConfig()
    return ReadyByConfig(
        cushion_hours=float(section.get("cushion_hours", 0.25)),
        assumed_soc_pct=float(section.get("assumed_soc_pct", 40.0)),
        battery_capacity_kwh=float(section.get("battery_capacity_kwh", 74.7)),
        target_soc_pct=float(section.get("target_soc_pct", 85.0)),
        ready_by_time=parse_ready_by_hhmm(str(section.get("ready_by_time", "07:00"))),
        enabled_default=bool(section.get("enabled", True)),
    )


def load_tariff_config(path: str | Path) -> TariffConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    weekday = raw["import_rates"]["weekday"]
    limits = raw["limits"]
    return TariffConfig(
        utility=str(raw["utility"]),
        timezone=str(raw["timezone"]),
        rate_schedule=str(raw.get("rate_schedule", "")),
        price_source=str(raw.get("price_source", "static_yaml")),
        weekday_on_peak_start=_parse_hhmm(weekday["on_peak"]["start"]),
        weekday_on_peak_end=_parse_hhmm(weekday["on_peak"]["end"]),
        weekday_on_peak_price=float(weekday["on_peak"]["price_per_kwh"]),
        weekday_off_peak_price=float(weekday["off_peak"]["price_per_kwh"]),
        weekend_price=float(raw["import_rates"]["weekend"]["all_day"]["price_per_kwh"]),
        export_credit_per_kwh=_export_credit(raw),
        limits=TariffLimits(
            panel_service_headroom_kw=float(limits["panel_service_headroom_kw"]),
            evse_max_amps=int(limits["evse_max_amps"]),
            branch_max_amps=int(limits["branch_max_amps"]),
            i_min_amps=int(limits["i_min_amps"]),
            amp_hysteresis_amps=float(limits["amp_hysteresis_amps"]),
            default_voltage_v=float(limits.get("default_voltage_v", 240.0)),
            peak_demand_limit_kw=float(limits.get("peak_demand_limit_kw", 0.0)),
            peak_demand_adder_per_kwh=float(
                limits.get("peak_demand_adder_per_kwh", 0.50)
            ),
        ),
        carbon_price=_load_carbon_price(raw),
        ready_by=_load_ready_by(raw),
    )


def resolve_import_price(cfg: TariffConfig, when: datetime) -> float:
    """Return current retail TOU import price ($/kWh) in the tariff timezone."""
    if cfg.price_source != "static_yaml":
        raise NotImplementedError(
            f"price_source={cfg.price_source!r} is not implemented yet; "
            "use static_yaml or see docs for planned realtime providers"
        )
    local = when.astimezone(ZoneInfo(cfg.timezone))
    if local.weekday() >= 5:  # Saturday=5, Sunday=6
        return cfg.weekend_price
    t = local.time()
    if cfg.weekday_on_peak_start <= t < cfg.weekday_on_peak_end:
        return cfg.weekday_on_peak_price
    return cfg.weekday_off_peak_price


def _signal_adder(
    value: float | None,
    signal: CarbonSignalConfig | None,
    *,
    threshold_override: float | None = None,
) -> float | None:
    """Hard gate: <= threshold → 0; above → max_adder. None if unconfigured / missing."""
    if signal is None:
        return None
    if value is None:
        return None
    gate = signal.threshold if threshold_override is None else float(threshold_override)
    if float(value) <= gate:
        return 0.0
    return signal.max_adder_per_kwh


def effective_carbon_thresholds(
    cfg: CarbonPriceConfig,
    *,
    history: CarbonHistoryStore | None = None,
    slack_hours: float | None = None,
    slack_low_hours: float = 0.25,
) -> AdaptiveThresholdResult:
    """Resolve adaptive or static permit thresholds for CO2 / fossil signals."""
    co2 = cfg.co2_intensity
    fossil = cfg.fossil_fuel_pct
    return resolve_adaptive_thresholds(
        adaptive=cfg.adaptive,
        co2_ceiling=None if co2 is None else co2.threshold,
        fossil_ceiling=None if fossil is None else fossil.threshold,
        co2_min=None if co2 is None else co2.min_threshold,
        fossil_min=None if fossil is None else fossil.min_threshold,
        history=history,
        slack_hours=slack_hours,
        slack_low_hours=slack_low_hours,
    )


def carbon_adder_per_kwh(
    cfg: CarbonPriceConfig,
    *,
    co2_intensity_g_per_kwh: float | None,
    fossil_fuel_pct: float | None,
    co2_threshold: float | None = None,
    fossil_threshold: float | None = None,
) -> tuple[float, str]:
    """
    Carbon $/kWh adder for the grid_import supply block.

    Optional co2_threshold / fossil_threshold override the YAML ceilings
    (used by adaptive learning). Returns (adder, reason).
    """
    if not cfg.enabled:
        return 0.0, "disabled"

    adders: list[float] = []
    if cfg.co2_intensity is not None:
        adder = _signal_adder(
            co2_intensity_g_per_kwh,
            cfg.co2_intensity,
            threshold_override=co2_threshold,
        )
        if adder is not None:
            adders.append(adder)
    if cfg.fossil_fuel_pct is not None:
        adder = _signal_adder(
            fossil_fuel_pct,
            cfg.fossil_fuel_pct,
            threshold_override=fossil_threshold,
        )
        if adder is not None:
            adders.append(adder)

    if adders:
        return max(adders), "signal"

    caps = [
        s.max_adder_per_kwh
        for s in (cfg.co2_intensity, cfg.fossil_fuel_pct)
        if s is not None
    ]
    if cfg.unavailable_behavior == "zero" or not caps:
        return 0.0, "unavailable_zero"
    return max(caps), "unavailable_max_adder"


def site_demand_kw(
    *,
    solar_kw: float,
    import_kw: float,
    export_kw: float,
) -> float:
    """Measured site demand (kW): solar + import − export."""
    return max(0.0, float(solar_kw) + float(import_kw) - float(export_kw))


def peak_demand_adder_per_kwh(
    *,
    peak_demand_limit_kw: float,
    adder_per_kwh: float,
    demand_kw: float,
) -> tuple[float, str]:
    """
    Hard gate on peak demand for grid-import price only.

    At or below peak_demand_limit_kw → $0. Above → full adder (typical bid fails).
    Disabled when peak_demand_limit_kw <= 0.
    """
    if float(peak_demand_limit_kw) <= 0:
        return 0.0, "disabled"
    if float(demand_kw) > float(peak_demand_limit_kw):
        return max(0.0, float(adder_per_kwh)), "over_peak_demand"
    return 0.0, "ok"


def effective_import_price(
    cfg: TariffConfig,
    when: datetime,
    *,
    co2_intensity_g_per_kwh: float | None = None,
    fossil_fuel_pct: float | None = None,
    demand_kw: float | None = None,
    history: CarbonHistoryStore | None = None,
    slack_hours: float | None = None,
    thresholds: AdaptiveThresholdResult | None = None,
) -> tuple[float, float, str]:
    """
    Return (effective_import, total_adder, adder_reason).

    total_adder is max(carbon, peak_demand) so either dirty grid or demand over
    the peak limit can make import uneconomic; solar export-credit blocks stay untouched.
    """
    tou = resolve_import_price(cfg, when)
    thr = thresholds or effective_carbon_thresholds(
        cfg.carbon_price,
        history=history,
        slack_hours=slack_hours,
        slack_low_hours=cfg.ready_by.cushion_hours,
    )
    carbon_adder, carbon_reason = carbon_adder_per_kwh(
        cfg.carbon_price,
        co2_intensity_g_per_kwh=co2_intensity_g_per_kwh,
        fossil_fuel_pct=fossil_fuel_pct,
        co2_threshold=thr.co2_threshold,
        fossil_threshold=thr.fossil_threshold,
    )
    demand_adder, demand_reason = peak_demand_adder_per_kwh(
        peak_demand_limit_kw=cfg.limits.peak_demand_limit_kw,
        adder_per_kwh=cfg.limits.peak_demand_adder_per_kwh,
        demand_kw=0.0 if demand_kw is None else float(demand_kw),
    )
    adder = max(carbon_adder, demand_adder)
    reason = demand_reason if demand_adder > carbon_adder else carbon_reason
    return tou + adder, adder, reason


def solar_surplus_kw(*, solar_kw: float, house_load_kw: float) -> float:
    """Power that would otherwise export if the EV does not consume it."""
    return max(0.0, solar_kw - house_load_kw)


def grid_net_surplus_kw(
    *,
    export_kw: float,
    import_kw: float,
    ev_charge_kw: float = 0.0,
) -> float:
    """
    Race-safe surplus from grid CTs plus current EV charge power.

    When solar/house MQTT topics update independently they can briefly disagree.
    Algebraically, with consistent sensors:
      house = solar + import - export - ev
      surplus = solar - house = export - import + ev
    Prefer this form on the VEN control path.
    """
    return max(0.0, export_kw - import_kw + max(0.0, ev_charge_kw))


def solar_only_target_kw(
    *,
    surplus_kw: float,
    user_amp_limit: int,
    voltage_v: float,
    i_max_amps: int,
    panel_service_headroom_kw: float,
) -> float:
    """
    Charge power when mode is solar_only: measured excess solar, no grid import.

    Ignores OpenADR IMPORT_POWER_LIMIT / cheap TOU import that economic mode would
    otherwise accept. Still clamped by user amps and panel headroom.
    """
    user_kw = (min(user_amp_limit, i_max_amps) * voltage_v) / 1000.0
    return max(0.0, min(max(0.0, surplus_kw), user_kw, panel_service_headroom_kw))
