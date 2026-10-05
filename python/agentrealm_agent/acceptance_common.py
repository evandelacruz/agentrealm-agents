"""Shared live acceptance helpers (M7 A16, M11 A40)."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from .config import Policy
from .executor.movement import step_landing
from .knowledge_base import KnowledgeBase
from .memory import Memory
from .survival import should_retreat
from .world import Pos, WorldModel
from .zone_discovery import safe_tiles

LOOP_STEP_LIMIT = 24  # Step-sending decisions in a row at one cell with one reason
OSCILLATION_ABORT_COUNT = 3
OSCILLATION_ABORT_TICKS = 6000

SURVIVAL_STATES = ("Sync", "Downed", "Escape", "Retreat", "Heal", "Flee", "Boss")


def withdraw_cells(pos: Pos | None, intents: list[dict]) -> list[Pos | None]:
    """Where the agent stands when each ``WithdrawFromChest`` in the queue runs."""
    cells: list[Pos | None] = []
    for intent in intents:
        verb = intent.get("verb")
        if pos is not None and verb == "Step":
            pos = step_landing(pos, intent["direction"])
        elif verb == "SetPosition":
            pos = (int(intent["x"]), int(intent["y"]))
        elif verb == "WithdrawFromChest":
            cells.append(pos)
    return cells


@dataclass
class StepLoopTracker:
    """Detects repeated Step decisions at one cell with one reason."""

    loop_detected: bool = False
    _loop_key: tuple[Pos, str] | None = None
    _loop_streak: int = 0

    def note(self, w: WorldModel, reason: str, intents: list[dict]) -> None:
        moving = any(i.get("verb") == "Step" for i in intents)
        if not moving or w.pos is None:
            self._loop_key, self._loop_streak = None, 0
            return
        key = (w.pos, reason)
        self._loop_streak = self._loop_streak + 1 if key == self._loop_key else 1
        self._loop_key = key
        if self._loop_streak >= LOOP_STEP_LIMIT:
            self.loop_detected = True


@dataclass
class OscillationAbortTracker:
    """Ends the run when pacing give-ups repeat within a tick window (A15)."""

    stop: threading.Event | None = None
    oscillation_ticks: list[int] = field(default_factory=list)
    pacing_give_up_ticks: list[int] = field(default_factory=list)
    oscillation_abort: str | None = None

    def on_oscillation(self, event: dict) -> None:
        tick = int(event.get("tick") or 0)
        self.oscillation_ticks.append(tick)
        if "goal" not in event:
            return
        self.pacing_give_up_ticks.append(tick)
        recent = [t for t in self.pacing_give_up_ticks if tick - t < OSCILLATION_ABORT_TICKS]
        if len(recent) > OSCILLATION_ABORT_COUNT and self.oscillation_abort is None:
            self.oscillation_abort = (
                f"sustained oscillation: gave up {len(recent)} targets for pacing in "
                f"{OSCILLATION_ABORT_TICKS} ticks (last at tick {tick}: {event['goal']} → "
                f"{tuple(event.get('target') or ())}, cells {event.get('cells')}, moved by "
                f"{', '.join(event.get('states') or []) or 'no state'})"
            )
            if self.stop is not None:
                self.stop.set()


def note_retreat_miss(
    metrics: object,
    w: WorldModel,
    policy: Policy,
    params: dict[str, float | int],
    *,
    state: str,
) -> None:
    if should_retreat(w, policy, params) and state not in SURVIVAL_STATES:
        metrics.retreat_misses += 1  # type: ignore[attr-defined]


def note_recover_withdraws(
    metrics: object,
    w: WorldModel,
    intents: list[dict],
) -> None:
    safe = safe_tiles(w, w.map_id) if w.map_id is not None else set()
    for cell in withdraw_cells(w.pos, intents):
        metrics.recover_withdraws += 1  # type: ignore[attr-defined]
        if cell not in safe:
            metrics.recover_unsafe += 1  # type: ignore[attr-defined]
