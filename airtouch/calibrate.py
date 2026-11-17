"""First-run calibration: a small tkinter-guided flow.

Measures the user's comfortable hand position/size and personal pinch
distances, then writes the results (control box center, pinch
thresholds, preferred hand) to ``config.json``.

Runs before the tray starts, on the main thread, with its own camera
session — the camera is fully released before the main loop begins.
"""

from __future__ import annotations

import logging
import statistics
import time
import tkinter as tk
from tkinter import messagebox
from typing import Any

from . import config as cfgmod
from .camera import Camera, CameraError
from .tracker import HandTracker, TrackerError

log = logging.getLogger(__name__)


class _CalibrationUI:
    """Minimal tkinter window that shows one instruction at a time."""

    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("AirTouch — Calibration")
        self.root.geometry("520x220")
        self.root.attributes("-topmost", True)
        self.root.resizable(False, False)
        self.label = tk.Label(self.root, text="", font=("Segoe UI", 13),
                              wraplength=480, justify="center")
        self.label.pack(expand=True, padx=20, pady=10)
        self.progress = tk.Label(self.root, text="", font=("Segoe UI", 11),
                                 fg="#0a7d32")
        self.progress.pack(pady=(0, 18))
        self.cancelled = False
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self) -> None:
        self.cancelled = True

    def say(self, text: str, progress: str = "") -> None:
        """Update the instruction text and keep the UI responsive."""
        self.label.config(text=text)
        self.progress.config(text=progress)
        self.root.update()

    def ask_hand(self) -> str | None:
        """Modal left/right hand choice; None if the window was closed."""
        choice: list[str] = []

        def pick(hand: str) -> None:
            choice.append(hand)

        self.say("Welcome to AirTouch!\nWhich hand will you use to control "
                 "the cursor?")
        frame = tk.Frame(self.root)
        frame.pack(pady=(0, 16))
        tk.Button(frame, text="Left hand", width=14, font=("Segoe UI", 11),
                  command=lambda: pick("left")).pack(side="left", padx=10)
        tk.Button(frame, text="Right hand", width=14, font=("Segoe UI", 11),
                  command=lambda: pick("right")).pack(side="left", padx=10)
        while not choice and not self.cancelled:
            self.root.update()
            time.sleep(0.02)
        frame.destroy()
        return choice[0] if choice else None

    def close(self) -> None:
        """Destroy the window (tolerates already-destroyed)."""
        try:
            self.root.destroy()
        except tk.TclError:
            pass


def run_calibration(cfg: dict[str, Any]) -> dict[str, Any] | None:
    """Run the full calibration flow. Returns the updated config, or None
    if the user cancelled. Camera errors raise `CameraError` upward so
    the caller can show a proper message."""
    ui = _CalibrationUI()
    camera: Camera | None = None
    tracker: HandTracker | None = None
    try:
        hand = ui.ask_hand()
        if hand is None:
            return None
        cfg["hand"] = hand

        ui.say("Opening your camera…")
        camera = Camera(cfg["camera_index"], cfg["frame_width"],
                        cfg["frame_height"], cfg["target_fps"])
        camera.open()
        tracker = HandTracker(cfg["min_detection_confidence"],
                              cfg["min_tracking_confidence"], hand)

        # --- Step 1: hand size + comfortable center (3 s of samples) --- #
        centers, sizes = _collect_hand_stats(ui, camera, tracker, seconds=3.0)
        if centers is None or sizes is None:
            return None
        cx = statistics.median(c[0] for c in centers)
        cy = statistics.median(c[1] for c in centers)
        log.info("Calibration: hand size %.3f, center (%.2f, %.2f)",
                 statistics.median(sizes), cx, cy)
        # Keep the box on-frame even if the hand sat near an edge.
        cfg["control_box_center_x"] = min(max(cx, 0.25), 0.75)
        cfg["control_box_center_y"] = min(max(cy, 0.25), 0.75)

        # --- Step 2: three thumb+index pinches for personal thresholds --- #
        pinch_mins = _collect_pinches(ui, camera, tracker, count=3)
        if pinch_mins is None:
            return None
        if pinch_mins:
            typical_pinch = statistics.median(pinch_mins)
            # Engage a bit above the user's tightest pinch; keep the
            # mandated hysteresis gap and sane bounds.
            engage = min(max(typical_pinch * 1.4, 0.15), 0.35)
            cfg["pinch_engage"] = round(engage, 3)
            cfg["pinch_release"] = round(engage + 0.10, 3)

        cfg["calibrated"] = True
        cfgmod.save(cfg)
        ui.say("Calibration complete! AirTouch will now start in your "
               "system tray.\n\nShow an OPEN PALM for 1 second to arm it.")
        ui.root.update()
        time.sleep(2.5)
        return cfg
    finally:
        if tracker is not None:
            tracker.close()
        if camera is not None:
            camera.close()
        ui.close()


def _collect_hand_stats(ui: _CalibrationUI, camera: Camera,
                        tracker: HandTracker, seconds: float,
                        ) -> tuple[list[tuple[float, float]], list[float]] | tuple[None, None]:
    """Sample palm center + hand size for `seconds` of visible-hand time."""
    ui.say("Hold your hand up in a comfortable position in front of the "
           "camera, fingers relaxed.\nKeep it there…")
    centers: list[tuple[float, float]] = []
    sizes: list[float] = []
    sampled = 0.0
    last = time.monotonic()
    deadline = time.monotonic() + 30.0  # give up eventually
    while sampled < seconds:
        if ui.cancelled or time.monotonic() > deadline:
            if not centers:
                _fail(ui, "No hand detected. Check lighting and try again "
                          "from the tray menu (Recalibrate).")
                return None, None
            break
        frame = camera.read()
        now = time.monotonic()
        dt, last = now - last, now
        if frame is None:
            continue
        hf = tracker.process(frame)
        if hf is None:
            ui.say("Hold your hand up in a comfortable position in front "
                   "of the camera.", "Looking for your hand…")
            continue
        centers.append(hf.palm_center)
        sizes.append(hf.hand_size)
        sampled += dt
        ui.say("Perfect — keep your hand there.",
               f"Measuring… {min(sampled / seconds, 1.0):.0%}")
    return centers, sizes


def _collect_pinches(ui: _CalibrationUI, camera: Camera,
                     tracker: HandTracker, count: int) -> list[float] | None:
    """Record the minimum normalized thumb–index distance of `count`
    deliberate pinches. Returns [] on timeout (defaults kept)."""
    mins: list[float] = []
    current_min = 1.0
    pinching = False
    deadline = time.monotonic() + 60.0
    while len(mins) < count:
        if ui.cancelled:
            return None
        if time.monotonic() > deadline:
            ui.say("Skipping pinch tuning (using default thresholds).")
            time.sleep(1.5)
            return []
        ui.say("Now pinch your THUMB and INDEX fingertip together firmly, "
               "then open them again.",
               f"Pinch {len(mins) + 1} of {count}")
        frame = camera.read()
        if frame is None:
            continue
        hf = tracker.process(frame)
        if hf is None:
            continue
        d = hf.pinch_index
        if d < 0.30:  # clearly pinching
            pinching = True
            current_min = min(current_min, d)
        elif pinching and d > 0.45:  # clearly released: pinch complete
            mins.append(current_min)
            current_min = 1.0
            pinching = False
    return mins


def _fail(ui: _CalibrationUI, message: str) -> None:
    """Show a calibration failure to the user."""
    try:
        messagebox.showwarning("AirTouch calibration", message,
                               parent=ui.root)
    except tk.TclError:
        log.warning("Calibration: %s", message)
