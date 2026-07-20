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
    slack_hours: float  # off-peak hours available minus hours_needed
    force: bool
    soc_assumed: bool
    reason: str  # inactive | ok | force | force_wait_off_peak | soc_assumed


def parse_ready_by_hhmm(value: str) -> time:
    """Parse sticky ready-by clock from 'HH:MM' or 'H:MM'."""
    text = value.strip()
    hour_s, minute_s = text.split(":", 1)
    return time(hour=int(hour_s), minute=int(minute_s))


def is_weekday_on_peak(
    now: datetime,
    *,
    timezone: str,
    on_peak_start: time,
    on_peak_end: time,
) -> bool:
    """True during weekday TOU on-peak; weekends are always off-peak."""
    local = now.astimezone(ZoneInfo(timezone))
    if local.weekday() >= 5:
        return False
    t = local.time()
    return on_peak_start <= t < on_peak_end


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


def hours_until_ready_by_clock(
    now: datetime,
    ready_by: time,
    *,
    timezone: str,
) -> float:
    """
    Hours until the next ready-by clock in site timezone.

    After today's ready-by has passed, roll forward to tomorrow so daytime
    plug-ins keep economic/solar slack instead of forcing all afternoon.
    """
    local = now.astimezone(ZoneInfo(timezone))
    target = next_ready_by_datetime(now, ready_by, timezone=timezone)
    return max(0.0, (target - local).total_seconds() / 3600.0)


def off_peak_hours_until(
    now: datetime,
    until: datetime,
    *,
    timezone: str,
    on_peak_start: time,
    on_peak_end: time,
) -> float:
    """
    Force-eligible (off-peak) hours in [now, until).

    Weekends count fully. Weekday on-peak windows do not count toward slack.
    """
    local_now = now.astimezone(ZoneInfo(timezone))
    local_until = until.astimezone(ZoneInfo(timezone))
    if local_until <= local_now:
        return 0.0

    total_sec = 0.0
    cursor = local_now
    while cursor < local_until:
        next_midnight = (cursor + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        if cursor.weekday() >= 5:
            chunk_end = min(local_until, next_midnight)
            total_sec += (chunk_end - cursor).total_seconds()
            cursor = chunk_end
            continue

        peak_start_dt = cursor.replace(
            hour=on_peak_start.hour,
            minute=on_peak_start.minute,
            second=0,
            microsecond=0,
        )
        peak_end_dt = cursor.replace(
            hour=on_peak_end.hour,
            minute=on_peak_end.minute,
            second=0,
            microsecond=0,
        )
        t = cursor.time()
        if t < on_peak_start:
            chunk_end = min(local_until, peak_start_dt)
            total_sec += (chunk_end - cursor).total_seconds()
            cursor = chunk_end
        elif t < on_peak_end:
            cursor = min(local_until, peak_end_dt)
        else:
            chunk_end = min(local_until, next_midnight)
            total_sec += (chunk_end - cursor).total_seconds()
            cursor = chunk_end

    return total_sec / 3600.0


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
    on_peak_start: time = time(11, 0),
    on_peak_end: time = time(19, 0),
) -> DeadlineDecision:
    """
    Decide whether the deadline overlay must force charge.

    Slack is off-peak hours until the next ready-by minus charge time needed.
    Force only when that slack is gone **and** the TOU window is currently
    off-peak (never force grid import during weekday on-peak).
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
    deadline_at = next_ready_by_datetime(now, ready_by_time, timezone=timezone)
    hours_until = max(
        0.0,
        (
            deadline_at.astimezone(ZoneInfo(timezone))
            - now.astimezone(ZoneInfo(timezone))
        ).total_seconds()
        / 3600.0,
    )
    off_peak_h = off_peak_hours_until(
        now,
        deadline_at,
        timezone=timezone,
        on_peak_start=on_peak_start,
        on_peak_end=on_peak_end,
    )
    slack = off_peak_h - hours_needed
    need_force = needed > 0 and slack <= float(cushion_hours)
    on_peak = is_weekday_on_peak(
        now,
        timezone=timezone,
        on_peak_start=on_peak_start,
        on_peak_end=on_peak_end,
    )
    force = need_force and not on_peak

    if force:
        reason = "force"
    elif need_force and on_peak:
        reason = "force_wait_off_peak"
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
