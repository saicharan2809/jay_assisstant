"""Gesture Manager for Jay Assistant.

Modular integration of AirTouch webcam-based hand-gesture control.
Runs headless in a background daemon thread, with dynamic enable/disable,
safe error handling (camera unplugged, missing permissions), and coordination
with Jay's automated mouse actions to prevent control conflicts.
"""

from __future__ import annotations

import logging
import threading
import time
import contextlib
from typing import Any

log = logging.getLogger(__name__)

# Lazy import flag
_AIRTOUCH_AVAILABLE = True
try:
    from airtouch import config as cfgmod
    from airtouch.camera import Camera, CameraError
    from airtouch.controller import MouseController
    from airtouch.gestures import GestureEngine, State
    from airtouch.motion import MotionFrame, create_backend
    from airtouch.tracker import HandTracker, TrackerError
except Exception as e:
    _AIRTOUCH_AVAILABLE = False
    log.warning(f"AirTouch module import warning: {e}")


class GestureManager:
    """Manages the lifecycle and state of the AirTouch hand-gesture controller."""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True

        self.cfg: dict[str, Any] = {}
        self.mouse: MouseController | None = None
        self.engine: GestureEngine | None = None
        self.motion: Any = None
        self.camera: Camera | None = None
        self.tracker: HandTracker | None = None

        self._worker: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._init_error: str | None = None
        self._ready_event = threading.Event()
        self._action_pause_lock = threading.Lock()
        self._paused_for_action = False

    def is_available(self) -> bool:
        """Returns True if AirTouch dependencies and models are loaded."""
        return _AIRTOUCH_AVAILABLE

    def is_enabled(self) -> bool:
        """Returns True if gesture tracking thread is actively running."""
        return self._worker is not None and self._worker.is_alive()

    def is_armed(self) -> bool:
        """Returns True if gestures are actively moving the cursor and clicking."""
        return self.engine is not None and getattr(self.engine, "armed", False)

    def enable(self, camera_index: int | None = None) -> tuple[bool, str]:
        """
        Starts webcam capture and hand-tracking in a background daemon thread.
        Returns (success: bool, message: str). Gracefully handles missing webcams.
        """
        if not self.is_available():
            return False, "AirTouch gesture tracking is not available (missing dependencies or model)."

        with self._lock:
            if self.is_enabled():
                return True, "Gesture control is already active."

            self._stop_event.clear()
            self._ready_event.clear()
            self._init_error = None

            try:
                self.cfg = cfgmod.load()
                if camera_index is not None:
                    self.cfg["camera_index"] = int(camera_index)
                # Ensure headless calibration is set so no popup blocks
                self.cfg["calibrated"] = True

                self.mouse = MouseController()
                self.engine = GestureEngine(self.cfg, self.mouse)
                self.motion = create_backend(self.cfg)
            except Exception as e:
                err_msg = f"Failed to initialize AirTouch configuration: {e}"
                log.error(err_msg)
                return False, err_msg

            self._worker = threading.Thread(target=self._run_tracking_loop, name="JayGestureWorker", daemon=True)
            self._worker.start()

        # Wait up to 3.0s for the camera and tracker to initialize
        ready = self._ready_event.wait(timeout=10.0)
        if not ready or self._init_error:
            err = self._init_error or "Webcam initialization timed out."
            self.disable()
            return False, err

        return True, "Gesture control enabled. Hold an open palm for 1 second to arm or disarm."

    def disable(self) -> tuple[bool, str]:
        """
        Safely stops the tracking loop, releases mouse buttons, and closes the camera.
        Returns (success: bool, message: str).
        """
        with self._lock:
            if not self.is_enabled():
                return True, "Gesture control is already disabled."

            self._stop_event.set()
            if self.engine:
                try:
                    self.engine.shutdown()
                except Exception:
                    pass

        # Wait briefly for worker thread to release resources
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=2.0)

        self._worker = None
        self.camera = None
        self.tracker = None
        return True, "Gesture control disabled and camera released."

    def arm(self) -> bool:
        """Manually arm gesture control (start controlling mouse)."""
        if self.engine and self.is_enabled():
            self.engine.set_armed(True)
            return True
        return False

    def disarm(self) -> bool:
        """Manually disarm gesture control (stop controlling mouse, keep camera ready)."""
        if self.engine and self.is_enabled():
            self.engine.set_armed(False)
            return True
        return False

    def pause(self) -> None:
        """Temporarily pauses gesture mouse injection."""
        if self.engine:
            self.engine.set_enabled(False)

    def resume(self) -> None:
        """Resumes gesture mouse injection."""
        if self.engine:
            self.engine.set_enabled(True)

    @contextlib.contextmanager
    def pause_for_action(self):
        """
        Context manager to pause gesture mouse injection during automated Jay actions
        (e.g. clicking or typing) so user hand movements do not fight automated control.
        """
        with self._action_pause_lock:
            active = self.is_armed()
            if active:
                self.pause()
            try:
                yield
            finally:
                if active and not self._stop_event.is_set():
                    self.resume()

    def get_status(self) -> dict[str, Any]:
        """Returns structured status dictionary."""
        enabled = self.is_enabled()
        armed = self.is_armed()
        state_val = self.engine.state.value if self.engine else "off"
        return {
            "available": self.is_available(),
            "enabled": enabled,
            "armed": armed,
            "state": state_val,
            "camera_index": self.cfg.get("camera_index", 0) if self.cfg else 0,
            "backend": self.cfg.get("motion_backend", "kalman") if self.cfg else "kalman",
            "sensitivity": self.cfg.get("sensitivity", "medium") if self.cfg else "medium"
        }

    # ------------------------------------------------------------------ #
    # Internal Tracking Loop
    # ------------------------------------------------------------------ #

    def _run_tracking_loop(self) -> None:
        """Isolated background thread loop: Camera -> MediaPipe -> MIL -> FSM -> Mouse."""
        cam = None
        trk = None
        try:
            cam = Camera(self.cfg["camera_index"], self.cfg["frame_width"],
                         self.cfg["frame_height"], self.cfg["target_fps"])
            cam.open()
            self.camera = cam

            trk = HandTracker(self.cfg["min_detection_confidence"],
                              self.cfg["min_tracking_confidence"],
                              self.cfg["hand"])
            self.tracker = trk

            # Signal ready to the caller
            self._ready_event.set()
            log.info("Jay AirTouch gesture worker running")

            while not self._stop_event.is_set():
                idle = self.engine.state in (State.DISARMED, State.IDLE)
                fps = self.cfg["target_fps"] / (2 if idle else 1)
                frame = cam.read(throttle_fps=fps)
                now = time.monotonic()

                if frame is None:
                    if self.engine:
                        self.engine.update(self._run_mil(None, now))
                    continue

                hand = trk.process(frame)
                stabilized = self._run_mil(hand, now)
                if self.engine:
                    self.engine.update(stabilized)

        except (CameraError, TrackerError) as exc:
            log.error(f"[Gesture Error] {exc}")
            self._init_error = str(exc)
            self._ready_event.set()
        except Exception as exc:
            log.exception(f"[Gesture Fatal] Unexpected tracking error: {exc}")
            self._init_error = f"Tracking error: {exc}"
            self._ready_event.set()
        finally:
            if self.engine:
                try:
                    self.engine.shutdown()
                except Exception:
                    pass
            if trk:
                try:
                    trk.close()
                except Exception:
                    pass
            if cam:
                try:
                    cam.close()
                except Exception:
                    pass
            log.info("Jay AirTouch gesture worker exited cleanly")

    def _run_mil(self, hand, now: float):
        """Passes hand features through the Motion Intelligence Layer (Kalman/OneEuro)."""
        if not self.motion:
            return hand
        try:
            out = self.motion.process(hand, now)
            return out.hand if out is not None else None
        except Exception:
            return hand


# Global singleton instance
gesture_manager = GestureManager()
