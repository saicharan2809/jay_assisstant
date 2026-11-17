"""Webcam capture: open, throttle, and read frames — never display them.

OpenCV is used strictly for capture. Any failure raises `CameraError`
with a human-readable message that the caller surfaces via the tray.
"""

from __future__ import annotations

import logging
import time

import cv2
import numpy as np

log = logging.getLogger(__name__)


class CameraError(RuntimeError):
    """Camera could not be opened or stopped delivering frames."""


class Camera:
    """A throttled webcam reader around ``cv2.VideoCapture``."""

    def __init__(self, index: int = 0, width: int = 640, height: int = 480,
                 target_fps: int = 30) -> None:
        self.index = index
        self.width = width
        self.height = height
        self.target_fps = max(10, min(60, target_fps))
        self._cap: cv2.VideoCapture | None = None
        self._last_read = 0.0
        self._consecutive_failures = 0

    def open(self) -> None:
        """Open the device or raise `CameraError` with a clear message."""
        # CAP_DSHOW opens far faster than MSMF on most Windows machines.
        cap = cv2.VideoCapture(self.index, cv2.CAP_DSHOW)
        if not cap.isOpened():
            cap.release()
            cap = cv2.VideoCapture(self.index)  # fall back to default API
        if not cap.isOpened():
            cap.release()
            raise CameraError(
                f"Camera {self.index} not found or in use by another app. "
                "Close other camera apps (Zoom/Teams) and check Windows "
                "camera privacy settings."
            )
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        cap.set(cv2.CAP_PROP_FPS, self.target_fps)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # always read the newest frame
        ok, _ = cap.read()
        if not ok:
            cap.release()
            raise CameraError(
                f"Camera {self.index} opened but delivered no frames. "
                "It may be in use by another application."
            )
        self._cap = cap
        self._consecutive_failures = 0
        log.info("Camera %d open at %dx%d", self.index, self.width, self.height)

    def read(self, throttle_fps: float | None = None) -> np.ndarray | None:
        """Return the next BGR frame, or None on a transient failure.

        Sleeps as needed to cap the effective rate at `throttle_fps`
        (defaults to `target_fps`). Raises `CameraError` after ~2 s of
        continuous failures (camera unplugged / grabbed by another app).
        """
        if self._cap is None:
            raise CameraError("Camera is not open")

        fps = throttle_fps or self.target_fps
        min_interval = 1.0 / fps
        elapsed = time.monotonic() - self._last_read
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)
        self._last_read = time.monotonic()

        ok, frame = self._cap.read()
        if not ok or frame is None:
            self._consecutive_failures += 1
            if self._consecutive_failures > 2 * self.target_fps:
                raise CameraError(
                    "Camera stopped delivering frames (unplugged or taken "
                    "over by another application)."
                )
            return None
        self._consecutive_failures = 0
        return frame

    def close(self) -> None:
        """Release the device. Safe to call repeatedly."""
        if self._cap is not None:
            self._cap.release()
            self._cap = None
            log.info("Camera %d released", self.index)

    def __enter__(self) -> "Camera":
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
