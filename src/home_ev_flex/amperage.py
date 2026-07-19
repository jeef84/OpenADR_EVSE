"""Discrete OpenEVSE amperage: floor quantization, I_min stop, amp hysteresis."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class AmpCommand:
    """Integer control-pilot current limit. Zero means stop charging."""

    amps: int
    reason: str


@dataclass
class AmpController:
    """
    Owns integer amp quantization and hysteresis.

    Deadband is amp hysteresis on continuous (raw) amps, not a watt offset like
    solar_surplus - 300 W.
    """

    i_min_amps: int = 6
    i_max_amps: int = 40
    hysteresis_amps: float = 0.75
    _last_cmd: int = 0

    @property
    def last_command_amps(self) -> int:
        return self._last_cmd

    def reset(self) -> None:
        self._last_cmd = 0

    def power_to_raw_amps(self, power_kw: float, voltage_v: float) -> float:
        if voltage_v <= 0:
            raise ValueError("voltage_v must be positive")
        if power_kw <= 0:
            return 0.0
        return (power_kw * 1000.0) / voltage_v

    def floor_amps(self, raw_amps: float) -> int:
        """Floor continuous current; never round up past available power."""
        if raw_amps <= 0:
            return 0
        return math.floor(raw_amps)

    def quantize_raw(self, raw_amps: float) -> AmpCommand:
        """Map raw continuous amps to stop or floored integer within limits."""
        floored = self.floor_amps(raw_amps)
        if floored < self.i_min_amps:
            return AmpCommand(amps=0, reason="below_i_min")
        return AmpCommand(amps=min(floored, self.i_max_amps), reason="economic")

    def quantize(self, power_kw: float, voltage_v: float) -> AmpCommand:
        return self.quantize_raw(self.power_to_raw_amps(power_kw, voltage_v))

    def apply_hysteresis(self, raw_amps: float, candidate: AmpCommand) -> AmpCommand:
        """
        Hold the last integer setpoint through small raw-amp noise.

        Raise only when raw clears last + hysteresis into a higher bucket.
        Lower only when raw falls below last - hysteresis.
        """
        last = self._last_cmd
        new = candidate.amps

        if last == 0:
            self._last_cmd = new
            return candidate

        if new == last:
            return candidate

        if new > last:
            if raw_amps >= last + self.hysteresis_amps:
                self._last_cmd = new
                return candidate
            return AmpCommand(amps=last, reason="hysteresis_hold_up")

        # new < last (including stop)
        if raw_amps <= last - self.hysteresis_amps:
            self._last_cmd = new
            return candidate
        return AmpCommand(amps=last, reason="hysteresis_hold_down")

    def command_for_power(self, power_kw: float, voltage_v: float) -> AmpCommand:
        """Quantize then apply hysteresis against the previous command."""
        raw = self.power_to_raw_amps(power_kw, voltage_v)
        candidate = self.quantize_raw(raw)
        return self.apply_hysteresis(raw, candidate)

    def charge_now(self, user_amps: int) -> AmpCommand:
        """
        Manual override: integer user amps, still clamped by hard max / I_min.

        Bypasses economic signals only. Never bypasses safety clamps here.
        """
        if user_amps < self.i_min_amps:
            cmd = AmpCommand(amps=0, reason="charge_now_below_i_min")
        else:
            cmd = AmpCommand(
                amps=min(user_amps, self.i_max_amps),
                reason="charge_now",
            )
        self._last_cmd = cmd.amps
        return cmd
