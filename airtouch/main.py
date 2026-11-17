"""AirTouch entry point: tray on the main thread, tracking on a worker.

Run with ``python -m airtouch.main`` (or the built AirTouch.exe).

Flags:
  --debug        show the camera feed with landmark overlay (dev only)
  --recalibrate  force the calibration flow before starting
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys
import threading
import time

from collections import deque

from . import __app_name__, __version__, config as cfgmod
from .camera import Camera, CameraError
from .controller import MouseController
from .gestures import GestureEngine, State
from .motion import MotionFrame, create_backend
from .tracker import HandTracker, TrackerError

log = logging.getLogger(__name__)


class StageTimers:
    """Rolling per-stage latency averages, logged once per minute."""

    def __init__(self, stages: tuple[str, ...], log_every: float = 60.0) -> None:
        self._samples = {s: deque(maxlen=300) for s in stages}
        self._log_every = log_every
        self._last_log = time.monotonic()

    def add(self, stage: str, seconds: float) -> None:
        """Record one duration for a stage."""
        self._samples[stage].append(seconds * 1000.0)

    def averages(self) -> dict[str, float]:
        """Rolling average per stage in milliseconds."""
        return {s: (sum(v) / len(v) if v else 0.0)
                for s, v in self._samples.items()}

    def maybe_log(self) -> None:
        """Emit one INFO line per minute with the averages."""
        now = time.monotonic()
        if now - self._last_log < self._log_every:
            return
        self._last_log = now
        avg = self.averages()
        total = sum(avg.values())
        log.info("stage timings (ms): %s | total %.1f",
                 ", ".join(f"{s}={v:.1f}" for s, v in avg.items()), total)


def setup_logging(debug: bool) -> None:
    """Rotating file log (1 MB) plus console when a console exists."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    file_handler = logging.handlers.RotatingFileHandler(
        cfgmod.app_dir() / "airtouch.log", maxBytes=1_000_000,
        backupCount=2, encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
    if sys.stderr is not None:  # absent in a windowed (no-console) build
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        root.addHandler(console)


