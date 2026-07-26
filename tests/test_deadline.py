"""Ready-by-departure overlay: force on off-peak when slack is gone."""

from __future__ import annotations

from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from home_ev_flex.deadline import (
    ReadyByConfig,
    effective_soc_pct,
    energy_needed_kwh,
    evaluate_deadline,
    hours_until_ready_by_clock,
    off_peak_hours_until,
    parse_ready_by_hhmm,
)
from home_ev_flex.tariff import load_tariff_config

TZ = "America/Detroit"
ON_PEAK_START = time(11, 0)
ON_PEAK_END = time(19, 0)


def _local(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(TZ))


def _deadline(**kwargs):
    defaults = dict(
        ready_by_enabled=True,
        soc_pct=50.0,
        target_soc_pct=85.0,
        battery_capacity_kwh=74.7,
        assumed_soc_pct=40.0,
        cushion_hours=0.25,
        ready_by_time=time(7, 0),
        timezone=TZ,
        now=_local(2026, 7, 20, 6, 0),
        energy_added_kwh=0.0,
        soc_tracking_active=True,
        user_amps=32,
        i_max_amps=40,
        voltage_v=240.0,
        on_peak_start=ON_PEAK_START,
        on_peak_end=ON_PEAK_END,
    )
    defaults.update(kwargs)
    return evaluate_deadline(**defaults)


def test_assumed_soc_energy_needed_uses_forty_percent() -> None:
    """Why: without a fallback, zero SOC would skip force and miss departure."""
    eff, assumed = effective_soc_pct(
        0.0,
        assumed_soc_pct=40.0,
        energy_added_kwh=10.0,
        battery_capacity_kwh=74.7,
        tracking_active=False,
    )
    assert assumed is True
    assert eff == pytest.approx(40.0)
    needed = energy_needed_kwh(
        effective_soc=eff, target_soc_pct=85.0, battery_capacity_kwh=74.7
    )
    assert needed == pytest.approx((85.0 - 40.0) / 100.0 * 74.7)


def test_real_soc_tracks_energy_added() -> None:
    """Why: delivered kWh since parked SOC must raise effective SOC for deadline math."""
    eff, assumed = effective_soc_pct(
        50.0,
        assumed_soc_pct=40.0,
        energy_added_kwh=10.0,
        battery_capacity_kwh=100.0,
        tracking_active=True,
    )
    assert assumed is False
    assert eff == pytest.approx(60.0)


def test_no_tracking_without_flag() -> None:
    """Why: before a parked SOC is accepted, do not invent progress from stray power."""
    eff, assumed = effective_soc_pct(
        50.0,
        assumed_soc_pct=40.0,
        energy_added_kwh=10.0,
        battery_capacity_kwh=100.0,
        tracking_active=False,
    )
    assert assumed is False
    assert eff == pytest.approx(50.0)


def test_hours_until_ready_by_before_clock() -> None:
    now = _local(2026, 7, 20, 1, 0)
    hours = hours_until_ready_by_clock(now, time(7, 0), timezone=TZ)
    assert hours == pytest.approx(6.0)


def test_hours_until_ready_by_past_clock_rolls_to_tomorrow() -> None:
    """Why: after 07:00, daytime plug-in must aim at tomorrow, not force all day."""
    now = _local(2026, 7, 20, 8, 0)
    hours = hours_until_ready_by_clock(now, time(7, 0), timezone=TZ)
    assert hours == pytest.approx(23.0)


def test_off_peak_hours_skips_weekday_on_peak() -> None:
    """Why: slack must ignore on-peak so force never budgets peak imports."""
    # Mon 10:00 → Tue 07:00: 1h before peak + 12h overnight = 13h off-peak.
    now = _local(2026, 7, 20, 10, 0)
    until = _local(2026, 7, 21, 7, 0)
    hours = off_peak_hours_until(
        now,
        until,
        timezone=TZ,
        on_peak_start=ON_PEAK_START,
        on_peak_end=ON_PEAK_END,
    )
    assert hours == pytest.approx(13.0)


