"""Adaptive carbon thresholds: history floor, slack ratchet, fail-closed paths."""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from home_ev_flex.carbon_adaptive import (
    AdaptiveCarbonConfig,
    CarbonHistoryStore,
    clamp_learned_floor,
    lerp,
    percentile,
    resolve_adaptive_thresholds,
    slack_urgency,
)
from home_ev_flex.tariff import (
    CarbonPriceConfig,
    CarbonSignalConfig,
    carbon_adder_per_kwh,
    effective_carbon_thresholds,
    effective_import_price,
    load_tariff_config,
)

ROOT = Path(__file__).resolve().parents[1]
TARIFF_PATH = ROOT / "config" / "tariff.yaml"


def _history_with_co2(tmp_path: Path, values: list[float]) -> CarbonHistoryStore:
    store = CarbonHistoryStore(tmp_path / "carbon_history.json", lookback_days=14)
    now = time.time()
    store.seed_samples([(now - i * 300.0, v, 60.0) for i, v in enumerate(values)])
    return store


def test_percentile_p25_of_uniform_range():
    """Why: learned floor must be a real quartile, not the min or mean."""
    values = list(range(100, 500, 4))  # 100 samples
    p25 = percentile(values, 25)
    assert p25 is not None
    assert 180.0 <= p25 <= 220.0


def test_slack_urgency_endpoints_and_mid():
    """Why: high slack must prefer clean floor; near cushion must open to YAML ceiling."""
    assert slack_urgency(5.0, slack_high_hours=4.0, slack_low_hours=0.25) == pytest.approx(0.0)
    assert slack_urgency(0.1, slack_high_hours=4.0, slack_low_hours=0.25) == pytest.approx(1.0)
    mid = slack_urgency(2.125, slack_high_hours=4.0, slack_low_hours=0.25)
    assert mid == pytest.approx(0.5)
    assert slack_urgency(None, slack_high_hours=4.0, slack_low_hours=0.25) is None


def test_lerp_and_min_floor_clamp():
    assert lerp(400.0, 580.0, 0.0) == pytest.approx(400.0)
    assert lerp(400.0, 580.0, 1.0) == pytest.approx(580.0)
    assert clamp_learned_floor(200.0, min_threshold=350.0, ceiling=580.0) == pytest.approx(
        350.0
    )
    assert clamp_learned_floor(600.0, min_threshold=350.0, ceiling=580.0) == pytest.approx(
        580.0
    )


def test_cold_start_uses_yaml_ceiling(tmp_path: Path):
    """Why: adapting before enough off-peak history would invent an unreachable gate."""
    store = _history_with_co2(tmp_path, [400.0] * 10)
    adaptive = AdaptiveCarbonConfig(enabled=True, min_samples=288, percentile=25)
    result = resolve_adaptive_thresholds(
        adaptive=adaptive,
        co2_ceiling=580.0,
        fossil_ceiling=80.0,
        co2_min=350.0,
        fossil_min=50.0,
        history=store,
        slack_hours=6.0,
        slack_low_hours=0.25,
    )
    assert result.reason == "cold_start"
    assert result.co2_threshold == pytest.approx(580.0)
    assert result.learned_co2_floor is None


def test_missing_slack_fail_closed_to_yaml(tmp_path: Path):
    """Why: without VEN slack, do not silently over-tighten the carbon gate."""
    store = _history_with_co2(tmp_path, [400.0 + (i % 40) for i in range(300)])
    adaptive = AdaptiveCarbonConfig(enabled=True, min_samples=288, percentile=25)
    result = resolve_adaptive_thresholds(
        adaptive=adaptive,
        co2_ceiling=580.0,
        fossil_ceiling=80.0,
        co2_min=350.0,
        fossil_min=50.0,
        history=store,
        slack_hours=None,
        slack_low_hours=0.25,
    )
    assert result.reason == "missing_slack"
    assert result.co2_threshold == pytest.approx(580.0)


def test_adaptive_high_slack_uses_learned_floor(tmp_path: Path):
    """Why: with overnight slack, permit only cleaner-than-YAML off-peak energy."""
    # Mostly ~400g with a dirty tail so p25 stays near the clean cluster.
    values = [400.0] * 225 + [520.0] * 75
    store = _history_with_co2(tmp_path, values)
    adaptive = AdaptiveCarbonConfig(
        enabled=True, min_samples=288, percentile=25, slack_high_hours=4.0
    )
    result = resolve_adaptive_thresholds(
        adaptive=adaptive,
        co2_ceiling=580.0,
        fossil_ceiling=80.0,
        co2_min=350.0,
        fossil_min=50.0,
        history=store,
        slack_hours=6.0,
        slack_low_hours=0.25,
    )
    assert result.reason == "adaptive"
    assert result.urgency == pytest.approx(0.0)
    assert result.learned_co2_floor is not None
    assert result.learned_co2_floor < 580.0
    assert result.co2_threshold == pytest.approx(result.learned_co2_floor)


def test_adaptive_low_slack_ratchets_to_ceiling(tmp_path: Path):
    """Why: as ready-by approaches, accept up to YAML before deadline_force."""
    values = [400.0] * 300
    store = _history_with_co2(tmp_path, values)
    adaptive = AdaptiveCarbonConfig(
        enabled=True, min_samples=288, percentile=25, slack_high_hours=4.0
    )
    result = resolve_adaptive_thresholds(
        adaptive=adaptive,
        co2_ceiling=580.0,
        fossil_ceiling=80.0,
        co2_min=350.0,
        fossil_min=50.0,
        history=store,
        slack_hours=0.2,
        slack_low_hours=0.25,
    )
    assert result.reason == "adaptive"
    assert result.urgency == pytest.approx(1.0)
    assert result.co2_threshold == pytest.approx(580.0)


