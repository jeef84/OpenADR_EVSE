"""Adaptive carbon permit thresholds from off-peak history + ready-by slack."""

from __future__ import annotations

import json
import logging
import math
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

from home_ev_flex.deadline import is_weekday_on_peak

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AdaptiveCarbonConfig:
    """Learn a clean floor from off-peak history; ratchet toward YAML as slack shrinks."""

    enabled: bool = False
    lookback_days: int = 14
    sample_interval_sec: float = 300.0
    percentile: float = 25.0
    min_samples: int = 288
    slack_high_hours: float = 4.0
    state_path: str = "/data/carbon_adaptive/carbon_history.json"


@dataclass(frozen=True)
class AdaptiveThresholdResult:
    """Effective gates plus diagnostics for status MQTT / logs."""

    co2_threshold: float | None
    fossil_threshold: float | None
    learned_co2_floor: float | None
    learned_fossil_floor: float | None
    urgency: float | None
    reason: str  # static | adaptive | cold_start | missing_slack | disabled
    sample_count: int


@dataclass
class _Sample:
    ts: float  # unix epoch seconds
    co2: float | None
    fossil: float | None


def slack_urgency(
    slack_hours: float | None,
    *,
    slack_high_hours: float,
    slack_low_hours: float,
) -> float | None:
    """
    Map ready-by slack to urgency in [0, 1].

    0 = use learned clean floor; 1 = use YAML ceiling.
    None slack → caller must keep static thresholds (fail closed).
    """
    if slack_hours is None:
        return None
    high = float(slack_high_hours)
    low = float(slack_low_hours)
    if high <= low:
        return 1.0 if float(slack_hours) <= low else 0.0
    if float(slack_hours) >= high:
        return 0.0
    if float(slack_hours) <= low:
        return 1.0
    return (high - float(slack_hours)) / (high - low)


def lerp(floor: float, ceiling: float, urgency: float) -> float:
    u = min(1.0, max(0.0, float(urgency)))
    return float(floor) + (float(ceiling) - float(floor)) * u


def percentile(values: Iterable[float], pct: float) -> float | None:
    """Linear-interpolation percentile; pct in [0, 100]."""
    data = sorted(float(v) for v in values)
    if not data:
        return None
    if len(data) == 1:
        return data[0]
    p = min(100.0, max(0.0, float(pct)))
    rank = (p / 100.0) * (len(data) - 1)
    lo = int(math.floor(rank))
    hi = int(math.ceil(rank))
    if lo == hi:
        return data[lo]
    frac = rank - lo
    return data[lo] + (data[hi] - data[lo]) * frac


def clamp_learned_floor(
    raw: float | None,
    *,
    min_threshold: float,
    ceiling: float,
) -> float | None:
    """Clamp percentile floor into [min_threshold, ceiling]."""
    if raw is None:
        return None
    return min(float(ceiling), max(float(min_threshold), float(raw)))


