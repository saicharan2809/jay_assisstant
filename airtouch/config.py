"""Configuration: load / save / validate ``config.json``.

The config lives next to the executable (frozen build) or in the project
root (source checkout). It is auto-created with defaults on first run and
re-validated on every load, so a hand-edited bad value falls back to its
default instead of crashing the app.
"""

from __future__ import annotations

import copy
import json
import logging
import sys
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULTS: dict[str, Any] = {
    "hand": "right",                 # "right" | "left" — which hand to track
    "camera_index": 0,               # cv2 camera index
    "frame_width": 640,
    "frame_height": 480,
    "target_fps": 30,

    # MediaPipe confidences
    "min_detection_confidence": 0.7,
    "min_tracking_confidence": 0.7,

    # Pinch hysteresis, as fraction of hand size (wrist -> middle MCP)
    "pinch_engage": 0.28,
    "pinch_release": 0.38,

    # Right-click gesture: index + middle fingertips touching. Adjacent
    # extended fingertips sit closer than thumb pinches, so thresholds
    # are tighter than pinch_engage/release.
    "vsign_engage": 0.20,
    "vsign_release": 0.34,

    # Control box: central region of the frame mapped to the full screen.
    # Fractions of frame dimensions; box is centered on box_center.
    "control_box_width": 0.60,
    "control_box_height": 0.60,
    "control_box_center_x": 0.5,
    "control_box_center_y": 0.5,

    # Cursor gain: the control box maps to gain x the screen, centered,
    # so screen edges are reachable without stretching to the box edge.
    "cursor_gain": 1.15,

    # Motion Intelligence Layer
    "motion_backend": "kalman",      # "oneeuro" | "kalman"
    "prediction_horizon_ms": 50,     # extrapolation for latency compensation
    "prediction_max_offset": 0.008,  # clamp on prediction offset (frame frac)
    "kalman_q_min": 0.002,            # index-tip process noise while hovering (tremor kill)
    "kalman_palm_q_min": 15.0,       # palm process noise floor (keep scroll responsive)
    "kalman_q_max": 400.0,           # process noise at speed (lag-free tracking)
    "kalman_r": 2.0e-5,              # measurement noise (landmark jitter var)
    "coast_ms": 150,                 # max dropout bridged by prediction

    # One Euro filter (used by the "oneeuro" backend)
    "min_cutoff": 1.0,
    "beta": 0.01,

    # Click accuracy: on pinch start, move the cursor back to where it was
    # this many ms earlier — the pinching motion itself perturbs the hand,
    # and the FSM's stability frames confirm the pinch ~100 ms after it
    # began, so the rewind must reach back past both.
    "click_rewind_ms": 180,

    # Scrolling
    "scroll_speed": 45.0,            # scroll notches/sec at full deflection
    "scroll_dead_zone": 0.015,       # fraction of frame height
    "scroll_full_offset": 0.12,      # hand offset (frame frac) = full speed
    "natural_scroll": False,         # True = content follows hand
    "scroll_axis_lock": True,        # lock to the first dominant axis
    # Scroll pinch holds through hand motion with a wider release than a
    # click: moving a pinched hand adds landmark noise that a tight
    # release would drop, silently ending the scroll mid-swipe.
    "scroll_release": 0.48,

    # Timings (seconds)
    "click_max_hold": 0.30,          # pinch shorter than this = click
    "click_debounce": 0.15,
    "double_click_window": 0.40,
    "arm_hold_time": 1.0,            # open palm hold to toggle arm
    "arm_grace_period": 0.5,         # ignore gestures right after arming
    "auto_disarm_timeout": 15.0,     # no-hand seconds before auto disarm

    # Anti-accident
    "stability_frames": 3,           # frames a gesture must persist

    # UX
    "beep_on_toggle": True,
    "sensitivity": "medium",         # last preset chosen from the tray

    # Set True by calibrate.py after first-run calibration completes.
    "calibrated": True,
}

VALID_HANDS = ("left", "right")
VALID_SENSITIVITIES = ("low", "medium", "high")
VALID_BACKENDS = ("oneeuro", "kalman")

# Tray sensitivity presets: smaller box + snappier filter = "higher"
# sensitivity (less hand travel, faster response, more jitter allowed).
SENSITIVITY_PRESETS: dict[str, dict[str, float]] = {
    "low": {"control_box_width": 0.75, "control_box_height": 0.75,
            "min_cutoff": 0.6, "beta": 0.004, "scroll_speed": 30.0},
    "medium": {"control_box_width": 0.60, "control_box_height": 0.60,
               "min_cutoff": 1.0, "beta": 0.01, "scroll_speed": 45.0},
    "high": {"control_box_width": 0.45, "control_box_height": 0.45,
             "min_cutoff": 1.6, "beta": 0.02, "scroll_speed": 65.0},
}