def test_force_when_off_peak_slack_gone() -> None:
    """Why: carbon/price must not strand the car when off-peak window is too short."""
    # At 6:00 off-peak, need ~2 h for ~15.3 kWh; only 1 h until 07:00 → force.
    decision = _deadline(soc_pct=50.0, now=_local(2026, 7, 20, 6, 0))
    assert decision.energy_needed_kwh > 0
    assert decision.force is True
    assert decision.reason == "force"


def test_no_force_in_afternoon_when_overnight_off_peak_has_slack() -> None:
    """Why: Mon 17:00 on-peak must not force; overnight off-peak covers ~1 kWh."""
    decision = _deadline(
        soc_pct=83.0,
        energy_added_kwh=0.357,
        now=_local(2026, 7, 20, 17, 0),
    )
    assert decision.force is False
    assert decision.reason != "force"
    assert decision.slack_hours > 1.0


def test_tight_slack_during_on_peak_waits() -> None:
    """Why: never deadline_force grid import during weekday on-peak."""
    # Huge need vs remaining overnight off-peak, but it is still 14:00 on-peak.
    decision = _deadline(
        soc_pct=10.0,
        now=_local(2026, 7, 20, 14, 0),
        user_amps=6,
    )
    assert decision.force is False
    assert decision.reason == "force_wait_off_peak"


def test_assumed_soc_still_forces_when_off_peak_window_is_tight() -> None:
    """Why: missing parked SOC must still force in off-peak when the window is short."""
    # 03:00 off-peak; assumed 40% → ~4.4 h needed; until 07:00 = 4 h off-peak → force.
    decision = _deadline(
        soc_pct=0.0,
        now=_local(2026, 7, 20, 3, 0),
        soc_tracking_active=False,
    )
    assert decision.soc_assumed is True
    assert decision.force is True
    assert decision.reason == "force"


def test_no_force_when_plenty_of_off_peak_slack() -> None:
    """Why: with hours of off-peak slack, prefer economic/solar (overlay stays quiet)."""
    decision = _deadline(soc_pct=80.0, now=_local(2026, 7, 20, 1, 0))
    # ~3.7 kWh / 7.68 kW ≈ 0.48 h; off-peak until 07:00 = 6 h → slack ~5.5 > cushion
    assert decision.force is False
    assert decision.reason == "ok"


def test_energy_added_reduces_needed() -> None:
    """Why: power-integrated kWh must shrink energy_needed while charging."""
    base = _deadline(soc_pct=80.0, energy_added_kwh=0.0, now=_local(2026, 7, 20, 1, 0))
    after = _deadline(soc_pct=80.0, energy_added_kwh=1.5, now=_local(2026, 7, 20, 1, 0))
    assert after.effective_soc_pct > base.effective_soc_pct
    assert after.energy_needed_kwh < base.energy_needed_kwh


def test_target_reached_clears_force() -> None:
    decision = _deadline(soc_pct=90.0, now=_local(2026, 7, 20, 8, 0))
    assert decision.energy_needed_kwh == pytest.approx(0.0)
    assert decision.force is False


def test_inactive_when_ready_by_disabled() -> None:
    decision = _deadline(
        ready_by_enabled=False,
        soc_pct=20.0,
        now=_local(2026, 7, 20, 8, 0),
    )
    assert decision.force is False
    assert decision.reason == "inactive"


def test_parse_ready_by_hhmm() -> None:
    assert parse_ready_by_hhmm("07:00") == time(7, 0)
    assert parse_ready_by_hhmm("7:30") == time(7, 30)


def test_sticky_defaults_match_site_baseline() -> None:
    """Why: battery/target/ready-by are set-and-forget, not per plug-in."""
    cfg = ReadyByConfig()
    assert cfg.battery_capacity_kwh == pytest.approx(74.7)
    assert cfg.target_soc_pct == pytest.approx(85.0)
    assert cfg.ready_by_time == time(7, 0)
    assert cfg.assumed_soc_pct == pytest.approx(40.0)
    assert cfg.enabled_default is True


def test_tariff_yaml_loads_ready_by() -> None:
    path = Path(__file__).resolve().parents[1] / "config" / "tariff.yaml"
    cfg = load_tariff_config(path)
    assert cfg.ready_by.battery_capacity_kwh == pytest.approx(74.7)
    assert cfg.ready_by.target_soc_pct == pytest.approx(85.0)
    assert cfg.ready_by.ready_by_time == time(7, 0)
