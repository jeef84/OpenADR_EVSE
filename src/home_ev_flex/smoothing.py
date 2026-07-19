"""Lightweight filters for noisy site telemetry on the control path."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class EmaFilter:
    """
    Exponential moving average.

    alpha is the weight on the newest sample (0 < alpha <= 1). Larger alpha
    tracks faster; smaller alpha rejects more MQTT / CT chatter.
    """

    alpha: float
    value: float | None = None

    def __post_init__(self) -> None:
        if not 0.0 < self.alpha <= 1.0:
            raise ValueError("alpha must be in (0, 1]")

    def update(self, sample: float) -> float:
        if self.value is None:
            self.value = float(sample)
        else:
            self.value = self.alpha * float(sample) + (1.0 - self.alpha) * self.value
        return self.value

    def reset(self) -> None:
        self.value = None
