"""Phase 1 deterministic predictive core: adaptive Kalman + coasting.

One adaptive rule does the heavy lifting: process noise scales with the
measured hand speed. Hovering (speed ~0) collapses the filter's process
noise so tremor is crushed; fast intentional motion opens it up so the
filter tracks tightly with no lag. On top of that:

- outputs are extrapolated `prediction_horizon_ms` ahead to compensate
  camera + MediaPipe + injection latency, with the prediction offset
  clamped so direction reversals can never overshoot noticeably;
- if the hand drops out for <= `coast_ms`, the filter coasts on decaying
  velocity with decaying confidence; beyond that it reports loss and the
  FSM performs its normal safe release;
- pinch distances get their own light low-pass, fast enough that a quick
  250 ms tap-pinch is never smeared below threshold.
"""

from __future__ import annotations

import logging
import math
from dataclasses import replace

import numpy as np

from . import MotionBackend, MotionFrame
from .feature_encoder import encode
from .temporal_buffer import TemporalBuffer
from ..tracker import HandFrame

log = logging.getLogger(__name__)


class AdaptiveKalman2D:
    """Constant-velocity Kalman filter for one 2D point, adaptive Q.

    State: [x, y, vx, vy]. Process noise (white acceleration) is scaled
    every step by the caller-supplied speed hint.
    """

    def __init__(self, q_min: float, q_max: float, r: float) -> None:
        self.q_min = q_min
        self.q_max = q_max
        self.r = r
        self._x: np.ndarray | None = None  # state (4,)
        self._p = np.eye(4)                # covariance

    # Speeds (frame-fractions/second) between which Q interpolates.
    # _SPEED_LO sits above the tremor noise floor of the windowed speed
    # estimate (~0.03/s), so hovering can never open the filter up.
    _SPEED_LO = 0.05   # hovering / aiming
    _SPEED_HI = 0.60   # deliberate fast motion

    def _q_scale(self, speed: float) -> float:
        a = (speed - self._SPEED_LO) / (self._SPEED_HI - self._SPEED_LO)
        a = min(max(a, 0.0), 1.0)
        # Log-space interpolation: q spans orders of magnitude, and linear
        # blending would let tiny speed-estimate spikes blow the filter
        # open — geometrically it stays near q_min until speed is real.
        return self.q_min * (self.q_max / self.q_min) ** a

    def step(self, zx: float, zy: float, dt: float,
             speed: float) -> tuple[float, float, float, float]:
        """Predict + update with measurement (zx, zy); returns the state."""
        if self._x is None:
            self._x = np.array([zx, zy, 0.0, 0.0])
            self._p = np.diag([self.r, self.r, 1.0, 1.0])
            return zx, zy, 0.0, 0.0

        f = np.eye(4)
        f[0, 2] = f[1, 3] = dt
        q = self._q_scale(speed)
        # Discrete white-acceleration noise model.
        dt2, dt3 = dt * dt, dt * dt * dt
        qm = q * np.array([
            [dt3 / 3, 0, dt2 / 2, 0],
            [0, dt3 / 3, 0, dt2 / 2],
            [dt2 / 2, 0, dt, 0],
            [0, dt2 / 2, 0, dt],
        ])

        x = f @ self._x
        p = f @ self._p @ f.T + qm

        h = np.zeros((2, 4))
        h[0, 0] = h[1, 1] = 1.0
        s = h @ p @ h.T + self.r * np.eye(2)
        k = p @ h.T @ np.linalg.inv(s)
        innov = np.array([zx, zy]) - h @ x
        self._x = x + k @ innov
        self._p = (np.eye(4) - k @ h) @ p
        return tuple(self._x)  # type: ignore[return-value]

    def coast(self, dt: float, decay_tau: float) -> tuple[float, float]:
        """Advance on decaying velocity only (no measurement)."""
        if self._x is None:
            return 0.0, 0.0
        decay = math.exp(-dt / decay_tau)
        self._x[0] += self._x[2] * dt
        self._x[1] += self._x[3] * dt
        self._x[2] *= decay
        self._x[3] *= decay
        return float(self._x[0]), float(self._x[1])

    # Filtered speeds between which the prediction ramps from 0 to full:
    # hovering gets NO extrapolation (it would only re-amplify tremor),
    # deliberate motion gets the full latency compensation.
    _PRED_LO = 0.06
    _PRED_HI = 0.25

    def predict_ahead(self, horizon: float,
                      max_offset: float) -> tuple[float, float]:
        """Position extrapolated `horizon` s ahead, speed-gated and clamped."""
        if self._x is None:
            return 0.0, 0.0
        speed = math.hypot(self._x[2], self._x[3])
        ramp = (speed - self._PRED_LO) / (self._PRED_HI - self._PRED_LO)
        ramp = min(max(ramp, 0.0), 1.0)
        if ramp == 0.0:
            return float(self._x[0]), float(self._x[1])
        ox, oy = self._x[2] * horizon * ramp, self._x[3] * horizon * ramp
        mag = math.hypot(ox, oy)
        if mag > max_offset:
            scale = max_offset / mag
            ox, oy = ox * scale, oy * scale
        return float(self._x[0] + ox), float(self._x[1] + oy)

    def reset(self) -> None:
        """Forget state (tracking loss beyond coasting)."""
        self._x = None


