"""OpenEVSE amp normalization, gateway/session helpers, and control MQTT mapping."""

import json

import pytest

from services.openevse_bridge.main import (
    control_command,
    gateway_state_from_announce,
    normalize_amps,
    session_connected,
)


def test_normalize_stop_and_floor():
    assert normalize_amps("0", i_min=6, i_max=48) == 0
    assert normalize_amps("5", i_min=6, i_max=48) == 0
    assert normalize_amps("6", i_min=6, i_max=48) == 6
    assert normalize_amps("32", i_min=6, i_max=48) == 32


def test_normalize_invalid_fails_safe():
    assert normalize_amps("nope", i_min=6, i_max=48) == 0
    assert normalize_amps("", i_min=6, i_max=48) == 0


def test_normalize_clamps_max():
    assert normalize_amps("99", i_min=6, i_max=48) == 48


def test_announce_connected_and_disconnected():
    """Why: WiFi drop must flip gateway offline from announce, not stale retained power."""
    assert gateway_state_from_announce('{"state":"connected","id":"x"}') is True
    assert gateway_state_from_announce('{"state":"disconnected","id":"x"}') is False
    assert gateway_state_from_announce("not-json") is None


def test_session_connected_treats_active_as_connected():
    """
    Why: OpenEVSE publishes status=active while charging; requiring the substring
    'connect' left HA stuck on connected=false during a live session.
    """
    assert session_connected(status="active", vehicle=None, state="3") is True
    assert session_connected(status="disabled", vehicle=None, state=None) is False
    assert session_connected(status="active", vehicle="0", state="3") is False
    assert session_connected(status="disabled", vehicle="1", state="0") is True


def test_claim_charge_forces_normal_divert_and_clears_override():
    """
    Why: Eco divert can claim at Priority_Limit (1100) and beat MQTT (500),
    leaving SETPOINT at ~6A while Max Current stays 32A.
    """
    charge = control_command("claim", 16, base_topic="openevse")
    assert charge[0] == ("openevse/divertmode/set", "1")
    assert charge[1] == ("openevse/override/set", "clear")
    assert charge[2][0] == "openevse/claim/set"
    assert json.loads(charge[2][1]) == {
        "state": "active",
        "charge_current": 16,
        "max_current": 16,
        "auto_release": True,
    }


def test_override_charge_releases_claim_and_forces_normal_divert():
    charge = control_command("override", 12, base_topic="openevse", auto_release=False)
    assert charge[0] == ("openevse/divertmode/set", "1")
    assert charge[1] == ("openevse/claim/set", "release")
    assert charge[2][0] == "openevse/override/set"
    assert json.loads(charge[2][1]) == {
        "state": "active",
        "charge_current": 12,
        "max_current": 12,
        "auto_release": False,
    }


def test_stop_disables_both_and_forces_normal_divert():
    """Why: stopping only one channel / leaving Eco on lets SETPOINT stick at 6A."""
    disabled = {"state": "disabled", "auto_release": True}
    for mode in ("claim", "override"):
        stop = control_command(mode, 0, base_topic="openevse")
        assert stop[0] == ("openevse/divertmode/set", "1")
        assert stop[1][0] == "openevse/claim/set"
        assert stop[2][0] == "openevse/override/set"
        assert json.loads(stop[1][1]) == disabled
        assert json.loads(stop[2][1]) == disabled


def test_stop_can_release_both_when_configured():
    stop = control_command("claim", 0, base_topic="openevse", stop_mode="release")
    assert stop == [
        ("openevse/claim/set", "release"),
        ("openevse/override/set", "clear"),
    ]


def test_rapi_legacy_fs_fc_sc():
    assert control_command("rapi", 0, base_topic="openevse") == [
        ("openevse/rapi/in/$FS", "")
    ]
    assert control_command("rapi", 24, base_topic="openevse") == [
        ("openevse/rapi/in/$FC", ""),
        ("openevse/rapi/in/$SC 24", ""),
    ]


def test_control_unknown_mode_raises():
    with pytest.raises(ValueError):
        control_command("nope", 6, base_topic="openevse")