class CarbonHistoryStore:
    """
    Persisted off-peak carbon samples (downsampled).

    Thread-safe for the tariff-engine MQTT + tick threads.
    """

    def __init__(self, path: str | Path, *, lookback_days: int = 14) -> None:
        self.path = Path(path)
        self.lookback_days = int(lookback_days)
        self._lock = threading.Lock()
        self._samples: list[_Sample] = []
        self._last_sample_mono: float | None = None
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            items = raw.get("samples") if isinstance(raw, dict) else raw
            if not isinstance(items, list):
                return
            samples: list[_Sample] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                ts = item.get("ts")
                if ts is None:
                    continue
                samples.append(
                    _Sample(
                        ts=float(ts),
                        co2=_optional_num(item.get("co2")),
                        fossil=_optional_num(item.get("fossil")),
                    )
                )
            with self._lock:
                self._samples = samples
                self._prune_unlocked()
        except Exception:  # noqa: BLE001
            logger.exception("failed to load carbon history from %s", self.path)

    def save(
        self,
        *,
        percentile: float = 25.0,
        min_samples: int = 288,
        co2_ceiling: float | None = None,
        co2_min: float | None = None,
        fossil_ceiling: float | None = None,
        fossil_min: float | None = None,
    ) -> None:
        with self._lock:
            self._prune_unlocked()
            samples = list(self._samples)
            payload = {
                "samples": [
                    {"ts": s.ts, "co2": s.co2, "fossil": s.fossil}
                    for s in samples
                ]
            }
            text = json.dumps(payload, separators=(",", ":"))
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(self.path)
            write_carbon_history_html(
                self.path.with_name("carbon_history.html"),
                samples,
                lookback_days=self.lookback_days,
                percentile_pct=percentile,
                min_samples=min_samples,
                co2_ceiling=co2_ceiling,
                co2_min=co2_min,
                fossil_ceiling=fossil_ceiling,
                fossil_min=fossil_min,
            )
        except Exception:  # noqa: BLE001
            logger.exception("failed to persist carbon history to %s", self.path)

    def _prune_unlocked(self) -> None:
        if not self._samples:
            return
        cutoff = datetime.now().timestamp() - (self.lookback_days * 86400.0)
        self._samples = [s for s in self._samples if s.ts >= cutoff]

    def maybe_record(
        self,
        *,
        now: datetime,
        timezone: str,
        on_peak_start,
        on_peak_end,
        co2: float | None,
        fossil: float | None,
        sample_interval_sec: float,
        mono_now: float,
    ) -> bool:
        """
        Record one off-peak sample if the interval has elapsed.

        Returns True when a new sample was stored (caller may persist).
        """
        if co2 is None and fossil is None:
            return False
        if is_weekday_on_peak(
            now,
            timezone=timezone,
            on_peak_start=on_peak_start,
            on_peak_end=on_peak_end,
        ):
            return False
        with self._lock:
            if self._last_sample_mono is not None:
                if mono_now - self._last_sample_mono < float(sample_interval_sec):
                    return False
            self._samples.append(
                _Sample(ts=now.timestamp(), co2=co2, fossil=fossil)
            )
            self._last_sample_mono = mono_now
            self._prune_unlocked()
            return True

    def sample_count(self) -> int:
        with self._lock:
            return len(self._samples)

    def co2_values(self) -> list[float]:
        with self._lock:
            return [s.co2 for s in self._samples if s.co2 is not None]

    def fossil_values(self) -> list[float]:
        with self._lock:
            return [s.fossil for s in self._samples if s.fossil is not None]

    def seed_samples(
        self, samples: list[tuple[float, float | None, float | None]]
    ) -> None:
        """Replace in-memory history. Intended for unit tests."""
        with self._lock:
            self._samples = [
                _Sample(ts=float(ts), co2=co2, fossil=fossil)
                for ts, co2, fossil in samples
            ]
            self._prune_unlocked()