class _FastLowPass:
    """First-order low-pass for pinch distances (~10 Hz cutoff): stable
    thresholds without smearing a 250 ms tap-pinch."""

    def __init__(self, cutoff_hz: float = 10.0) -> None:
        self._tau = 1.0 / (2.0 * math.pi * cutoff_hz)
        self._y: float | None = None

    def apply(self, value: float, dt: float) -> float:
        if self._y is None:
            self._y = value
        else:
            alpha = dt / (dt + self._tau)
            self._y += alpha * (value - self._y)
        return self._y

    def reset(self) -> None:
        self._y = None


class KalmanBackend(MotionBackend):
    """Phase 1 MIL backend: adaptive Kalman on index tip + palm center."""

    name = "kalman"

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        q_min = cfg.get("kalman_q_min", 0.002)
        q_max = cfg.get("kalman_q_max", 400.0)
        r = cfg.get("kalman_r", 2.0e-5)
        self._index = AdaptiveKalman2D(q_min, q_max, r)
        # The palm drives scroll displacement and arm-stillness, not the
        # cursor. It must stay RESPONSIVE: a held scroll offset reads as
        # zero-speed "hovering", which would collapse a low q_min and make
        # the palm lag far behind the hand — starving scroll of its signal.
        # A high floor keeps it tracking with minimal lag (jitter there is
        # absorbed by the scroll dead zone).
        palm_q_min = cfg.get("kalman_palm_q_min", 15.0)
        self._palm = AdaptiveKalman2D(palm_q_min, q_max, r)
        self._pinches = {k: _FastLowPass() for k in
                         ("index", "middle", "vsign")}
        self._buffer = TemporalBuffer(60)
        self._last_t: float | None = None
        self._last_real_t: float | None = None
        self._last_hand: HandFrame | None = None
        # EMA'd speed hints (tau ~150 ms) so single-frame estimate spikes
        # can't yank the adaptive process noise around.
        self._speed_ema = {"index": 0.0, "palm": 0.0}

    def process(self, hand: HandFrame | None, now: float) -> MotionFrame | None:
        dt = 1 / 30 if self._last_t is None else max(now - self._last_t, 1e-4)
        self._last_t = now
        if hand is None:
            return self._coast(now, dt)

        self._buffer.push(hand, now)
        self._last_real_t = now
        self._last_hand = hand
        feats = encode(self._buffer)
        alpha = dt / (dt + 0.15)
        for key, speed in (("index", feats.index_speed),
                           ("palm", feats.palm_speed)):
            self._speed_ema[key] += alpha * (speed - self._speed_ema[key])

        ix, iy, *_ = self._index.step(*hand.index_tip, dt,
                                      self._speed_ema["index"])
        px, py, *_ = self._palm.step(*hand.palm_center, dt,
                                     self._speed_ema["palm"])

        horizon = self.cfg.get("prediction_horizon_ms", 50) / 1000.0
        max_off = self.cfg.get("prediction_max_offset", 0.008)
        ix, iy = self._index.predict_ahead(horizon, max_off)
        px, py = self._palm.predict_ahead(horizon, max_off)

        smoothed = replace(
            hand,
            index_tip=(ix, iy),
            palm_center=(px, py),
            pinch_index=self._pinches["index"].apply(hand.pinch_index, dt),
            pinch_middle=self._pinches["middle"].apply(hand.pinch_middle, dt),
            pinch_vsign=self._pinches["vsign"].apply(hand.pinch_vsign, dt),
        )
        return MotionFrame(hand=smoothed, confidence=hand.confidence,
                           coasting=False)

    def _coast(self, now: float, dt: float) -> MotionFrame | None:
        """Bridge a short dropout with decaying predicted motion."""
        coast_limit = self.cfg.get("coast_ms", 150) / 1000.0
        if self._last_real_t is None or self._last_hand is None:
            return None
        elapsed = now - self._last_real_t
        if elapsed > coast_limit:
            self.reset()
            return None

        ix, iy = self._index.coast(dt, decay_tau=0.08)
        px, py = self._palm.coast(dt, decay_tau=0.08)
        confidence = self._last_hand.confidence * math.exp(-elapsed / coast_limit)
        coasted = replace(self._last_hand, index_tip=(ix, iy),
                          palm_center=(px, py), confidence=confidence)
        return MotionFrame(hand=coasted, confidence=confidence, coasting=True)

    def reset(self) -> None:
        """Drop all filter state and history."""
        self._index.reset()
        self._palm.reset()
        for lp in self._pinches.values():
            lp.reset()
        self._buffer.clear()
        self._last_real_t = None
        self._last_hand = None
        self._speed_ema = {"index": 0.0, "palm": 0.0}
