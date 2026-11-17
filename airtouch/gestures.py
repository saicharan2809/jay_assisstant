"""Gesture engine: a finite state machine from hand features to intents.

States: DISARMED, IDLE, CURSOR, SCROLL, CLICK_PENDING, DRAG,
RIGHT_CLICK_PENDING. Exactly one state is active at a time.

Safety design (all mandatory, see README):
 - starts DISARMED; open-palm-held-1s toggles armed
 - hysteresis on every pinch (engage 0.28 / release 0.38 of hand size)
 - a gesture must be stable for N frames (default 3) before it triggers
 - thumb+index pinch (scroll) is a modal lock: nothing else can fire
 - click debounce + grace period after arming
 - any tracking loss releases held buttons and returns to IDLE
"""

from __future__ import annotations

import enum
import logging
import time
from typing import Any, Callable

from collections import deque

from .controller import MouseController
from .tracker import HandFrame

log = logging.getLogger(__name__)


class State(enum.Enum):
    """FSM states. Exactly one is active at any moment."""

    DISARMED = "disarmed"
    IDLE = "idle"
    CURSOR = "cursor"
    SCROLL = "scroll"
    CLICK_PENDING = "click_pending"
    DRAG = "drag"
    RIGHT_CLICK_PENDING = "right_click_pending"


class PinchDetector:
    """Hysteresis pinch detector: engages below one threshold, releases
    above a higher one, so a pinch near the boundary can never flicker."""

    def __init__(self, engage: float, release: float) -> None:
        self.engage_threshold = engage
        self.release_threshold = release
        self.engaged = False

    def update(self, normalized_distance: float) -> bool:
        """Feed one distance sample; return current engaged state."""
        if self.engaged:
            if normalized_distance > self.release_threshold:
                self.engaged = False
        else:
            if normalized_distance < self.engage_threshold:
                self.engaged = True
        return self.engaged

    def reset(self) -> None:
        """Force released (after tracking loss)."""
        self.engaged = False


class StabilityCounter:
    """Requires the same candidate for N consecutive frames to confirm."""

    def __init__(self, frames: int) -> None:
        self.required = frames
        self._candidate: Any = None
        self._count = 0

    def update(self, candidate: Any) -> bool:
        """Feed this frame's candidate; True once it has held N frames."""
        if candidate == self._candidate:
            self._count += 1
        else:
            self._candidate = candidate
            self._count = 1
        return self._count >= self.required

    def reset(self) -> None:
        """Clear history."""
        self._candidate = None
        self._count = 0


