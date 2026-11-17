"""The v1 One Euro filter wrapped as a MIL backend (A/B baseline).

Behaves like AirTouch 1.0: One Euro smoothing on index tip and palm
center, raw pinch distances, no prediction, no coasting.
"""

from __future__ import annotations

from dataclasses import replace

from . import MotionBackend, MotionFrame
from ..smoothing import OneEuroFilter2D
from ..tracker import HandFrame


class OneEuroBackend(MotionBackend):
    """Baseline backend: pure One Euro smoothing, no prediction."""

    name = "oneeuro"

    def __init__(self, cfg: dict) -> None:
        self._index = OneEuroFilter2D(cfg["min_cutoff"], cfg["beta"])
        self._palm = OneEuroFilter2D(cfg["min_cutoff"], cfg["beta"])

    def process(self, hand: HandFrame | None, now: float) -> MotionFrame | None:
        if hand is None:
            self.reset()
            return None
        ix, iy = self._index.apply(*hand.index_tip, now)
        px, py = self._palm.apply(*hand.palm_center, now)
        smoothed = replace(hand, index_tip=(ix, iy), palm_center=(px, py))
        return MotionFrame(hand=smoothed, confidence=hand.confidence,
                           coasting=False)

    def reset(self) -> None:
        """Drop filter history."""
        self._index.reset()
        self._palm.reset()
