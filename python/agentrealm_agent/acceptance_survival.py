"""Survival gates shared by live acceptance runs (M7 A16, M9 A29).

Death, retreat timing, Recover safe tiles, Step loops and API errors are
judged the same way on long exploration runs as on the M7 hour.
"""

from __future__ import annotations

from dataclasses import dataclass

from .acceptance_run import TimedRunHooks
from .config import Policy
from .executor.movement import step_landing
from .healing import regen_known
from .knowledge_base import KnowledgeBase
from .memory import Memory
from .survival import should_retreat
from .world import Pos, WorldModel
from .zone_discovery import safe_tiles

LOOP_STEP_LIMIT = 24  # Step-sending decisions in a row at one cell with one reason

# States that are already the right answer when should_retreat holds.
SURVIVAL_STATES = ("Sync", "Downed", "Escape", "Retreat", "Heal", "Flee")


@dataclass(kw_only=True)
class SurvivalAcceptanceMetrics(TimedRunHooks):
    """Retreat misses, unsafe Recover and loops, on top of ``TimedRunHooks``'
    deaths, API errors and wall-clock stop."""

    retreat_misses: int = 0
    recover_withdraws: int = 0
    recover_unsafe: int = 0
    heal_actions: int = 0
    loop_detected: bool = False
    _loop_key: tuple[Pos, str] | None = None
    _loop_streak: int = 0

    def note_survival_tick(
        self,
        w: WorldModel,
        m: Memory,
        *,
        state: str,
        reason: str,
        intents: list[dict] | None,
        policy: Policy,
        params: dict[str, float | int],
        knowledge: KnowledgeBase | None,
        track_regen: bool = False,
    ) -> str | None:
        """Shared ``before_tick`` survival checks. Returns regen verdict when tracked."""
        if should_retreat(w, policy, params) and state not in SURVIVAL_STATES:
            self.retreat_misses += 1
        if intents and state == "Heal":
            self.heal_actions += 1
        if intents and state == "Recover":
            safe = safe_tiles(w, w.map_id) if w.map_id is not None else set()
            for cell in withdraw_cells(w.pos, intents):
                self.recover_withdraws += 1
                if cell not in safe:
                    self.recover_unsafe += 1
        if intents is not None:
            self._note_loop(w, reason, intents)
        if track_regen:
            return regen_known(knowledge, m)
        return None

    def _note_loop(self, w: WorldModel, reason: str, intents: list[dict]) -> None:
        moving = any(i.get("verb") == "Step" for i in intents)
        if not moving or w.pos is None:
            self._loop_key, self._loop_streak = None, 0
            return
        key = (w.pos, reason)
        self._loop_streak = self._loop_streak + 1 if key == self._loop_key else 1
        self._loop_key = key
        if self._loop_streak >= LOOP_STEP_LIMIT:
            self.loop_detected = True

    def survival_failures(self) -> list[str]:
        out = list(self.base_failures())
        if self.retreat_misses:
            out.append(f"{self.retreat_misses} tick(s) should_retreat held outside a survival state")
        if self.recover_unsafe:
            out.append(f"{self.recover_unsafe} Recover withdraw(s) from a cell not known safe")
        if self.loop_detected:
            out.append(f"loop: {LOOP_STEP_LIMIT} Steps in a row at one cell with one reason")
        return out

    def survival_summary_lines(self) -> list[str]:
        return [
            f"deaths: {self.deaths}",
            f"retreat misses: {self.retreat_misses}",
            f"recover withdraws: {self.recover_withdraws} (from a cell not known safe: {self.recover_unsafe})",
            f"heal actions: {self.heal_actions}",
            f"loop detected: {self.loop_detected}",
            f"API errors: {len(self.api_errors)}",
        ]


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
