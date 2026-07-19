"""Resolve utility TOU import price and export opportunity cost from YAML."""

from __future__ import annotations

from dataclasses import dataclass
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


def solar_surplus_kw(*, solar_kw: float, house_load_kw: float) -> float:
    """Power that would otherwise export if the EV does not consume it."""
    return max(0.0, solar_kw - house_load_kw)
