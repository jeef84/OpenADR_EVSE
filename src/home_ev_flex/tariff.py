"""Resolve utility TOU import price and export opportunity cost from YAML."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml


@dataclass(frozen=True)
class TariffLimits:
    panel_service_headroom_kw: float
    evse_max_amps: int
    branch_max_amps: int
    i_min_amps: int
    amp_hysteresis_amps: float
    default_voltage_v: float


@dataclass(frozen=True)
class CarbonSignalConfig:
    """Hard permit gate: at or below threshold → $0; above → full max_adder."""

    threshold: float
    max_adder_per_kwh: float
    # Optional legacy field; ignored by the step gate (kept for older YAML).
    dollars_per_unit: float = 0.0


@dataclass(frozen=True)
class CarbonPriceConfig:
    """
    Optional carbon overlay on grid import price only.

    effective_import = TOU_import + max(configured signal adders).
    Each signal is a hard gate: value <= threshold permits (adder 0);
    value > threshold applies max_adder_per_kwh (blocks typical bids).
    Solar export-credit blocks are never inflated.
    """

    enabled: bool = False
    co2_intensity: CarbonSignalConfig | None = None
    fossil_fuel_pct: CarbonSignalConfig | None = None
    # max_adder: treat missing MQTT as dirty (do not silently import).
    # zero: ignore carbon until a reading arrives (logs should warn).
    unavailable_behavior: str = "max_adder"


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


def _carbon_signal(raw: dict | None, *, threshold_key: str) -> CarbonSignalConfig | None:
    if not raw:
        return None
    return CarbonSignalConfig(
        threshold=float(raw[threshold_key]),
        max_adder_per_kwh=float(raw.get("max_adder_per_kwh", 0.50)),
        dollars_per_unit=float(raw.get("dollars_per_unit", 0.0)),
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
        ),
        fossil_fuel_pct=_carbon_signal(
            section.get("fossil_fuel_pct"),
            threshold_key="threshold_pct",
        ),
        unavailable_behavior=behavior,
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
        ),
        carbon_price=_load_carbon_price(raw),
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


def _signal_adder(value: float | None, signal: CarbonSignalConfig | None) -> float | None:
    """Hard gate: <= threshold → 0; above → max_adder. None if unconfigured / missing."""
    if signal is None:
        return None
    if value is None:
        return None
    if float(value) <= signal.threshold:
        return 0.0
    return signal.max_adder_per_kwh


def carbon_adder_per_kwh(
    cfg: CarbonPriceConfig,
    *,
    co2_intensity_g_per_kwh: float | None,
    fossil_fuel_pct: float | None,
) -> tuple[float, str]:
    """
    Carbon $/kWh adder for the grid_import supply block.

    Returns (adder, reason). reason is for logs/status, not OpenADR.
    """
    if not cfg.enabled:
        return 0.0, "disabled"

    adders: list[float] = []
    if cfg.co2_intensity is not None:
        adder = _signal_adder(co2_intensity_g_per_kwh, cfg.co2_intensity)
        if adder is not None:
            adders.append(adder)
    if cfg.fossil_fuel_pct is not None:
        adder = _signal_adder(fossil_fuel_pct, cfg.fossil_fuel_pct)
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


def effective_import_price(
    cfg: TariffConfig,
    when: datetime,
    *,
    co2_intensity_g_per_kwh: float | None = None,
    fossil_fuel_pct: float | None = None,
) -> tuple[float, float, str]:
    """Return (effective_import, carbon_adder, adder_reason)."""
    tou = resolve_import_price(cfg, when)
    adder, reason = carbon_adder_per_kwh(
        cfg.carbon_price,
        co2_intensity_g_per_kwh=co2_intensity_g_per_kwh,
        fossil_fuel_pct=fossil_fuel_pct,
    )
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
