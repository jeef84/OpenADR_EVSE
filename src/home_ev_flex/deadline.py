"""Ready-by-departure deadline overlay: SOC tracking, slack, and force decision."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class ReadyByConfig:
    """Sticky site defaults for deadline overlay (YAML + MQTT overrides)."""

    cushion_hours: float = 0.25
    assumed_soc_pct: float = 40.0
    battery_capacity_kwh: float = 74.7
    target_soc_pct: float = 85.0
    ready_by_time: time = time(7, 0)
    enabled_default: bool = True


@dataclass(frozen=True)
class DeadlineDecision:
    """Result of one deadline evaluation tick."""

    effective_soc_pct: float
    energy_needed_kwh: float
    hours_needed: float
    hours_until_ready_by: float
    slack_hours: float
    force: bool
    soc_assumed: bool
    reason: str  # inactive | ok | force | soc_assumed


def parse_ready_by_hhmm(value: str) -> time:
    """Parse sticky ready-by clock from 'HH:MM' or 'H:MM'."""
    text = value.strip()
    hour_s, minute_s = text.split(":", 1)
    return time(hour=int(hour_s), minute=int(minute_s))


def hours_until_ready_by_clock(
    now: datetime,
    ready_by: time,
    *,
    timezone: str,
) -> float:
    """
    Hours until today's ready-by clock in site timezone.

    If local time is already past today's ready-by, return 0.0 so unmet energy
    is treated as overdue (force), not deferred to tomorrow.
    """
    local = now.astimezone(ZoneInfo(timezone))
    today_deadline = local.replace(
        hour=ready_by.hour,
        minute=ready_by.minute,
        second=0,
        microsecond=0,
    )
    if local <= today_deadline:
        return max(0.0, (today_deadline - local).total_seconds() / 3600.0)
    return 0.0


def effective_soc_pct(
    soc_pct: float,
    *,
    assumed_soc_pct: float,
    energy_added_kwh: float,
    battery_capacity_kwh: float,
    tracking_active: bool,
) -> tuple[float, bool]:
    """
    Return (effective_soc, soc_assumed).

    Missing/zero SOC uses assumed_soc_pct with no energy accrual (fail closed toward
    charging). With a real parked SOC, add energy_added_kwh (from power integration
    or meter delta since the SOC was set).
    """
    soc_assumed = float(soc_pct) <= 0.0
    base = float(assumed_soc_pct) if soc_assumed else float(soc_pct)
    if soc_assumed or not tracking_active or battery_capacity_kwh <= 0:
        return min(100.0, max(0.0, base)), soc_assumed
    added_pct = float(energy_added_kwh) / float(battery_capacity_kwh) * 100.0
    return min(100.0, max(0.0, base + added_pct)), False


def energy_needed_kwh(
    *,
    effective_soc: float,
    target_soc_pct: float,
    battery_capacity_kwh: float,
) -> float:
    if battery_capacity_kwh <= 0:
        return 0.0
    return max(0.0, (float(target_soc_pct) - float(effective_soc)) / 100.0 * float(battery_capacity_kwh))


def charge_power_kw(*, user_amps: int, i_max_amps: int, voltage_v: float) -> float:
    if voltage_v <= 0:
        return 0.0
    amps = min(int(user_amps), int(i_max_amps))
    if amps <= 0:
        return 0.0
    return (amps * float(voltage_v)) / 1000.0


def evaluate_deadline(
    *,
    ready_by_enabled: bool,
    soc_pct: float,
    target_soc_pct: float,
    battery_capacity_kwh: float,
    assumed_soc_pct: float,
    cushion_hours: float,
    ready_by_time: time,
    timezone: str,
    now: datetime,
    energy_added_kwh: float,
    soc_tracking_active: bool,
    user_amps: int,
    i_max_amps: int,
    voltage_v: float,
) -> DeadlineDecision:
    """
    Decide whether the deadline overlay must force charge.

    Prefer cheap/clean while slack > cushion; force when remaining energy cannot
    wait until the ready-by clock (or clock already passed with energy still needed).
    """
    if not ready_by_enabled:
        return DeadlineDecision(
            effective_soc_pct=0.0,
            energy_needed_kwh=0.0,
            hours_needed=0.0,
            hours_until_ready_by=0.0,
            slack_hours=0.0,
            force=False,
            soc_assumed=float(soc_pct) <= 0.0,
            reason="inactive",
        )

    eff, assumed = effective_soc_pct(
        soc_pct,
        assumed_soc_pct=assumed_soc_pct,
        energy_added_kwh=energy_added_kwh,
        battery_capacity_kwh=battery_capacity_kwh,
        tracking_active=soc_tracking_active,
    )
    needed = energy_needed_kwh(
        effective_soc=eff,
        target_soc_pct=target_soc_pct,
        battery_capacity_kwh=battery_capacity_kwh,
    )
    charge_kw = charge_power_kw(
        user_amps=user_amps, i_max_amps=i_max_amps, voltage_v=voltage_v
    )
    hours_needed = (needed / charge_kw) if charge_kw > 0 and needed > 0 else 0.0
    hours_until = hours_until_ready_by_clock(now, ready_by_time, timezone=timezone)
    slack = hours_until - hours_needed
    force = needed > 0 and slack <= float(cushion_hours)

    if force:
        reason = "force"
    elif assumed:
        reason = "soc_assumed"
    else:
        reason = "ok"

    return DeadlineDecision(
        effective_soc_pct=eff,
        energy_needed_kwh=needed,
        hours_needed=hours_needed,
        hours_until_ready_by=hours_until,
        slack_hours=slack,
        force=force,
        soc_assumed=assumed,
        reason=reason,
    )


def next_ready_by_datetime(
    now: datetime,
    ready_by: time,
    *,
    timezone: str,
) -> datetime:
    """Next calendar occurrence of ready-by (tomorrow if already past today)."""
    local = now.astimezone(ZoneInfo(timezone))
    today = local.replace(
        hour=ready_by.hour,
        minute=ready_by.minute,
        second=0,
        microsecond=0,
    )
    if local <= today:
        return today
    return today + timedelta(days=1)
