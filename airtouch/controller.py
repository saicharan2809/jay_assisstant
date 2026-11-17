"""OS input injection: gesture outputs -> system-wide mouse events.

The `InputBackend` ABC isolates every OS call so that a macOS/Linux
backend can be dropped in later; everything above this module deals only
in normalized screen coordinates and abstract actions.

`MouseController` adds the safety layer: it tracks which buttons *we*
hold down and exposes `release_all()`, which the gesture engine calls on
tracking loss, disarm, and shutdown so a mouse button can never be left
stuck.
"""

from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod

log = logging.getLogger(__name__)


class InputBackend(ABC):
    """Platform-neutral interface for injecting input."""

    @abstractmethod
    def screen_size(self) -> tuple[int, int]:
        """Return (width, height) of the primary screen in pixels."""

    @abstractmethod
    def move_to(self, x: int, y: int) -> None:
        """Move the cursor to absolute pixel coordinates."""

    @abstractmethod
    def press(self, button: str) -> None:
        """Press and hold a mouse button ("left" | "right")."""

    @abstractmethod
    def release(self, button: str) -> None:
        """Release a mouse button ("left" | "right")."""

    @abstractmethod
    def click(self, button: str, count: int = 1) -> None:
        """Click a mouse button `count` times."""

    @abstractmethod
    def scroll(self, dx: int, dy: int) -> None:
        """Scroll by (dx, dy) wheel steps. dy>0 scrolls content up."""


class PynputBackend(InputBackend):
    """Windows/macOS/Linux backend using pynput (system-wide injection)."""

    def __init__(self) -> None:
        from pynput.mouse import Button, Controller as Mouse

        self._mouse = Mouse()
        self._buttons = {"left": Button.left, "right": Button.right}

    def screen_size(self) -> tuple[int, int]:
        import ctypes

        user32 = ctypes.windll.user32
        return int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1))

    def move_to(self, x: int, y: int) -> None:
        self._mouse.position = (x, y)

    def press(self, button: str) -> None:
        self._mouse.press(self._buttons[button])

    def release(self, button: str) -> None:
        self._mouse.release(self._buttons[button])

    def click(self, button: str, count: int = 1) -> None:
        self._mouse.click(self._buttons[button], count)

    def scroll(self, dx: int, dy: int) -> None:
        self._mouse.scroll(dx, dy)


def default_backend() -> InputBackend:
    """Return the backend for the current platform (Windows-only today)."""
    return PynputBackend()


class MouseController:
    """Safety wrapper: tracked button state + normalized-coordinate API."""

    def __init__(self, backend: InputBackend | None = None) -> None:
        self._backend = backend or default_backend()
        self._screen_w, self._screen_h = self._backend.screen_size()
        self._held: set[str] = set()
        self._lock = threading.Lock()
        # Fractional scroll remainder so slow hand motion still scrolls.
        self._scroll_rem_x = 0.0
        self._scroll_rem_y = 0.0

    def move_normalized(self, nx: float, ny: float) -> None:
        """Move cursor to (nx, ny) in [0,1] screen space."""
        x = int(min(max(nx, 0.0), 1.0) * (self._screen_w - 1))
        y = int(min(max(ny, 0.0), 1.0) * (self._screen_h - 1))
        self._backend.move_to(x, y)

    def click(self, button: str = "left", count: int = 1) -> None:
        """Single/double click (no held state to track)."""
        self._backend.click(button, count)
        log.debug("click %s x%d", button, count)

    def press(self, button: str = "left") -> None:
        """Press and hold; remembered for `release_all`."""
        with self._lock:
            if button in self._held:
                return
            self._backend.press(button)
            self._held.add(button)
        log.debug("press %s", button)

    def release(self, button: str = "left") -> None:
        """Release a held button (no-op if we don't hold it)."""
        with self._lock:
            if button not in self._held:
                return
            self._backend.release(button)
            self._held.discard(button)
        log.debug("release %s", button)

    def release_all(self) -> None:
        """Release every button we hold. Safe to call repeatedly."""
        with self._lock:
            for button in list(self._held):
                try:
                    self._backend.release(button)
                except Exception:  # noqa: BLE001 - releasing must never raise
                    log.exception("release_all: failed to release %s", button)
                self._held.discard(button)

    def scroll_amount(self, dx: float, dy: float) -> None:
        """Scroll by fractional wheel steps, accumulating remainders."""
        self._scroll_rem_x += dx
        self._scroll_rem_y += dy
        sx, sy = int(self._scroll_rem_x), int(self._scroll_rem_y)
        if sx or sy:
            self._scroll_rem_x -= sx
            self._scroll_rem_y -= sy
            self._backend.scroll(sx, sy)

    def reset_scroll(self) -> None:
        """Drop accumulated fractional scroll (on scroll-mode exit)."""
        self._scroll_rem_x = 0.0
        self._scroll_rem_y = 0.0

    @property
    def holding(self) -> bool:
        """True while any button is held by us."""
        return bool(self._held)
