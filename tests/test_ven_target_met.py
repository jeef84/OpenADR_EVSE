"""Target ceiling latch: raising sticky target must reopen charging."""

from __future__ import annotations

from datetime import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from home_ev_flex.tariff import load_tariff_config
from services.ven_adapter.main import LocalState, VenAdapter, topics


def _adapter() -> VenAdapter:
    """Build a VenAdapter without MQTT/OpenADR env (unit-test harness)."""
    adapter = VenAdapter.__new__(VenAdapter)
    path = Path(__file__).resolve().parents[1] / "config" / "tariff.yaml"
    adapter.cfg = load_tariff_config(path)
    adapter.state = LocalState(
        target_soc_pct=75.0,
        battery_capacity_kwh=74.7,
        ready_by_time=time(7, 0),
        ready_by_enabled=False,
    )
    adapter._mqtt = MagicMock()
    adapter._allow_soc_tracking_publish = False
    return adapter


def _site(adapter: VenAdapter, **overrides: float | bool) -> dict:
    with adapter.state.lock:
        base = {
            "soc_pct": adapter.state.soc_pct,
            "target_soc_pct": adapter.state.target_soc_pct,
            "battery_capacity_kwh": adapter.state.battery_capacity_kwh,
            "energy_added_since_soc_kwh": adapter.state.energy_added_since_soc_kwh,
            "soc_tracking_active": adapter.state.soc_tracking_active,
        }
    base.update(overrides)
    return base


def test_raising_target_clears_latched_target_met() -> None:
    """Why: overnight 81%>75% latch must not block solar once target rises to 85%."""
    adapter = _adapter()
    adapter.state.soc_pct = 81.0
    adapter.state.last_soc_for_snapshot = 81.0
    adapter.state.soc_tracking_active = True
    adapter.state.energy_added_since_soc_kwh = 0.0
    adapter.state.target_soc_pct = 75.0
    adapter.state.target_met = True

    assert adapter._target_reached(_site(adapter)) is True

    adapter.state.target_soc_pct = 85.0
    assert adapter._target_reached(_site(adapter)) is False
    assert adapter.state.target_met is False


def test_target_soc_mqtt_clears_latch_when_raised() -> None:
    """Why: HA target slider must reopen charge without waiting for a SOC republish."""
    adapter = _adapter()
    adapter.state.target_soc_pct = 75.0
    adapter.state.target_met = True
    msg = SimpleNamespace(topic=topics.TARGET_SOC_PCT, payload=b"85.0")
    adapter._on_message(None, None, msg)
    assert adapter.state.target_soc_pct == pytest.approx(85.0)
    assert adapter.state.target_met is False


def test_restore_does_not_invent_energy_for_higher_target() -> None:
    """Why: retained target_met from a lower target must not floor kWh to the new target."""
    adapter = _adapter()
    adapter.state.target_soc_pct = 85.0
    adapter.state.soc_pct = 81.0
    payload = (
        '{"baseline_soc_pct": 81.0, "energy_added_kwh": 0.0, "target_met": true}'
    )
    adapter._restore_soc_tracking(payload)
    assert adapter.state.target_met is False
    assert adapter.state.energy_added_since_soc_kwh == pytest.approx(0.0)
    assert adapter.state.last_soc_for_snapshot == pytest.approx(81.0)


def test_restore_keeps_latch_when_still_at_target() -> None:
    """Why: restart must not reopen charge when sticky target is still met."""
    adapter = _adapter()
    adapter.state.target_soc_pct = 75.0
    adapter.state.soc_pct = 81.0
    payload = (
        '{"baseline_soc_pct": 81.0, "energy_added_kwh": 0.0, "target_met": true}'
    )
    adapter._restore_soc_tracking(payload)
    assert adapter.state.target_met is True
