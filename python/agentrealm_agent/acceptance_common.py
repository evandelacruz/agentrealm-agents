"""Shared live acceptance helpers (M7 A16, M11 A40)."""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import Policy
from .executor.movement import step_landing
from .survival import should_retreat
from .world import Pos, WorldModel
from .zone_discovery import safe_tiles

LOOP_STEP_LIMIT = 24  # Step-sending decisions in a row at one cell with one reason
# Sustained pacing: the guard gave up a target more than this many times in
# this many ticks (10 minutes at 10 ticks/s); events that gave nothing up do
# not count. Its backoffs double (30 s, 60 s, 120 s),
# so a target that keeps making the agent pace trips this within minutes.
OSCILLATION_ABORT_COUNT = 3
OSCILLATION_ABORT_TICKS = 6000


def withdraw_cells(pos: Pos | None, intents: list[dict]) -> list[Pos | None]:
    """Where the agent stands when each ``WithdrawFromChest`` in the queue runs.

    Walks the queue from ``pos``: a ``Step`` moves one block, a ``SetPosition``
    moves to its cell. None when the start is unknown.
    """
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


def retreat_missed(
    w: WorldModel,
    policy: Policy,
    params: dict[str, float | int],
    *,
    state: str,
    survival_states: tuple[str, ...],
) -> bool:
    """``should_retreat`` held on the decision's world while a non-survival state ran."""
    return state not in survival_states and should_retreat(w, policy, params)


def recover_withdraws(w: WorldModel, intents: list[dict]) -> tuple[int, int]:
    """``(withdraws, unsafe)``: Recover's ``WithdrawFromChest`` intents, and those off a known safe tile."""
    safe = safe_tiles(w, w.map_id) if w.map_id is not None else set()
    cells = withdraw_cells(w.pos, intents)
    return len(cells), sum(1 for cell in cells if cell not in safe)


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
    """Counts the guard's events and decides when sustained pacing aborts the run (A15)."""

    oscillation_ticks: list[int] = field(default_factory=list)  # each guard event's tick
    pacing_give_up_ticks: list[int] = field(default_factory=list)  # ticks of events that gave up a target
    oscillation_abort: str | None = None  # why the run was stopped for pacing

    def on_oscillation(self, event: dict) -> bool:
        """True when this event is the one that trips the abort; the caller stops the run.

        Only an event that gave up a target (it carries ``goal``) counts
        toward the abort; survival states pacing on their own do not.
        """
        tick = int(event.get("tick") or 0)
        self.oscillation_ticks.append(tick)
        if "goal" not in event:
            return False
        self.pacing_give_up_ticks.append(tick)
        recent = [t for t in self.pacing_give_up_ticks if tick - t < OSCILLATION_ABORT_TICKS]
        if len(recent) <= OSCILLATION_ABORT_COUNT or self.oscillation_abort is not None:
            return False
        self.oscillation_abort = (
            f"sustained oscillation: gave up {len(recent)} targets for pacing in "
            f"{OSCILLATION_ABORT_TICKS} ticks (last at tick {tick}: {event['goal']} → "
            f"{tuple(event.get('target') or ())}, cells {event.get('cells')}, moved by "
            f"{', '.join(event.get('states') or []) or 'no state'})"
        )
        return True
