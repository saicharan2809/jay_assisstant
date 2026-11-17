"""System tray icon and menu — the entire visible UI of AirTouch.

Gray hand icon = disarmed, green = armed. The menu mirrors the palm
gesture (arm/disarm), offers a 5-minute pause, sensitivity presets,
config/recalibrate shortcuts, a Start-with-Windows toggle, and Quit.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable

import pystray
from PIL import Image, ImageDraw

from . import __app_name__, config as cfgmod

log = logging.getLogger(__name__)

_GRAY = (128, 128, 128, 255)
_GREEN = (46, 174, 82, 255)
_PAUSED = (214, 158, 46, 255)


def _make_icon(color: tuple[int, int, int, int]) -> Image.Image:
    """Draw a simple stylized hand (palm + fingers) in the given color."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([18, 28, 46, 56], radius=8, fill=color)   # palm
    finger_w = 6
    for i, (x, top) in enumerate([(19, 12), (27, 6), (35, 8), (43, 14)]):
        d.rounded_rectangle([x, top, x + finger_w, 34], radius=3, fill=color)
    d.rounded_rectangle([8, 30, 22, 40], radius=4, fill=color)    # thumb
    return img


class TrayApp:
    """Owns the pystray icon; runs on the main thread via `run()`."""

    def __init__(self,
                 cfg: dict[str, Any],
                 on_toggle_arm: Callable[[], None],
                 on_pause: Callable[[float], None],
                 on_recalibrate: Callable[[], None],
                 on_sensitivity: Callable[[str], None],
                 on_backend: Callable[[str], None],
                 on_quit: Callable[[], None]) -> None:
        self.cfg = cfg
        self._on_toggle_arm = on_toggle_arm
        self._on_pause = on_pause
        self._on_recalibrate = on_recalibrate
        self._on_sensitivity = on_sensitivity
        self._on_backend = on_backend
        self._on_quit = on_quit

        self._armed = False
        self._paused = False
        self._icons = {"armed": _make_icon(_GREEN),
                       "disarmed": _make_icon(_GRAY),
                       "paused": _make_icon(_PAUSED)}
        self.icon = pystray.Icon(
            __app_name__, self._icons["disarmed"],
            f"{__app_name__} — Disarmed", menu=self._build_menu())

    # ------------------------------------------------------------------ #
    # State shown in the tray
    # ------------------------------------------------------------------ #

    def set_armed(self, armed: bool) -> None:
        """Reflect arm state: icon color, tooltip, menu, notification."""
        self._armed = armed
        self._refresh()
        self.notify("Armed — gestures active" if armed
                    else "Disarmed — gestures off")

    def set_paused(self, paused: bool) -> None:
        """Reflect pause state in the icon."""
        self._paused = paused
        self._refresh()

    def notify(self, message: str) -> None:
        """Show a brief tray notification (best-effort)."""
        try:
            self.icon.notify(message, __app_name__)
        except Exception:  # noqa: BLE001 - notifications must never crash us
            log.debug("Tray notification failed", exc_info=True)

    def _refresh(self) -> None:
        key = "paused" if self._paused else ("armed" if self._armed
                                             else "disarmed")
        self.icon.icon = self._icons[key]
        status = ("Paused" if self._paused
                  else "Armed" if self._armed else "Disarmed")
        self.icon.title = f"{__app_name__} — {status}"
        self.icon.update_menu()

    # ------------------------------------------------------------------ #
    # Menu
    # ------------------------------------------------------------------ #

    def _build_menu(self) -> pystray.Menu:
        def status_text(_: object) -> str:
            if self._paused:
                return "Status: Paused"
            return f"Status: {'Armed' if self._armed else 'Disarmed'}"

        def sensitivity_item(preset: str) -> pystray.MenuItem:
            return pystray.MenuItem(
                preset.capitalize(),
                lambda: self._pick_sensitivity(preset),
                checked=lambda _i, p=preset: self.cfg["sensitivity"] == p,
                radio=True)

        def backend_item(kind: str, label: str) -> pystray.MenuItem:
            return pystray.MenuItem(
                label,
                lambda: self._pick_backend(kind),
                checked=lambda _i, k=kind: self.cfg["motion_backend"] == k,
                radio=True)

        return pystray.Menu(
            pystray.MenuItem(status_text, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                lambda _: "Disarm" if self._armed else "Arm",
                lambda: self._on_toggle_arm()),
            pystray.MenuItem("Pause 5 minutes",
                             lambda: self._on_pause(300.0)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open config file", self._open_config),
            pystray.MenuItem("Recalibrate",
                             lambda: self._on_recalibrate()),
            pystray.MenuItem("Sensitivity", pystray.Menu(
                sensitivity_item("low"),
                sensitivity_item("medium"),
                sensitivity_item("high"))),
            pystray.MenuItem("Motion backend", pystray.Menu(
                backend_item("kalman", "Kalman (predictive)"),
                backend_item("oneeuro", "One Euro (v1)"))),
            pystray.MenuItem("Start with Windows", self._toggle_startup,
                             checked=lambda _: self._startup_enabled()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._quit),
        )

    def _pick_sensitivity(self, preset: str) -> None:
        self._on_sensitivity(preset)
        self.icon.update_menu()

    def _pick_backend(self, kind: str) -> None:
        self._on_backend(kind)
        self.icon.update_menu()

    def _open_config(self) -> None:
        path = cfgmod.config_path()
        try:
            os.startfile(str(path))  # default .json editor
        except OSError:
            subprocess.Popen(["notepad.exe", str(path)])

    # ------------------------------------------------------------------ #
    # Start with Windows (shortcut in the user's Startup folder)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _startup_dir() -> Path:
        return (Path(os.environ["APPDATA"]) / "Microsoft" / "Windows"
                / "Start Menu" / "Programs" / "Startup")

    @classmethod
    def _startup_path(cls) -> Path:
        return cls._startup_dir() / f"{__app_name__}.bat"

    def _startup_enabled(self) -> bool:
        return self._startup_path().exists()

    def _toggle_startup(self) -> None:
        path = self._startup_path()
        try:
            if path.exists():
                path.unlink()
                self.notify("Removed from Windows startup")
            else:
                if getattr(sys, "frozen", False):
                    cmd = f'start "" "{sys.executable}"'
                else:
                    exe = Path(sys.executable)
                    pyw = exe.with_name("pythonw.exe")
                    runner = pyw if pyw.exists() else exe
                    cmd = (f'cd /d "{cfgmod.app_dir()}"\n'
                           f'start "" "{runner}" -m airtouch.main')
                path.write_text(f"@echo off\n{cmd}\n", encoding="utf-8")
                self.notify("Will start with Windows")
        except OSError as exc:
            log.error("Startup toggle failed: %s", exc)
            self.notify(f"Could not change startup setting: {exc}")
        self.icon.update_menu()

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def _quit(self) -> None:
        self._on_quit()
        self.icon.stop()

    def run(self) -> None:
        """Blocking: run the tray loop on the main thread."""
        self.icon.run()

    def stop_from_thread(self) -> None:
        """Stop the tray from a worker thread (fatal camera error path)."""
        threading.Thread(target=self.icon.stop, daemon=True).start()