class GestureEngine:
    """Consumes `HandFrame`s, drives a `MouseController`, owns the FSM."""

    def __init__(self, cfg: dict[str, Any], mouse: MouseController,
                 on_arm_change: Callable[[bool], None] | None = None) -> None:
        self.cfg = cfg
        self.mouse = mouse
        self.on_arm_change = on_arm_change

        self.state = State.DISARMED
        self.armed = False
        self.enabled = True  # tray pause sets this False

        # Scroll uses a WIDER release than clicks so a pinched hand can be
        # moved around to scroll without the pinch flickering off.
        self._pinch_scroll = PinchDetector(cfg["pinch_engage"], cfg["scroll_release"])
        self._pinch_click = PinchDetector(cfg["pinch_engage"], cfg["pinch_release"])
        # Right click: index+middle fingertips touching (tighter thresholds
        # since adjacent extended fingertips naturally sit closer).
        self._pinch_vsign = PinchDetector(cfg["vsign_engage"], cfg["vsign_release"])
        self._stability = StabilityCounter(cfg["stability_frames"])

        # Recent cursor positions (t, nx, ny) for the click rewind: on
        # pinch start the cursor returns to where the user was aiming
        # before the pinching motion perturbed the hand.
        self._cursor_history: deque[tuple[float, float, float]] = deque(maxlen=30)

        # Arm-toggle tracking
        self._palm_anchor: tuple[float, float] | None = None
        self._palm_anchor_time = 0.0
        self._palm_toggled = False  # one toggle per continuous palm hold
        self._armed_at = 0.0

        # Click / drag timing
        self._pinch_started = 0.0
        self._last_click_time = 0.0

        # Scroll origin (palm center at pinch start) and axis lock
        self._scroll_origin: tuple[float, float] | None = None
        self._scroll_axis: str | None = None  # "x" | "y" once locked
        self._scroll_last_t: float = 0.0

        self._last_hand_seen = time.monotonic()

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def update(self, hand: HandFrame | None) -> None:
        """Process one frame's hand features (or None if no hand)."""
        now = time.monotonic()

        if not self.enabled:
            self._safe_release()
            return

        if hand is None:
            self._on_hand_lost(now)
            return
        self._last_hand_seen = now

        self._pinch_scroll.update(hand.pinch_index)
        self._pinch_click.update(hand.pinch_middle)
        self._pinch_vsign.update(hand.pinch_vsign)

        self._update_arm_toggle(hand, now)

        if not self.armed:
            return
        if now - self._armed_at < self.cfg["arm_grace_period"]:
            return  # ignore gestures right after arming

        handler = {
            State.IDLE: self._in_idle,
            State.CURSOR: self._in_cursor,
            State.SCROLL: self._in_scroll,
            State.CLICK_PENDING: self._in_click_pending,
            State.DRAG: self._in_drag,
            State.RIGHT_CLICK_PENDING: self._in_right_click_pending,
        }.get(self.state)
        if handler is not None:
            handler(hand, now)

    def set_armed(self, armed: bool) -> None:
        """Arm/disarm from the tray (mirrors the palm gesture)."""
        now = time.monotonic()
        if armed == self.armed:
            return
        self.armed = armed
        if armed:
            self._armed_at = now
            self._transition(State.IDLE)
        else:
            self._safe_release()
            self._transition(State.DISARMED)
        if self.on_arm_change:
            self.on_arm_change(armed)
        log.info("%s", "ARMED" if armed else "DISARMED")

    def set_enabled(self, enabled: bool) -> None:
        """Pause/resume (tray 'Pause 5 minutes'). Pausing releases buttons."""
        self.enabled = enabled
        if not enabled:
            self._safe_release()

    def shutdown(self) -> None:
        """Release everything before app exit — never leave a stuck button."""
        self._safe_release()

    def apply_config(self, cfg: dict[str, Any]) -> None:
        """Live-apply new tunables (sensitivity preset, config reload)."""
        self.cfg = cfg
        self._pinch_scroll.engage_threshold = cfg["pinch_engage"]
        self._pinch_scroll.release_threshold = cfg["scroll_release"]
        self._pinch_click.engage_threshold = cfg["pinch_engage"]
        self._pinch_click.release_threshold = cfg["pinch_release"]
        self._pinch_vsign.engage_threshold = cfg["vsign_engage"]
        self._pinch_vsign.release_threshold = cfg["vsign_release"]
        self._stability.required = cfg["stability_frames"]

    # ------------------------------------------------------------------ #
    # Arm / disarm
    # ------------------------------------------------------------------ #

    def _update_arm_toggle(self, hand: HandFrame, now: float) -> None:
        """Open palm held roughly still for `arm_hold_time` toggles armed."""
        if not hand.open_palm:
            self._palm_anchor = None
            self._palm_toggled = False
            return
        if self._palm_toggled:
            return  # wait for the palm to close before another toggle

        px, py = hand.palm_center
        if self._palm_anchor is None:
            self._palm_anchor = (px, py)
            self._palm_anchor_time = now
            return
        ax, ay = self._palm_anchor
        if abs(px - ax) > 0.06 or abs(py - ay) > 0.06:  # hand moved: restart
            self._palm_anchor = (px, py)
            self._palm_anchor_time = now
            return
        if now - self._palm_anchor_time >= self.cfg["arm_hold_time"]:
            self._palm_toggled = True
            self.set_armed(not self.armed)

    def _on_hand_lost(self, now: float) -> None:
        """No hand this frame: safe-release, and auto-disarm on timeout."""
        if self.state in (State.DRAG, State.SCROLL, State.CLICK_PENDING,
                          State.RIGHT_CLICK_PENDING, State.CURSOR):
            self._safe_release()
            self._transition(State.IDLE)
        self._palm_anchor = None
        for det in (self._pinch_scroll, self._pinch_click, self._pinch_vsign):
            det.reset()
        self._stability.reset()
        if self.armed and (now - self._last_hand_seen
                           > self.cfg["auto_disarm_timeout"]):
            log.info("Auto-disarm: no hand for %.0fs",
                     self.cfg["auto_disarm_timeout"])
            self.set_armed(False)

    # ------------------------------------------------------------------ #
    # State handlers
    # ------------------------------------------------------------------ #

    def _in_idle(self, hand: HandFrame, now: float) -> None:
        candidate = self._candidate_gesture(hand)
        if candidate is None:
            self._stability.reset()
            return
        if not self._stability.update(candidate):
            return
        self._stability.reset()
        if candidate is State.SCROLL:
            self._enter_scroll(hand)
        elif candidate is State.CLICK_PENDING:
            self._enter_click_pending(now)
        elif candidate is State.RIGHT_CLICK_PENDING:
            self._enter_right_click_pending(now)
        elif candidate is State.CURSOR:
            self._transition(State.CURSOR)

    def _in_cursor(self, hand: HandFrame, now: float) -> None:
        # Pinches may start directly from cursor mode (point, then click).
        candidate = self._candidate_gesture(hand)
        if candidate in (State.SCROLL, State.CLICK_PENDING,
                         State.RIGHT_CLICK_PENDING):
            if self._stability.update(candidate):
                self._stability.reset()
                if candidate is State.SCROLL:
                    self._enter_scroll(hand)
                elif candidate is State.CLICK_PENDING:
                    self._enter_click_pending(now)
                else:
                    self._enter_right_click_pending(now)
                return
        elif candidate is not State.CURSOR:
            if self._stability.update(candidate):  # pose ended
                self._stability.reset()
                self._transition(State.IDLE)
                return
        else:
            self._stability.reset()
        self._move_cursor(hand, now)

    def _in_scroll(self, hand: HandFrame, now: float) -> None:
        # Modal lock: only the scroll pinch matters; release exits instantly.
        if not self._pinch_scroll.engaged:
            self._exit_scroll()
            return
        assert self._scroll_origin is not None
        ox, oy = self._scroll_origin
        px, py = hand.palm_center
        dx, dy = px - ox, py - oy

        dead = self.cfg["scroll_dead_zone"]
        speed = self.cfg["scroll_speed"]
        full = max(self.cfg["scroll_full_offset"], dead + 1e-3)
        direction = 1.0 if self.cfg["natural_scroll"] else -1.0

        def deflection(d: float) -> float:
            # 0 inside the dead zone, ramping to ±1 at the full-speed offset.
            if abs(d) < dead:
                return 0.0
            mag = min((abs(d) - dead) / (full - dead), 1.0)
            return mag * (1 if d > 0 else -1)

        # Axis lock: once movement clearly favors one axis, stick to it
        # for the rest of this scroll — diagonal hand wobble stops causing
        # stray horizontal scrolls during a vertical scroll (and vice versa).
        if self.cfg["scroll_axis_lock"]:
            if self._scroll_axis is None and (abs(dx) > dead or abs(dy) > dead):
                self._scroll_axis = "y" if abs(dy) >= abs(dx) else "x"
            if self._scroll_axis == "y":
                dx = 0.0
            elif self._scroll_axis == "x":
                dy = 0.0

        # Joystick-style: normalized deflection from the pinch origin sets
        # the scroll rate. Integrate over the REAL elapsed time so the rate
        # is correct even when the loop runs below target_fps.
        dt = now - self._scroll_last_t if self._scroll_last_t else 1.0 / 30
        dt = min(max(dt, 1e-3), 0.1)
        self._scroll_last_t = now
        scroll_y = direction * deflection(dy) * speed * dt
        scroll_x = -direction * deflection(dx) * speed * dt
        self.mouse.scroll_amount(scroll_x, scroll_y)

    def _in_click_pending(self, hand: HandFrame, now: float) -> None:
        held = now - self._pinch_started
        if not self._pinch_click.engaged:
            # Released: quick = click (OS pairs two quick clicks into a
            # double-click, satisfying the double-pinch rule with no lag).
            if held <= self.cfg["click_max_hold"]:
                if now - self._last_click_time >= self.cfg["click_debounce"]:
                    self.mouse.click("left")
                    self._last_click_time = now
            self._transition(State.IDLE)
            return
        if held > self.cfg["click_max_hold"]:
            self.mouse.press("left")
            self._transition(State.DRAG)

    def _in_drag(self, hand: HandFrame, now: float) -> None:
        if not self._pinch_click.engaged:
            self.mouse.release("left")
            self._transition(State.IDLE)
            return
        self._move_cursor(hand, now)  # button stays down while hand moves

    def _in_right_click_pending(self, hand: HandFrame, now: float) -> None:
        held = now - self._pinch_started
        if not self._pinch_vsign.engaged:
            if held <= self.cfg["click_max_hold"]:
                if now - self._last_click_time >= self.cfg["click_debounce"]:
                    self.mouse.click("right")
                    self._last_click_time = now
            self._transition(State.IDLE)
            return
        # Holding a right pinch does nothing extra; just wait for release.

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _candidate_gesture(self, hand: HandFrame) -> State | None:
        """This frame's most-plausible gesture, in strict priority order.

        Scroll pinch outranks everything (modal); then clicks; the plain
        pointing pose comes last.
        """
        if self._pinch_scroll.engaged:
            return State.SCROLL
        if self._pinch_click.engaged:
            return State.CLICK_PENDING
        # Right click = index+middle tips touching; requiring both fingers
        # extended keeps a curled-finger pinch from ever looking like it.
        if self._pinch_vsign.engaged and hand.vsign_pose:
            return State.RIGHT_CLICK_PENDING
        if hand.index_only:
            return State.CURSOR
        return None

    def _enter_scroll(self, hand: HandFrame) -> None:
        self._scroll_origin = hand.palm_center
        self._scroll_axis = None
        self._scroll_last_t = 0.0
        self.mouse.reset_scroll()
        log.info("scroll start")
        self._transition(State.SCROLL)

    def _exit_scroll(self) -> None:
        self._scroll_origin = None
        self._scroll_axis = None
        self.mouse.reset_scroll()
        log.info("scroll end")
        self._transition(State.IDLE)

    def _enter_click_pending(self, now: float) -> None:
        # The cursor freezes here (no _move_cursor in CLICK_PENDING), and
        # rewinds to where the user was aiming just before the pinching
        # motion perturbed the hand — clicks land on the intended target.
        self._pinch_started = now
        self._rewind_cursor(now)
        self._transition(State.CLICK_PENDING)

    def _enter_right_click_pending(self, now: float) -> None:
        self._pinch_started = now
        self._rewind_cursor(now)
        self._transition(State.RIGHT_CLICK_PENDING)

    def _rewind_cursor(self, now: float) -> None:
        """Move the cursor back to its position `click_rewind_ms` ago."""
        rewind_s = self.cfg["click_rewind_ms"] / 1000.0
        if rewind_s <= 0:
            return
        target = now - rewind_s
        for t, nx, ny in self._cursor_history:
            if t >= target:
                self.mouse.move_normalized(nx, ny)
                return

    def _move_cursor(self, hand: HandFrame, now: float) -> None:
        """Map index fingertip through the control box to the screen.

        The box maps to `cursor_gain` x the screen (centered), so edges
        are reachable without stretching the hand to the box boundary.
        """
        cfg = self.cfg
        bw, bh = cfg["control_box_width"], cfg["control_box_height"]
        cx, cy = cfg["control_box_center_x"], cfg["control_box_center_y"]
        left, top = cx - bw / 2, cy - bh / 2

        fx, fy = hand.index_tip
        gain = cfg["cursor_gain"]
        nx = 0.5 + ((fx - left) / bw - 0.5) * gain
        ny = 0.5 + ((fy - top) / bh - 0.5) * gain
        nx = min(max(nx, 0.0), 1.0)  # outside the box: clamp, don't drop
        ny = min(max(ny, 0.0), 1.0)

        self._cursor_history.append((now, nx, ny))
        self.mouse.move_normalized(nx, ny)

    def _safe_release(self) -> None:
        """Release held buttons and clear transient gesture state."""
        self.mouse.release_all()
        self.mouse.reset_scroll()
        self._scroll_origin = None
        self._scroll_axis = None
        self._cursor_history.clear()
        self._stability.reset()

    def _transition(self, new: State) -> None:
        if new is self.state:
            return
        log.debug("state %s -> %s", self.state.value, new.value)
        self.state = new
