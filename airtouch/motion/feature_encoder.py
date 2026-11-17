"""Derived motion features computed from the temporal buffer.

All spatial quantities are normalized by hand size so they are invariant
to distance from the camera. Velocities and accelerations use finite
differences over REAL timestamps — frame intervals are never assumed
fixed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .temporal_buffer import TemporalBuffer


@dataclass
class MotionFeatures:
    """Instantaneous motion descriptors for the tracked hand."""

    index_speed: float        # |velocity| of index tip, frame-fractions/s
    palm_speed: float         # |velocity| of palm center, frame-fractions/s
    index_accel: float        # |acceleration| of index tip, fractions/s^2
    pinch_index_vel: float    # d/dt of thumb-index distance (per second)
    pinch_middle_vel: float   # d/dt of thumb-middle distance (per second)


_ZERO = MotionFeatures(0.0, 0.0, 0.0, 0.0, 0.0)


def _vel(p1: tuple[float, float], p0: tuple[float, float],
         dt: float) -> tuple[float, float]:
    return (p1[0] - p0[0]) / dt, (p1[1] - p0[1]) / dt


def encode(buffer: TemporalBuffer) -> MotionFeatures:
    """Compute motion features from recent buffered frames.

    Speeds are measured over a ~150 ms window (up to 6 frames) rather
    than adjacent frames: adjacent-frame finite differences of noisy
    landmarks are noise-dominated, which would fool the adaptive filter
    into treating tremor as intentional motion.
    """
    frames = buffer.last(6)
    if len(frames) < 2:
        return _ZERO

    first, b1, b2 = frames[0], frames[-2], frames[-1]
    span = max(b2.t - first.t, 1e-4)
    v_index = _vel(b2.hand.index_tip, first.hand.index_tip, span)
    v_palm = _vel(b2.hand.palm_center, first.hand.palm_center, span)
    dt12 = max(b2.t - b1.t, 1e-4)

    accel = 0.0
    if len(frames) >= 4:
        mid = frames[len(frames) // 2]
        dt_a = max(mid.t - first.t, 1e-4)
        dt_b = max(b2.t - mid.t, 1e-4)
        v_a = _vel(mid.hand.index_tip, first.hand.index_tip, dt_a)
        v_b = _vel(b2.hand.index_tip, mid.hand.index_tip, dt_b)
        accel = math.hypot(v_b[0] - v_a[0], v_b[1] - v_a[1]) / max(span / 2, 1e-4)

    return MotionFeatures(
        index_speed=math.hypot(*v_index),
        palm_speed=math.hypot(*v_palm),
        index_accel=accel,
        pinch_index_vel=(b2.hand.pinch_index - b1.hand.pinch_index) / dt12,
        pinch_middle_vel=(b2.hand.pinch_middle - b1.hand.pinch_middle) / dt12,
    )
