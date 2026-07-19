"""OpenEVSE amp normalization and gateway/session helpers."""

from services.openevse_bridge.main import (
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