def test_site_tariff_adaptive_blocks_mid_clean_at_high_slack(tmp_path: Path):
    """
    Why: tonight-like 514 g must be blockable once history learns below the YAML 580 gate.
    """
    cfg = load_tariff_config(TARIFF_PATH)
    assert cfg.carbon_price.adaptive.enabled
    values = [450.0] * 300
    store = _history_with_co2(tmp_path, values)
    thr = effective_carbon_thresholds(
        cfg.carbon_price,
        history=store,
        slack_hours=8.0,
        slack_low_hours=cfg.ready_by.cushion_hours,
    )
    assert thr.reason == "adaptive"
    assert thr.co2_threshold is not None
    assert thr.co2_threshold < 514.0

    adder, reason = carbon_adder_per_kwh(
        cfg.carbon_price,
        co2_intensity_g_per_kwh=514.0,
        fossil_fuel_pct=69.0,
        co2_threshold=thr.co2_threshold,
        fossil_threshold=thr.fossil_threshold,
    )
    assert reason == "signal"
    assert adder == pytest.approx(0.50)


def test_off_peak_only_sampling_skips_weekday_on_peak(tmp_path: Path):
    """Why: daytime dirty peaks must not pull the learned clean floor."""
    cfg = load_tariff_config(TARIFF_PATH)
    store = CarbonHistoryStore(tmp_path / "h.json", lookback_days=14)
    tz = ZoneInfo(cfg.timezone)
    # Use a recent Wednesday so lookback prune does not drop the sample.
    on_peak = datetime(2026, 7, 29, 14, 0, tzinfo=tz)
    assert not store.maybe_record(
        now=on_peak,
        timezone=cfg.timezone,
        on_peak_start=cfg.weekday_on_peak_start,
        on_peak_end=cfg.weekday_on_peak_end,
        co2=700.0,
        fossil=90.0,
        sample_interval_sec=300,
        mono_now=1000.0,
    )
    off_peak = datetime(2026, 7, 29, 21, 0, tzinfo=tz)
    assert store.maybe_record(
        now=off_peak,
        timezone=cfg.timezone,
        on_peak_start=cfg.weekday_on_peak_start,
        on_peak_end=cfg.weekday_on_peak_end,
        co2=420.0,
        fossil=55.0,
        sample_interval_sec=300,
        mono_now=1000.0,
    )
    assert store.sample_count() == 1
    assert store.co2_values()[0] == pytest.approx(420.0)


def test_history_persists_across_reload(tmp_path: Path):
    """Why: container restarts must not wipe the lookback window."""
    path = tmp_path / "carbon_history.json"
    store = CarbonHistoryStore(path, lookback_days=14)
    store.seed_samples([(time.time(), 410.0, 58.0)] * 5)
    store.save(co2_ceiling=580.0, co2_min=350.0, fossil_ceiling=80.0, fossil_min=50.0)
    reloaded = CarbonHistoryStore(path, lookback_days=14)
    assert reloaded.sample_count() == 5
    assert reloaded.co2_values()[0] == pytest.approx(410.0)
    html = tmp_path / "carbon_history.html"
    assert html.exists()
    text = html.read_text(encoding="utf-8")
    assert "Adaptive carbon history" in text
    assert "410" in text or "learned" in text.lower()


def test_history_html_shows_empty_state(tmp_path: Path):
    """Why: fresh install must still produce a readable view, not a blank file."""
    from home_ev_flex.carbon_adaptive import write_carbon_history_html

    html_path = tmp_path / "carbon_history.html"
    write_carbon_history_html(
        html_path,
        [],
        lookback_days=14,
        min_samples=288,
        co2_ceiling=580.0,
        co2_min=350.0,
    )
    assert "No samples yet" in html_path.read_text(encoding="utf-8")


def test_effective_import_price_cold_start_matches_static_yaml():
    """Why: before history accumulates, adaptive must not change TOU economics."""
    cfg = load_tariff_config(TARIFF_PATH)
    tz = ZoneInfo(cfg.timezone)
    off_peak = datetime(2026, 7, 15, 20, 0, tzinfo=tz)
    import_eff, adder, _ = effective_import_price(
        cfg,
        off_peak,
        co2_intensity_g_per_kwh=514.0,
        fossil_fuel_pct=69.0,
        history=None,
        slack_hours=8.0,
    )
    assert adder == pytest.approx(0.0)
    assert import_eff == pytest.approx(cfg.weekday_off_peak_price)


def test_disabled_adaptive_ignores_history(tmp_path: Path):
    carbon = CarbonPriceConfig(
        enabled=True,
        co2_intensity=CarbonSignalConfig(
            threshold=580.0, max_adder_per_kwh=0.50, min_threshold=350.0
        ),
        adaptive=AdaptiveCarbonConfig(enabled=False, min_samples=1),
    )
    store = _history_with_co2(tmp_path, [400.0] * 50)
    thr = effective_carbon_thresholds(
        carbon, history=store, slack_hours=8.0, slack_low_hours=0.25
    )
    assert thr.reason == "disabled"
    assert thr.co2_threshold == pytest.approx(580.0)
