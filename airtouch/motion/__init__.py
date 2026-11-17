"""Motion Intelligence Layer (MIL).

Sits between MediaPipe landmark extraction and the gesture FSM. Its only
job is to improve the motion signal: stabilize positions, compensate
latency by short-horizon prediction, and coast through brief tracking
dropouts. It NEVER injects OS events and NEVER decides actions — the
deterministic FSM remains the sole authority for clicks, drags, scrolls,
arming, and all safety behavior.

Backends implement `MotionBackend` and are interchangeable at runtime
(`config.json: motion_backend`, or the tray's Motion backend menu):

- "oneeuro" — the v1 One Euro filter, kept for A/B comparison
- "kalman"  — Phase 1 adaptive Kalman predictor (default)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..tracker import HandFrame


@dataclass
class MotionFrame:
    """One MIL output frame consumed by the gesture FSM."""

    hand: HandFrame     # stabilized / predicted hand features
    confidence: float   # 0..1 trajectory quality (decays while coasting)
    coasting: bool      # True when synthesized from prediction, no real frame


class MotionBackend(ABC):
    """Interface every motion processor implements."""

    name: str = "base"

    @abstractmethod
    def process(self, hand: HandFrame | None, now: float) -> MotionFrame | None:
        """Consume one raw frame (or None on tracking loss).

        Returns a stabilized MotionFrame, a coasting MotionFrame during a
        short dropout, or None once the loss exceeds the coasting limit —
        at which point the FSM performs its normal safe release.
        """

    @abstractmethod
    def reset(self) -> None:
        """Drop all history (backend switch, recalibration)."""


def create_backend(cfg: dict) -> MotionBackend:
    """Instantiate the backend selected in config."""
    kind = cfg.get("motion_backend", "kalman")
    if kind == "oneeuro":
        from .oneeuro_backend import OneEuroBackend

        return OneEuroBackend(cfg)
    from .kalman_core import KalmanBackend

    return KalmanBackend(cfg)
