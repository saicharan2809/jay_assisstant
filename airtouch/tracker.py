"""MediaPipe hand tracking wrapper: frames in, hand features out.

Uses the MediaPipe Tasks `HandLandmarker` in VIDEO running mode with the
bundled ``models/hand_landmarker.task`` model. Produces a `HandFrame`
per processed frame containing the raw 21 landmarks plus everything the
gesture FSM needs already computed: per-finger extension flags and pinch
distances normalized by hand size, so all downstream thresholds are
distance-invariant.
"""

from __future__ import annotations

import logging
import math
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

# MediaPipe Hands landmark indices.
WRIST = 0
THUMB_TIP = 4
INDEX_MCP, INDEX_PIP, INDEX_TIP = 5, 6, 8
MIDDLE_MCP, MIDDLE_PIP, MIDDLE_TIP = 9, 10, 12
RING_MCP, RING_PIP, RING_TIP = 13, 14, 16
PINKY_MCP, PINKY_PIP, PINKY_TIP = 17, 18, 20
THUMB_IP = 3


class TrackerError(RuntimeError):
    """MediaPipe failed to initialize."""


@dataclass
class HandFrame:
    """Everything the gesture engine needs from one processed frame."""

    landmarks: np.ndarray          # (21, 3) normalized image coords + rel z
    handedness: str                # "left" | "right" (of the physical hand)
    hand_size: float               # wrist -> middle MCP distance (3D)
    index_tip: tuple[float, float]  # normalized (x, y) of index fingertip
    fingers_extended: tuple[bool, bool, bool, bool, bool]  # thumb..pinky
    pinch_index: float             # thumb-index distance / hand_size
    pinch_middle: float            # thumb-middle distance / hand_size
    pinch_ring: float              # thumb-ring distance / hand_size
    pinch_vsign: float             # index-middle fingertip distance / hand_size
    palm_center: tuple[float, float]  # normalized centroid of MCPs + wrist
    pinch_point: tuple[float, float]  # midpoint of thumb & index tips (the
    #                                   spot the fingers visually meet in a
    #                                   pinch — the natural pen/ink anchor)
    confidence: float = 1.0        # MediaPipe hand confidence (0..1)

    @property
    def open_palm(self) -> bool:
        """All five fingers extended."""
        return all(self.fingers_extended)

    @property
    def index_only(self) -> bool:
        """Index extended, middle/ring/pinky curled (thumb ignored)."""
        _, idx, mid, ring, pinky = self.fingers_extended
        return idx and not mid and not ring and not pinky

    @property
    def vsign_pose(self) -> bool:
        """Index AND middle extended (the pose for the vsign right-click)."""
        _, idx, mid, _, _ = self.fingers_extended
        return idx and mid


def _dist3(a: np.ndarray, b: np.ndarray) -> float:
    return float(math.dist(a, b))


def _model_path() -> Path:
    """Locate the bundled hand_landmarker.task model (source or frozen)."""
    if getattr(sys, "frozen", False):  # PyInstaller: unpacked to _MEIPASS
        base = Path(getattr(sys, "_MEIPASS", ".")) / "airtouch" / "models"
    else:
        base = Path(__file__).resolve().parent / "models"
    return base / "hand_landmarker.task"


