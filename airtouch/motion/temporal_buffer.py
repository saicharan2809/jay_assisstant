"""Rolling landmark history: the MIL never sees a frame in isolation."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from ..tracker import HandFrame


@dataclass
class BufferedFrame:
    """One history entry: raw hand features at a wall-clock timestamp."""

    t: float
    hand: HandFrame


class TemporalBuffer:
    """Fixed-length rolling buffer of the most recent raw frames."""

    def __init__(self, maxlen: int = 60) -> None:
        self._frames: deque[BufferedFrame] = deque(maxlen=maxlen)

    def push(self, hand: HandFrame, t: float) -> None:
        """Append one raw frame."""
        self._frames.append(BufferedFrame(t, hand))

    def clear(self) -> None:
        """Drop all history (tracking loss beyond coasting, reset)."""
        self._frames.clear()

    def __len__(self) -> int:
        return len(self._frames)

    @property
    def latest(self) -> BufferedFrame | None:
        """Most recent entry, or None when empty."""
        return self._frames[-1] if self._frames else None

    def last(self, n: int) -> list[BufferedFrame]:
        """The most recent `n` entries, oldest first."""
        if n <= 0:
            return []
        return list(self._frames)[-n:]

    def at_or_before(self, t: float) -> BufferedFrame | None:
        """Newest entry with timestamp <= t (None if history too short)."""
        for bf in reversed(self._frames):
            if bf.t <= t:
                return bf
        return None