def app_dir() -> Path:
    """Directory holding config/log: exe folder when frozen, project root otherwise."""
    if getattr(sys, "frozen", False):  # PyInstaller bundle
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def config_path() -> Path:
    """Full path of ``config.json``."""
    return app_dir() / "airtouch_config.json"


def _validate(cfg: dict[str, Any]) -> dict[str, Any]:
    """Coerce/clamp values; silently fall back to defaults for bad entries."""
    out = copy.deepcopy(DEFAULTS)
    for key, default in DEFAULTS.items():
        if key not in cfg:
            continue
        value = cfg[key]
        if isinstance(default, bool):
            if isinstance(value, bool):
                out[key] = value
        elif isinstance(default, (int, float)):
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                out[key] = type(default)(value)
        elif isinstance(default, str):
            if isinstance(value, str):
                out[key] = value

    if out["hand"] not in VALID_HANDS:
        out["hand"] = DEFAULTS["hand"]
    if out["sensitivity"] not in VALID_SENSITIVITIES:
        out["sensitivity"] = DEFAULTS["sensitivity"]

    if out["motion_backend"] not in VALID_BACKENDS:
        out["motion_backend"] = DEFAULTS["motion_backend"]

    # Hysteresis sanity: engage strictly below release.
    if out["pinch_engage"] >= out["pinch_release"]:
        out["pinch_engage"] = DEFAULTS["pinch_engage"]
        out["pinch_release"] = DEFAULTS["pinch_release"]
    if out["vsign_engage"] >= out["vsign_release"]:
        out["vsign_engage"] = DEFAULTS["vsign_engage"]
        out["vsign_release"] = DEFAULTS["vsign_release"]
    # Scroll must release no tighter than a click, or a moving pinched
    # hand would drop the scroll lock.
    out["scroll_release"] = max(out["pinch_engage"] + 0.05,
                                min(1.2, out["scroll_release"]))
    out["scroll_full_offset"] = max(0.03, min(0.5, out["scroll_full_offset"]))
    out["scroll_dead_zone"] = max(0.0, min(0.1, out["scroll_dead_zone"]))
    out["scroll_speed"] = max(1.0, min(300.0, out["scroll_speed"]))

    out["pinch_engage"] = max(0.05, min(0.9, out["pinch_engage"]))
    out["pinch_release"] = max(out["pinch_engage"] + 0.02, min(1.2, out["pinch_release"]))
    for key in ("control_box_width", "control_box_height"):
        out[key] = max(0.2, min(1.0, out[key]))
    for key in ("control_box_center_x", "control_box_center_y"):
        out[key] = max(0.2, min(0.8, out[key]))
    for key in ("min_detection_confidence", "min_tracking_confidence"):
        out[key] = max(0.1, min(1.0, out[key]))
    out["stability_frames"] = max(1, min(10, int(out["stability_frames"])))
    out["target_fps"] = max(10, min(60, int(out["target_fps"])))
    out["cursor_gain"] = max(1.0, min(1.6, out["cursor_gain"]))
    out["prediction_horizon_ms"] = max(0, min(120, int(out["prediction_horizon_ms"])))
    out["coast_ms"] = max(0, min(400, int(out["coast_ms"])))
    return out


def load() -> dict[str, Any]:
    """Load and validate config; create the file with defaults if missing."""
    path = config_path()
    if not path.exists():
        log.info("No config found; creating defaults at %s", path)
        save(dict(DEFAULTS))
        return dict(DEFAULTS)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("config root is not an object")
    except (OSError, ValueError) as exc:
        log.warning("Config unreadable (%s); using defaults", exc)
        return dict(DEFAULTS)
    return _validate(raw)


def save(cfg: dict[str, Any]) -> None:
    """Write config to disk (pretty-printed, atomic-ish)."""
    path = config_path()
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        log.error("Could not save config: %s", exc)


def apply_sensitivity(cfg: dict[str, Any], preset: str) -> dict[str, Any]:
    """Return a copy of `cfg` with a sensitivity preset applied."""
    out = dict(cfg)
    out.update(SENSITIVITY_PRESETS.get(preset, SENSITIVITY_PRESETS["medium"]))
    out["sensitivity"] = preset
    return out


def is_calibrated(cfg: dict[str, Any]) -> bool:
    """True once first-run calibration has been completed and saved."""
    return bool(cfg.get("calibrated", False))
