"""Intent-focused unit tests for supply-curve stacking and discrete amps."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from home_ev_flex.amperage import AmpController
from home_ev_flex.supply_curve import build_supply_curve, dispatch
from home_ev_flex.smoothing import EmaFilter
from home_ev_flex.tariff import (
    grid_net_surplus_kw,
    load_tariff_config,
    resolve_import_price,
    solar_only_target_kw,
    solar_surplus_kw,
)

ROOT = Path(__file__).resolve().parents[1]
TARIFF_PATH = ROOT / "config" / "tariff.yaml"


def test_worked_stack_accepts_only_solar_block_at_bid():
    """
    Why: charging must follow marginal $/kWh blocks, not a blended average.

    Solar surplus 3 kW @ $0.07, grid @ $0.18, bid $0.16 -> accept 3 kW only.
    """
    curve = build_supply_curve(
        solar_surplus_kw=3.0,
        export_credit_per_kwh=0.07,
        import_price_per_kwh=0.18,
        panel_service_headroom_kw=7.7,
    )
    result = dispatch(
        curve,
        bid_price_per_kwh=0.16,
        evse_maximum_kw=7.7,
        vehicle_maximum_kw=7.7,
        panel_service_headroom_kw=7.7,
        user_charging_limit_kw=7.7,
    )
    assert result.accepted_power_kw == pytest.approx(3.0)
    assert result.effective_marginal_price == pytest.approx(0.07)
    assert result.import_power_limit_kw == pytest.approx(0.0)
    assert [b.source for b in result.accepted_blocks] == ["solar_export_opportunity"]


def test_bid_at_or_above_import_accepts_grid_block():
    """Why: bid threshold is inclusive; clearing import unlocks remaining headroom."""
    curve = build_supply_curve(
        solar_surplus_kw=3.0,
        export_credit_per_kwh=0.07,
        import_price_per_kwh=0.18,
        panel_service_headroom_kw=7.7,
    )
    result = dispatch(
        curve,
        bid_price_per_kwh=0.18,
        evse_maximum_kw=7.7,
        vehicle_maximum_kw=7.7,
        panel_service_headroom_kw=7.7,
        user_charging_limit_kw=7.7,
    )
    assert result.accepted_power_kw == pytest.approx(7.7)
    assert result.effective_marginal_price == pytest.approx(0.18)
    assert result.import_power_limit_kw == pytest.approx(4.7)


def test_floor_not_round_at_240v_maps_3kw_to_12a():
    """Why: OpenEVSE must not overshoot available surplus by rounding up amps."""
    ctrl = AmpController(i_min_amps=6, i_max_amps=40)
    # 3.0 kW at 240 V -> 12.5 A raw -> floor to 12 A (~2.88 kW)
    cmd = ctrl.quantize(3.0, 240.0)
    assert cmd.amps == 12
    assert cmd.reason == "economic"


def test_never_command_1_to_5_amps():
    """Why: J1772 continuous minimum is ~6 A; sub-min must stop, not trickle."""
    ctrl = AmpController(i_min_amps=6, i_max_amps=40)
    # 1.0 kW at 240 V -> 4.16 A -> stop
    assert ctrl.quantize(1.0, 240.0).amps == 0
    assert ctrl.quantize(1.44, 240.0).amps == 6  # exactly I_min


def test_amp_hysteresis_holds_through_small_surplus_noise():
    """
    Why: amp hysteresis prevents chatter at amp bucket boundaries.

    Holding 12 A must not flip to 11 A on tiny surplus noise.
    """
    ctrl = AmpController(i_min_amps=6, i_max_amps=40, hysteresis_amps=0.75)
    first = ctrl.command_for_power(3.0, 240.0)  # 12 A
    assert first.amps == 12
    # ~2.88 kW is still 12.0 A raw; nudge slightly down but stay within hold.
    held = ctrl.command_for_power(2.80, 240.0)  # 11.66 -> floor 11, within hysteresis
    assert held.amps == 12
    assert held.reason == "hysteresis_hold_down"
    # Clear drop below hysteresis margin steps down.
    dropped = ctrl.command_for_power(2.40, 240.0)  # 10.0 A
    assert dropped.amps == 10


def test_charge_now_bypasses_economics_but_respects_hard_max():
    """Why: Charge Now ignores price but never bypasses configured hard limits."""
    ctrl = AmpController(i_min_amps=6, i_max_amps=32)
    cmd = ctrl.charge_now(40)
    assert cmd.amps == 32
    assert cmd.reason == "charge_now"
    assert ctrl.charge_now(5).amps == 0


def test_tou_resolver_uses_config_timezone_and_windows():
    """Why: import price must follow the YAML TOU windows, not a hard-coded utility."""
    cfg = load_tariff_config(TARIFF_PATH)
    tz = ZoneInfo(cfg.timezone)
    on_peak = datetime(2026, 7, 15, 14, 0, tzinfo=tz)  # Wednesday inside on-peak
    off_peak = datetime(2026, 7, 15, 20, 0, tzinfo=tz)
    weekend = datetime(2026, 7, 18, 14, 0, tzinfo=tz)  # Saturday
    assert cfg.price_source == "static_yaml"
    assert resolve_import_price(cfg, on_peak) == pytest.approx(cfg.weekday_on_peak_price)
    assert resolve_import_price(cfg, off_peak) == pytest.approx(cfg.weekday_off_peak_price)
    assert resolve_import_price(cfg, weekend) == pytest.approx(cfg.weekend_price)


def test_example_utility_configs_load():
    """Why: shipped examples must stay loadable so contributors can fork baselines."""
    example = ROOT / "config" / "examples" / "dte.yaml"
    cfg = load_tariff_config(example)
    assert cfg.utility == "DTE"
    assert cfg.export_credit_per_kwh == pytest.approx(0.07)


def test_solar_surplus_is_non_negative():
    assert solar_surplus_kw(solar_kw=5.0, house_load_kw=2.0) == pytest.approx(3.0)
    assert solar_surplus_kw(solar_kw=1.0, house_load_kw=2.0) == pytest.approx(0.0)


def test_grid_net_surplus_matches_solar_minus_house_when_consistent():
    """
    Why: VEN must use export-import+EV so a stale solar/house MQTT pair cannot
    invent a 5 kW surplus spike (seen in live calibrate captures).
    """
    # Consistent: solar 5.386, house 3.616, export 1.77, import 0, ev 0.
    assert grid_net_surplus_kw(export_kw=1.77, import_kw=0.0, ev_charge_kw=0.0) == pytest.approx(
        1.77
    )
    # Stale solar-house pairing would claim ~5.1 kW; grid form stays at export.
    assert solar_surplus_kw(solar_kw=5.386, house_load_kw=0.293) == pytest.approx(5.093)
    assert grid_net_surplus_kw(export_kw=1.77, import_kw=0.0) == pytest.approx(1.77)
    # While EV is already drawing, add its power back into available setpoint room.
    assert grid_net_surplus_kw(export_kw=0.5, import_kw=0.0, ev_charge_kw=2.0) == pytest.approx(2.5)


def test_solar_only_ignores_headroom_beyond_surplus():
    """
    Why: solar_only must not pull grid when HVAC raises house load, even if
    panel headroom and a high bid would let economic mode import.
    """
    assert solar_only_target_kw(
        surplus_kw=2.0,
        user_amp_limit=32,
        voltage_v=240.0,
        i_max_amps=40,
        panel_service_headroom_kw=7.68,
    ) == pytest.approx(2.0)
    assert solar_only_target_kw(
        surplus_kw=0.0,
        user_amp_limit=32,
        voltage_v=240.0,
        i_max_amps=40,
        panel_service_headroom_kw=7.68,
    ) == pytest.approx(0.0)


def test_surplus_ema_rejects_single_sample_spike():
    """Why: one mismatched MQTT publish must not yank the amp command immediately."""
    ema = EmaFilter(alpha=0.2)
    assert ema.update(1.8) == pytest.approx(1.8)
    # Spike like the stale solar/house pair (~5.1 kW) only moves EMA modestly.
    assert ema.update(5.1) == pytest.approx(0.2 * 5.1 + 0.8 * 1.8)
    assert ema.value < 3.0