def write_carbon_history_html(
    path: str | Path,
    samples: list[_Sample],
    *,
    lookback_days: int,
    percentile_pct: float = 25.0,
    min_samples: int = 288,
    co2_ceiling: float | None = None,
    co2_min: float | None = None,
    fossil_ceiling: float | None = None,
    fossil_min: float | None = None,
) -> None:
    """
    Write a self-contained HTML history view next to the JSON store.

    Regenerated on each persist so opening the file in a browser shows the
    latest off-peak samples and learned floors. Auto-refreshes every 60s.
    """
    path = Path(path)
    co2_pts = [(s.ts, s.co2) for s in samples if s.co2 is not None]
    fossil_pts = [(s.ts, s.fossil) for s in samples if s.fossil is not None]
    co2_vals = [v for _, v in co2_pts]
    fossil_vals = [v for _, v in fossil_pts]
    p_co2 = percentile(co2_vals, percentile_pct)
    p_fossil = percentile(fossil_vals, percentile_pct)
    learned_co2 = (
        None
        if p_co2 is None or co2_ceiling is None
        else clamp_learned_floor(
            p_co2,
            min_threshold=float(co2_min if co2_min is not None else co2_ceiling),
            ceiling=float(co2_ceiling),
        )
    )
    learned_fossil = (
        None
        if p_fossil is None or fossil_ceiling is None
        else clamp_learned_floor(
            p_fossil,
            min_threshold=float(
                fossil_min if fossil_min is not None else fossil_ceiling
            ),
            ceiling=float(fossil_ceiling),
        )
    )
    cold = len(samples) < int(min_samples)
    updated = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    svg_co2 = _svg_series_chart(
        co2_pts,
        y_label="gCO2eq/kWh",
        title="Off-peak CO2 intensity (lookback window)",
        hlines=[
            ("learned p25 floor", learned_co2, "#2a7a4b"),
            ("YAML ceiling", co2_ceiling, "#6b7280"),
            ("min clamp", co2_min, "#9ca3af"),
        ],
    )
    svg_fossil = _svg_series_chart(
        fossil_pts,
        y_label="fossil %",
        title="Off-peak fossil fuel %",
        hlines=[
            ("learned p25 floor", learned_fossil, "#2a7a4b"),
            ("YAML ceiling", fossil_ceiling, "#6b7280"),
            ("min clamp", fossil_min, "#9ca3af"),
        ],
    )
    status = "cold_start (YAML gate)" if cold else "adaptive ready"
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta http-equiv="refresh" content="60"/>
<title>HOME EV FLEX carbon history</title>
<style>
  body {{ font-family: ui-sans-serif, system-ui, sans-serif; margin: 24px;
         color: #111; background: #fafafa; }}
  h1 {{ font-size: 1.35rem; margin: 0 0 0.25rem; }}
  .meta {{ color: #555; font-size: 0.9rem; margin-bottom: 1.25rem; }}
  .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
            gap: 12px; margin-bottom: 1.5rem; }}
  .stat {{ background: #fff; border: 1px solid #e5e5e5; border-radius: 8px; padding: 12px; }}
  .stat .k {{ color: #666; font-size: 0.75rem; }}
  .stat .v {{ font-size: 1.15rem; font-weight: 600; margin-top: 4px; }}
  .panel {{ background: #fff; border: 1px solid #e5e5e5; border-radius: 8px;
            padding: 12px; margin-bottom: 1rem; }}
  .pill {{ display: inline-block; padding: 2px 8px; border-radius: 999px;
           font-size: 0.75rem; background: #eef2ff; color: #3730a3; }}
  .pill.warn {{ background: #fef3c7; color: #92400e; }}
  svg {{ width: 100%; height: auto; display: block; }}
  code {{ font-size: 0.85rem; }}
</style>
</head>
<body>
  <h1>Adaptive carbon history</h1>
  <p class="meta">
    Updated {updated} · auto-refresh 60s · lookback {lookback_days}d off-peak only ·
    <span class="pill{' warn' if cold else ''}">{status}</span>
  </p>
  <div class="stats">
    <div class="stat"><div class="k">Samples</div>
      <div class="v">{len(samples)} / {min_samples}</div></div>
    <div class="stat"><div class="k">CO2 p{percentile_pct:g}</div>
      <div class="v">{_fmt(p_co2)} g</div></div>
    <div class="stat"><div class="k">Learned CO2 floor</div>
      <div class="v">{_fmt(learned_co2)} g</div></div>
    <div class="stat"><div class="k">CO2 min / max</div>
      <div class="v">{_fmt(min(co2_vals) if co2_vals else None)} / {_fmt(max(co2_vals) if co2_vals else None)}</div></div>
    <div class="stat"><div class="k">Fossil p{percentile_pct:g}</div>
      <div class="v">{_fmt(p_fossil)}%</div></div>
    <div class="stat"><div class="k">Learned fossil floor</div>
      <div class="v">{_fmt(learned_fossil)}%</div></div>
  </div>
  <div class="panel">{svg_co2}</div>
  <div class="panel">{svg_fossil}</div>
  <p class="meta">
    Data file: <code>{path.with_name("carbon_history.json").name}</code> ·
    Survives <code>docker compose stop</code> / <code>down</code> when using the
    bind mount under <code>data/carbon_adaptive/</code>. Removed only if you delete
    that directory or run <code>docker compose down -v</code> on a named volume.
  </p>
</body>
</html>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(html, encoding="utf-8")
    tmp.replace(path)


def _fmt(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.1f}"


def _svg_series_chart(
    points: list[tuple[float, float]],
    *,
    y_label: str,
    title: str,
    hlines: list[tuple[str, float | None, str]],
) -> str:
    width, height = 920, 280
    pad_l, pad_r, pad_t, pad_b = 52, 16, 28, 36
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    if not points:
        return (
            f"<svg viewBox='0 0 {width} {height}' xmlns='http://www.w3.org/2000/svg'>"
            f"<text x='{pad_l}' y='{height / 2}' fill='#666'>No samples yet</text>"
            f"</svg>"
        )
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    for _, hv, _ in hlines:
        if hv is not None:
            ys = ys + [hv]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    if x1 <= x0:
        x1 = x0 + 1.0
    pad_y = max(5.0, (y1 - y0) * 0.08)
    y0 -= pad_y
    y1 += pad_y

    def sx(t: float) -> float:
        return pad_l + (t - x0) / (x1 - x0) * plot_w

    def sy(v: float) -> float:
        return pad_t + (1.0 - (v - y0) / (y1 - y0)) * plot_h

    poly = " ".join(f"{sx(t):.1f},{sy(v):.1f}" for t, v in points)
    h_svg = []
    for label, hv, color in hlines:
        if hv is None:
            continue
        y = sy(hv)
        h_svg.append(
            f"<line x1='{pad_l}' y1='{y:.1f}' x2='{width - pad_r}' y2='{y:.1f}' "
            f"stroke='{color}' stroke-dasharray='4 4' stroke-width='1'/>"
            f"<text x='{width - pad_r - 4}' y='{y - 4:.1f}' text-anchor='end' "
            f"fill='{color}' font-size='11'>{label} {hv:.1f}</text>"
        )
    # Simple time ticks: start / mid / end
    tick_ts = [x0, (x0 + x1) / 2, x1]
    ticks = []
    for t in tick_ts:
        label = datetime.fromtimestamp(t).strftime("%m-%d %H:%M")
        ticks.append(
            f"<text x='{sx(t):.1f}' y='{height - 10}' text-anchor='middle' "
            f"fill='#666' font-size='11'>{label}</text>"
        )
    y_ticks = [y0, (y0 + y1) / 2, y1]
    yt = []
    for v in y_ticks:
        yt.append(
            f"<text x='{pad_l - 8}' y='{sy(v) + 4:.1f}' text-anchor='end' "
            f"fill='#666' font-size='11'>{v:.0f}</text>"
        )
    return f"""<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">
  <text x="{pad_l}" y="18" fill="#111" font-size="13" font-weight="600">{title}</text>
  <text x="12" y="{pad_t + plot_h / 2}" fill="#666" font-size="11"
        transform="rotate(-90 12,{pad_t + plot_h / 2})">{y_label}</text>
  <rect x="{pad_l}" y="{pad_t}" width="{plot_w}" height="{plot_h}"
        fill="#fcfcfc" stroke="#e5e5e5"/>
  {"".join(h_svg)}
  <polyline fill="none" stroke="#2563eb" stroke-width="1.5" points="{poly}"/>
  {"".join(ticks)}
  {"".join(yt)}
</svg>"""


def resolve_adaptive_thresholds(
    *,
    adaptive: AdaptiveCarbonConfig,
    co2_ceiling: float | None,
    fossil_ceiling: float | None,
    co2_min: float | None,
    fossil_min: float | None,
    history: CarbonHistoryStore | None,
    slack_hours: float | None,
    slack_low_hours: float,
) -> AdaptiveThresholdResult:
    """
    Compute effective permit thresholds.

    Fail closed to YAML ceilings when adaptive is off, cold, or slack missing.
    """
    if not adaptive.enabled:
        return AdaptiveThresholdResult(
            co2_threshold=co2_ceiling,
            fossil_threshold=fossil_ceiling,
            learned_co2_floor=None,
            learned_fossil_floor=None,
            urgency=None,
            reason="disabled",
            sample_count=0 if history is None else history.sample_count(),
        )

    count = 0 if history is None else history.sample_count()
    if history is None or count < adaptive.min_samples:
        return AdaptiveThresholdResult(
            co2_threshold=co2_ceiling,
            fossil_threshold=fossil_ceiling,
            learned_co2_floor=None,
            learned_fossil_floor=None,
            urgency=None,
            reason="cold_start",
            sample_count=count,
        )

    urgency = slack_urgency(
        slack_hours,
        slack_high_hours=adaptive.slack_high_hours,
        slack_low_hours=slack_low_hours,
    )
    if urgency is None:
        return AdaptiveThresholdResult(
            co2_threshold=co2_ceiling,
            fossil_threshold=fossil_ceiling,
            learned_co2_floor=None,
            learned_fossil_floor=None,
            urgency=None,
            reason="missing_slack",
            sample_count=count,
        )

    learned_co2 = None
    if co2_ceiling is not None:
        raw = percentile(history.co2_values(), adaptive.percentile)
        learned_co2 = clamp_learned_floor(
            raw,
            min_threshold=float(co2_min if co2_min is not None else co2_ceiling),
            ceiling=float(co2_ceiling),
        )

    learned_fossil = None
    if fossil_ceiling is not None:
        raw = percentile(history.fossil_values(), adaptive.percentile)
        learned_fossil = clamp_learned_floor(
            raw,
            min_threshold=float(
                fossil_min if fossil_min is not None else fossil_ceiling
            ),
            ceiling=float(fossil_ceiling),
        )

    eff_co2 = (
        None
        if learned_co2 is None or co2_ceiling is None
        else lerp(learned_co2, float(co2_ceiling), urgency)
    )
    eff_fossil = (
        None
        if learned_fossil is None or fossil_ceiling is None
        else lerp(learned_fossil, float(fossil_ceiling), urgency)
    )

    # If a signal had no history values, keep YAML ceiling for that signal.
    if co2_ceiling is not None and eff_co2 is None:
        eff_co2 = float(co2_ceiling)
    if fossil_ceiling is not None and eff_fossil is None:
        eff_fossil = float(fossil_ceiling)

    return AdaptiveThresholdResult(
        co2_threshold=eff_co2,
        fossil_threshold=eff_fossil,
        learned_co2_floor=learned_co2,
        learned_fossil_floor=learned_fossil,
        urgency=urgency,
        reason="adaptive",
        sample_count=count,
    )


def _optional_num(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

