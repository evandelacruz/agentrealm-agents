"""The character's call budget, kept on our side (PLAN.md **What the API gives today**).

The server's limiter is a token bucket per character: one token per tick, a
burst of 3, about one a second while the character sleeps, spent by every
``/characters/{id}/…`` request, reads included (manual §7.4). ``Client``
spends a token here before every such request, so no caller (a decision, a
held-queue poll, a startup read, an error retry) can send more than the
server allows. A 429 means the server's bucket is empty: the pacer empties
too and holds off for its ``Retry-After``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

BURST = 3
# A sleeping character is held to about one request per second (manual §7.4).
ASLEEP_INTERVAL = 1.0
# Land a little after a window opens, so a clock skew of a few ms does not put
# two calls in one window.
WINDOW_MARGIN = 0.05


@dataclass
class Pacer:
    """One character's token bucket, refilled one token per tick window.

    The limiter buckets wall-clock time as epoch / tick interval
    (internal/api/ratelimit.go), so the pacer does the same. ``window`` is
    one tick (1 s until the world read gives the tick rate); ``asleep`` slows
    the refill to the sleeping rate.
    """

    window: float = 1.0
    asleep: bool = False
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    tokens: float = BURST
    hold_until: float = 0.0  # a 429's Retry-After
    _refilled_at: float | None = field(default=None, repr=False)

    def wait_next_window(self, not_before: float = 0.0) -> None:
        """Sleep until the next tick window opens, and past ``not_before``."""
        now = self.clock()
        next_open = (int(now / self.window) + 1) * self.window + WINDOW_MARGIN
        self.sleep(max(0.0, max(next_open, not_before, self.hold_until) - now))

    def spend(self) -> None:
        """Take one token, first waiting for the windows that refill it."""
        self._refill()
        while self.tokens < 1 - 1e-9 or self.clock() < self.hold_until:
            self.wait_next_window()
            self._refill()
        self.tokens = max(0.0, self.tokens - 1)

    def drain(self, retry_after: float | None = None) -> None:
        """The server said 429: its bucket is empty, so ours is too."""
        self._refill()
        self.tokens = 0.0
        if retry_after:
            self.hold_until = self.clock() + retry_after

    def _refill(self) -> None:
        """One token per window opened since the last refill (a fraction asleep)."""
        now = self.clock()
        if self._refilled_at is not None:
            opened = int(now / self.window) - int(self._refilled_at / self.window)
            interval = max(self.window, ASLEEP_INTERVAL) if self.asleep else self.window
            self.tokens = min(BURST, self.tokens + max(0, opened) * self.window / interval)
        self._refilled_at = now


@dataclass
class Pacers:
    """One pacer per character id, as the server keys its buckets."""

    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    by_character: dict[int, Pacer] = field(default_factory=dict)

    def get(self, cid: int) -> Pacer:
        if cid not in self.by_character:
            self.by_character[cid] = Pacer(clock=self.clock, sleep=self.sleep)
        return self.by_character[cid]


def pacer_of(client, cid: int) -> Pacer:
    """The pacer ``client`` spends ``cid``'s calls from, so decision windows
    and calls share one bucket. A client with none (a test fake) gets its own."""
    pacers = getattr(client, "pacers", None)
    return pacers.get(cid) if isinstance(pacers, Pacers) else Pacer()