class AirTouchApp:
    """Wires camera, tracker, gesture engine, and tray together."""

    def __init__(self, debug: bool = False) -> None:
        self.debug = debug
        self.cfg = cfgmod.load()
        self.mouse = MouseController()
        self.engine = GestureEngine(self.cfg, self.mouse,
                                    on_arm_change=self._on_arm_change)
        self.motion = create_backend(self.cfg)
        self.timers = StageTimers(("mediapipe", "mil", "fsm"))
        self.tray = None  # set in run()
        self._stop = threading.Event()
        self._pause_timer: threading.Timer | None = None
        self._recalibrate_requested = threading.Event()
        self._worker: threading.Thread | None = None

    # ------------------------------------------------------------------ #
    # Worker thread: camera + tracking loop
    # ------------------------------------------------------------------ #

    def _tracking_loop(self) -> None:
        """Camera capture -> tracker -> gesture engine, until stopped."""
        try:
            self._run_camera_session()
        except CameraError as exc:
            log.error("Camera error: %s", exc)
            self._fatal(str(exc))
        except TrackerError as exc:
            log.error("Tracker error: %s", exc)
            self._fatal(str(exc))
        except Exception:  # noqa: BLE001 - last-resort: never die silently
            log.exception("Tracking loop crashed")
            self._fatal("Tracking stopped unexpectedly — see airtouch.log")
        finally:
            self.engine.shutdown()  # zero stuck buttons, whatever happened

    def _run_camera_session(self) -> None:
        camera = Camera(self.cfg["camera_index"], self.cfg["frame_width"],
                        self.cfg["frame_height"], self.cfg["target_fps"])
        camera.open()
        tracker = HandTracker(self.cfg["min_detection_confidence"],
                              self.cfg["min_tracking_confidence"],
                              self.cfg["hand"])
        log.info("Tracking loop started")
        try:
            while not self._stop.is_set():
                if self._recalibrate_requested.is_set():
                    self._do_recalibration(camera, tracker)
                    # Camera/tracker were rebuilt by the helper.
                    camera, tracker = self._camera_after_recal
                # Idle throttle: half rate while disarmed saves CPU.
                idle = self.engine.state in (State.DISARMED, State.IDLE)
                fps = self.cfg["target_fps"] / (2 if idle else 1)
                frame = camera.read(throttle_fps=fps)
                now = time.monotonic()
                if frame is None:
                    self.engine.update(self._run_mil(None, now))
                    continue
                t0 = time.perf_counter()
                hand = tracker.process(frame)
                t1 = time.perf_counter()
                stabilized = self._run_mil(hand, now)
                t2 = time.perf_counter()
                self.engine.update(stabilized)
                t3 = time.perf_counter()
                self.timers.add("mediapipe", t1 - t0)
                self.timers.add("mil", t2 - t1)
                self.timers.add("fsm", t3 - t2)
                self.timers.maybe_log()
                if self.debug:
                    self._show_debug(frame, hand)
        finally:
            tracker.close()
            camera.close()
            if self.debug:
                import cv2
                cv2.destroyAllWindows()
        log.info("Tracking loop stopped")

    def _run_mil(self, hand, now: float):
        """Run the motion backend, never letting it break the pipeline.

        A MIL exception falls back to the raw landmarks (and is logged),
        so a filter bug can never crash the app or strand a held button —
        the FSM still sees every frame and keeps its safety behavior.
        """
        try:
            out = self.motion.process(hand, now)
        except Exception:  # noqa: BLE001 - MIL must never take down the app
            log.exception("MIL backend %r failed; using raw landmarks",
                          self.motion.name)
            out = MotionFrame(hand, hand.confidence, False) if hand else None
        return out.hand if out is not None else None

    def _do_recalibration(self, camera: Camera,
                          tracker: HandTracker) -> None:
        """Release the camera, run calibration, then rebuild both."""
        from .calibrate import run_calibration

        self._recalibrate_requested.clear()
        self.engine.set_armed(False)
        tracker.close()
        camera.close()
        try:
            updated = run_calibration(dict(self.cfg))
            if updated is not None:
                self.cfg = updated
                self.engine.apply_config(self.cfg)
                self.motion = create_backend(self.cfg)
        except (CameraError, TrackerError) as exc:
            log.error("Recalibration failed: %s", exc)
            if self.tray:
                self.tray.notify(f"Recalibration failed: {exc}")
        new_cam = Camera(self.cfg["camera_index"], self.cfg["frame_width"],
                         self.cfg["frame_height"], self.cfg["target_fps"])
        new_cam.open()
        new_tracker = HandTracker(self.cfg["min_detection_confidence"],
                                  self.cfg["min_tracking_confidence"],
                                  self.cfg["hand"])
        self._camera_after_recal = (new_cam, new_tracker)

    def _show_debug(self, frame, hand) -> None:
        """Dev-only: landmark overlay window (never in normal mode)."""
        import cv2

        vis = cv2.flip(frame, 1)
        if hand is not None:
            h, w = vis.shape[:2]
            for x, y, _z in hand.landmarks:
                cv2.circle(vis, (int(x * w), int(y * h)), 3, (0, 255, 0), -1)
            cv2.putText(vis, f"{self.engine.state.value}"
                        f"{' ARMED' if self.engine.armed else ''}",
                        (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8,
                        (0, 255, 255), 2)
        avg = self.timers.averages()
        cv2.putText(vis, f"[{self.motion.name}] " +
                    " ".join(f"{s}:{v:.1f}ms" for s, v in avg.items()),
                    (10, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                    (255, 255, 0), 1)
        cv2.imshow("AirTouch debug", vis)
        cv2.waitKey(1)

    # ------------------------------------------------------------------ #
    # Tray callbacks (run on the tray thread)
    # ------------------------------------------------------------------ #

    def _on_arm_change(self, armed: bool) -> None:
        if self.cfg["beep_on_toggle"]:
            try:
                import winsound
                threading.Thread(
                    target=winsound.Beep,
                    args=(880 if armed else 440, 120), daemon=True).start()
            except Exception:  # noqa: BLE001
                pass
        if self.tray:
            self.tray.set_armed(armed)

    def _toggle_arm(self) -> None:
        self.engine.set_armed(not self.engine.armed)

    def _pause(self, seconds: float) -> None:
        log.info("Paused for %.0f s", seconds)
        self.engine.set_enabled(False)
        if self.tray:
            self.tray.set_paused(True)
            self.tray.notify(f"Paused for {int(seconds / 60)} minutes")
        if self._pause_timer:
            self._pause_timer.cancel()
        self._pause_timer = threading.Timer(seconds, self._resume)
        self._pause_timer.daemon = True
        self._pause_timer.start()

    def _resume(self) -> None:
        log.info("Resumed")
        self.engine.set_enabled(True)
        if self.tray:
            self.tray.set_paused(False)
            self.tray.notify("Resumed")

    def _recalibrate(self) -> None:
        self._recalibrate_requested.set()

    def _set_sensitivity(self, preset: str) -> None:
        log.info("Sensitivity preset: %s", preset)
        self.cfg = cfgmod.apply_sensitivity(self.cfg, preset)
        cfgmod.save(self.cfg)
        self.engine.apply_config(self.cfg)
        self.motion = create_backend(self.cfg)  # re-tune filter params
        if self.tray:
            self.tray.cfg = self.cfg
            self.tray.notify(f"Sensitivity: {preset}")

    def _set_backend(self, kind: str) -> None:
        """Swap the motion backend live (tray Motion backend menu)."""
        log.info("Motion backend: %s", kind)
        self.cfg = dict(self.cfg)
        self.cfg["motion_backend"] = kind
        cfgmod.save(self.cfg)
        self.motion = create_backend(self.cfg)  # worker picks it up next frame
        if self.tray:
            self.tray.cfg = self.cfg
            self.tray.notify(f"Motion backend: {kind}")

    def _quit(self) -> None:
        log.info("Quit requested")
        self._stop.set()
        if self._pause_timer:
            self._pause_timer.cancel()
        self.engine.shutdown()

    # ------------------------------------------------------------------ #
    # Fatal error path
    # ------------------------------------------------------------------ #

    def _fatal(self, message: str) -> None:
        """Surface a fatal worker error and shut the app down cleanly."""
        if self.tray:
            self.tray.notify(message)
            time.sleep(4)  # let the notification be seen
            self.tray.stop_from_thread()
        else:
            print(f"{__app_name__}: {message}", file=sys.stderr or sys.stdout)
        self._stop.set()

    # ------------------------------------------------------------------ #
    # Run
    # ------------------------------------------------------------------ #

    def run(self, force_calibration: bool = False) -> int:
        """Calibrate if needed, start the worker, run the tray (blocking)."""
        from .tray import TrayApp

        if force_calibration or not cfgmod.is_calibrated(self.cfg):
            from .calibrate import run_calibration
            log.info("Running first-run calibration")
            try:
                updated = run_calibration(dict(self.cfg))
            except (CameraError, TrackerError) as exc:
                log.error("Calibration failed: %s", exc)
                _show_error(str(exc))
                return 1
            if updated is None:
                log.info("Calibration cancelled; using defaults")
                self.cfg["calibrated"] = True  # don't nag every launch
                cfgmod.save(self.cfg)
            else:
                self.cfg = updated
            self.engine.apply_config(self.cfg)

        self.tray = TrayApp(
            self.cfg,
            on_toggle_arm=self._toggle_arm,
            on_pause=self._pause,
            on_recalibrate=self._recalibrate,
            on_sensitivity=self._set_sensitivity,
            on_backend=self._set_backend,
            on_quit=self._quit)

        self._worker = threading.Thread(target=self._tracking_loop,
                                        name="tracking", daemon=False)
        self._worker.start()
        log.info("%s %s running — tray active", __app_name__, __version__)
        self.tray.run()  # blocks until Quit / fatal error

        self._stop.set()
        self._worker.join(timeout=5.0)
        self.engine.shutdown()
        log.info("Clean shutdown complete")
        return 0


def _show_error(message: str) -> None:
    """Error dialog that works even without a console."""
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(__app_name__, message)
        root.destroy()
    except Exception:  # noqa: BLE001
        print(f"{__app_name__}: {message}", file=sys.stderr or sys.stdout)


def main() -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="airtouch", description="Touchless finger control for your PC")
    parser.add_argument("--debug", action="store_true",
                        help="show camera feed with landmarks (dev only)")
    parser.add_argument("--recalibrate", action="store_true",
                        help="run the calibration flow again")
    parser.add_argument("--version", action="version",
                        version=f"{__app_name__} {__version__}")
    args = parser.parse_args()

    setup_logging(args.debug)
    log.info("---- %s %s starting ----", __app_name__, __version__)
    app = AirTouchApp(debug=args.debug)
    try:
        return app.run(force_calibration=args.recalibrate)
    except KeyboardInterrupt:
        app._quit()
        return 0


if __name__ == "__main__":
    sys.exit(main())
