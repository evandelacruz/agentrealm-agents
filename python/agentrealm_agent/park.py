"""Park before exiting (A65): the world does not pause when the client stops.

A run that ends (its time limit, Ctrl-C, SIGTERM) leaves the character in
the world. Standing on field ground, it can be killed while nobody plays it.
So the runner first parks: it walks to the nearest known safe tile or the
town cell, with the survival reflexes still on (``states/park.py``), for at
most ``park_seconds``. Then it clears the intent queue, so no stale queue
keeps moving the character, and logs how the park ended.

The park phase is not the scenario: acceptance hooks are off during it, and
its outcome reaches them once, as a ``ParkReport`` (``AcceptanceHooks.on_park_end``).
"""

from __future__ import annotations

import signal
import threading
from dataclasses import dataclass
from typing import Callable

from .knowledge_base import KnowledgeBase
from .survival import retreat_goal
from .world import Pos, WorldModel

# Wall-clock cap on the park phase. 0 turns parking off.
DEFAULT_PARK_SECONDS = 60.0
# After a second stop signal, how long the process waits for the runner
# (its queue clear included) before it exits anyway.
ABORT_JOIN_SECONDS = 5.0

PARK_SECONDS_HELP = (
    f"on stop, walk to safe ground for up to this many seconds before exiting (default {DEFAULT_PARK_SECONDS:.0f}; 0 = off)"
)

# How a park ended (``ParkReport.outcome``).
PARKED_SAFE = "parked safe"
PARK_TIMED_OUT = "park timed out"
PARK_ABORTED = "park aborted"  # a second stop signal
PARK_NOWHERE = "no safe ground known"
PARK_DOWNED = "downed"  # dead before the park began
PARK_DIED = "died while parking"


@dataclass
class ParkReport:
    """How the park phase ended, where, and how long it took."""

    outcome: str
    seconds: float
    map_id: int | None
    pos: Pos | None
    queue_cleared: bool

    def line(self) -> str:
        where = f"{self.map_id}:{self.pos[0]},{self.pos[1]}" if self.pos is not None else "unknown position"
        cleared = "queue cleared" if self.queue_cleared else "queue NOT cleared"
        return f"park: {self.outcome} at {where} after {self.seconds:.1f}s, {cleared}"

    def to_dict(self) -> dict:
        return {
            "outcome": self.outcome,
            "seconds": round(self.seconds, 3),
            "map": self.map_id,
            "pos": list(self.pos) if self.pos is not None else None,
            "queue_cleared": self.queue_cleared,
        }


def parked(w: WorldModel, knowledge: KnowledgeBase | None) -> bool:
    """Standing where Retreat would head: a known safe tile, or the town cell."""
    return w.pos is not None and retreat_goal(w, knowledge) == w.pos


def install_stop_signals(
    stop: threading.Event, abort: threading.Event, out: Callable[[str], None], park_seconds: float
) -> Callable[[], None]:
    """SIGINT and SIGTERM stop the run; a second one sets ``abort``.

    The caller then stops waiting on the runner after at most
    ``ABORT_JOIN_SECONDS``, whatever the runner is doing.

    Returns a function that puts the previous handlers back. Outside the main
    thread (where Python allows no handlers) it installs nothing.
    """
    if threading.current_thread() is not threading.main_thread():
        return lambda: None

    def handle(signum, _frame) -> None:
        name = signal.Signals(signum).name
        if stop.is_set():
            abort.set()
            out(
                f"{name} again: exiting without parking, within {ABORT_JOIN_SECONDS:.0f}s; "
                "the queue is cleared only if that call finishes first"
            )
        elif park_seconds > 0:
            stop.set()
            out(f"{name}: stopping; parking on safe ground first (up to {park_seconds:.0f}s, {name} again to exit now)")
        else:
            stop.set()
            out(f"{name}: stopping; parking is off, the character stays where it stands")

    previous = {s: signal.signal(s, handle) for s in (signal.SIGINT, signal.SIGTERM)}

    def restore() -> None:
        for s, h in previous.items():
            signal.signal(s, h)

    return restore