class HandTracker:
    """MediaPipe HandLandmarker in VIDEO mode, tracking a single hand."""

    def __init__(self, min_detection_confidence: float = 0.7,
                 min_tracking_confidence: float = 0.7,
                 preferred_hand: str = "right") -> None:
        try:
            import mediapipe as mp
            from mediapipe.tasks.python import BaseOptions, vision
        except Exception as exc:  # noqa: BLE001 - import may fail in odd envs
            raise TrackerError(f"MediaPipe failed to load: {exc}") from exc

        model = _model_path()
        if not model.exists():
            raise TrackerError(
                f"Hand-tracking model missing: {model}. Reinstall AirTouch."
            )
        try:
            options = vision.HandLandmarkerOptions(
                base_options=BaseOptions(model_asset_path=str(model)),
                running_mode=vision.RunningMode.VIDEO,  # track, don't re-detect
                num_hands=2,                            # see both, then pick one
                min_hand_detection_confidence=min_detection_confidence,
                min_hand_presence_confidence=min_detection_confidence,
                min_tracking_confidence=min_tracking_confidence,
            )
            self._landmarker = vision.HandLandmarker.create_from_options(options)
        except Exception as exc:  # noqa: BLE001
            raise TrackerError(f"MediaPipe HandLandmarker failed to "
                               f"initialize: {exc}") from exc
        self._mp = mp
        self._timestamp_ms = 0
        self.preferred_hand = preferred_hand
        log.info("HandLandmarker ready (preferred hand: %s)", preferred_hand)

    def process(self, frame_bgr: np.ndarray) -> HandFrame | None:
        """Process one BGR frame; return features or None if no hand.

        The frame is mirrored horizontally first so on-screen motion
        matches the user's natural sense of left/right (and so MediaPipe's
        selfie-view handedness labels match the physical hand).
        """
        import cv2

        frame = cv2.flip(frame_bgr, 1)
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        mp_image = self._mp.Image(image_format=self._mp.ImageFormat.SRGB,
                                  data=rgb)
        # VIDEO mode needs strictly increasing timestamps; a synthetic
        # 33 ms/frame clock is immune to wall-clock hiccups.
        self._timestamp_ms += 33
        result = self._landmarker.detect_for_video(mp_image,
                                                   self._timestamp_ms)
        if not result.hand_landmarks:
            return None

        landmarks, label, score = self._pick_hand(result)
        pts = np.array([[p.x, p.y, p.z] for p in landmarks], dtype=np.float64)
        hand_size = _dist3(pts[WRIST], pts[MIDDLE_MCP])
        if hand_size < 1e-6:
            return None

        return HandFrame(
            landmarks=pts,
            handedness=label,
            hand_size=hand_size,
            index_tip=(float(pts[INDEX_TIP][0]), float(pts[INDEX_TIP][1])),
            fingers_extended=self._finger_states(pts),
            pinch_index=_dist3(pts[THUMB_TIP], pts[INDEX_TIP]) / hand_size,
            pinch_middle=_dist3(pts[THUMB_TIP], pts[MIDDLE_TIP]) / hand_size,
            pinch_ring=_dist3(pts[THUMB_TIP], pts[RING_TIP]) / hand_size,
            pinch_vsign=_dist3(pts[INDEX_TIP], pts[MIDDLE_TIP]) / hand_size,
            palm_center=self._palm_center(pts),
            pinch_point=(float((pts[THUMB_TIP][0] + pts[INDEX_TIP][0]) / 2),
                         float((pts[THUMB_TIP][1] + pts[INDEX_TIP][1]) / 2)),
            confidence=score,
        )

    def _pick_hand(self, result: object) -> tuple[list, str, float]:
        """Pick the preferred hand if visible, else the first detected.

        Only ever returns ONE hand; the other is ignored entirely.
        Handedness labels assume a mirrored (selfie) view, which — because
        we flip the frame ourselves — matches the physical hand.
        """
        hands = list(zip(result.hand_landmarks, result.handedness))
        for landmarks, categories in hands:
            label = categories[0].category_name.lower()
            if label == self.preferred_hand:
                return landmarks, label, float(categories[0].score)
        landmarks, categories = hands[0]
        return (landmarks, categories[0].category_name.lower(),
                float(categories[0].score))

    @staticmethod
    def _finger_states(pts: np.ndarray) -> tuple[bool, bool, bool, bool, bool]:
        """Per-finger extended flags, rotation-tolerant.

        A finger counts as extended when its tip is farther from the
        wrist than its PIP joint by a margin — this works regardless of
        hand orientation, unlike naive tip-above-PIP y-comparisons.
        """
        wrist = pts[WRIST]

        def extended(tip: int, pip: int) -> bool:
            return _dist3(pts[tip], wrist) > _dist3(pts[pip], wrist) * 1.08

        # Thumb: tip farther from pinky MCP than the IP joint is (abduction).
        thumb = _dist3(pts[THUMB_TIP], pts[PINKY_MCP]) > \
            _dist3(pts[THUMB_IP], pts[PINKY_MCP]) * 1.05
        return (
            thumb,
            extended(INDEX_TIP, INDEX_PIP),
            extended(MIDDLE_TIP, MIDDLE_PIP),
            extended(RING_TIP, RING_PIP),
            extended(PINKY_TIP, PINKY_PIP),
        )

    @staticmethod
    def _palm_center(pts: np.ndarray) -> tuple[float, float]:
        """Centroid of wrist + four finger MCPs (stable under finger motion)."""
        idx = [WRIST, INDEX_MCP, MIDDLE_MCP, RING_MCP, PINKY_MCP]
        c = pts[idx].mean(axis=0)
        return float(c[0]), float(c[1])

    def close(self) -> None:
        """Free MediaPipe resources."""
        self._landmarker.close()
