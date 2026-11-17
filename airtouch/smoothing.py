"""One Euro filter for cursor smoothing.

Reference: Casiez, Roussel & Vogel (CHI 2012), "1€ Filter: A Simple
Speed-based Low-pass Filter for Noisy Input in Interactive Systems".

Low speeds get heavy smoothing (kills jitter); high speeds reduce the
smoothing (kills lag). `min_cutoff` controls jitter at rest, `beta`
controls how aggressively the cutoff opens up with speed.
"""

from __future__ import annotations

import math


class LowPassFilter:
    """Exponential low-pass filter with a runtime-variable alpha."""

    def __init__(self) -> None:
        self._prev: float | None = None

    def apply(self, value: float, alpha: float) -> float:
        """Filter one sample; the first sample passes through unchanged."""
        if self._prev is None:
            self._prev = value
        else:
            self._prev = alpha * value + (1.0 - alpha) * self._prev
        return self._prev

    def reset(self) -> None:
        """Forget filter history (use after tracking loss)."""
        self._prev = None


class OneEuroFilter:
    """One Euro filter for a single scalar signal."""

    def __init__(self, min_cutoff: float = 1.0, beta: float = 0.007,
                 d_cutoff: float = 1.0) -> None:
        self.min_cutoff = min_cutoff
        self.beta = beta
        self.d_cutoff = d_cutoff
        self._x = LowPassFilter()
        self._dx = LowPassFilter()
        self._last_t: float | None = None
        self._last_raw: float | None = None

    @staticmethod
    def _alpha(cutoff: float, dt: float) -> float:
        tau = 1.0 / (2.0 * math.pi * cutoff)
        return 1.0 / (1.0 + tau / dt)

    def apply(self, value: float, timestamp: float) -> float:
        """Filter one sample taken at `timestamp` (seconds)."""
        if self._last_t is None or timestamp <= self._last_t:
            dt = 1.0 / 30.0
        else:
            dt = timestamp - self._last_t
        self._last_t = timestamp

        dx = 0.0 if self._last_raw is None else (value - self._last_raw) / dt
        self._last_raw = value
        dx_hat = self._dx.apply(dx, self._alpha(self.d_cutoff, dt))

        cutoff = self.min_cutoff + self.beta * abs(dx_hat)
        return self._x.apply(value, self._alpha(cutoff, dt))

    def reset(self) -> None:
        """Forget all history (use after tracking loss)."""
        self._x.reset()
        self._dx.reset()
        self._last_t = None
        self._last_raw = None


class OneEuroFilter2D:
    """Pair of One Euro filters for an (x, y) point."""

    def __init__(self, min_cutoff: float = 1.0, beta: float = 0.007) -> None:
        self._fx = OneEuroFilter(min_cutoff, beta)
        self._fy = OneEuroFilter(min_cutoff, beta)

    def apply(self, x: float, y: float, timestamp: float) -> tuple[float, float]:
        """Filter one 2D sample taken at `timestamp` (seconds)."""
        return self._fx.apply(x, timestamp), self._fy.apply(y, timestamp)

    def set_params(self, min_cutoff: float, beta: float) -> None:
        """Retune both axes (e.g. after a sensitivity preset change)."""
        for f in (self._fx, self._fy):
            f.min_cutoff = min_cutoff
            f.beta = beta

    def reset(self) -> None:
        """Forget all history (use after tracking loss)."""
        self._fx.reset()
        self._fy.reset()
