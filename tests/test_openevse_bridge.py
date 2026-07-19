"""OpenEVSE amp normalization: never send 1-5 A; invalid -> 0."""

from services.openevse_bridge.main import normalize_amps


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
