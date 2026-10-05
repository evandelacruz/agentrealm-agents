"""M11 acceptance metrics (A40): clear one level unattended, then attempt the next.

The M11 done-when (docs/PLAYABLE_AGENT_PLAN.md, Milestones) and what this
module checks:

- Clears the easiest open level unattended: at least one ``level_clear_ceremony``
  with a ``level_number`` (boss defeat observed, never inferred from absence).
- Uses what it learned to attempt the next: after the first clear, the character
  leaves level interior maps and enters a level interior again (a positive
  ``level`` on the position read), or the plan's top op is ``enter_level`` or
  ``fight_boss`` while not already inside that level.

Shared survival gates (same as M7 A16 where they apply during a long unattended
run): no death; no retreat miss; Recover withdraws only on known safe tiles; no
Step loop; no sustained oscillation pacing abort; no API error.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from .acceptance import AcceptanceHooks, CountingClient
from .acceptance_common import (
    LOOP_STEP_LIMIT,
    OscillationAbortTracker,
    StepLoopTracker,
    note_recover_withdraws,
    note_retreat_miss,
)
from .config import Policy
from .knowledge_base import KnowledgeBase
from .memory import Memory
from .states.level import inside_level
from .world import WorldModel

TARGET_SECONDS = 7200.0  # default max wall-clock before the smoke script stops


@dataclass
class M11AcceptanceMetrics(AcceptanceHooks):
    """Counts level clears, the follow-on attempt, and shared survival faults."""

    overworld_map_id: int | None = None
    target_seconds: float = TARGET_SECONDS
    stop: threading.Event | None = None
    clock: Callable[[], float] = time.monotonic
    started_at: float | None = None
    cleared_levels: set[int] = field(default_factory=set)
    next_level_attempted: bool = False
    deaths: int = 0
    lives_seen: int | None = None
    retreat_misses: int = 0
    recover_withdraws: int = 0
    recover_unsafe: int = 0
    loop_detected: bool = False
    oscillation_ticks: list[int] = field(default_factory=list)
    pacing_give_up_ticks: list[int] = field(default_factory=list)
    oscillation_abort: str | None = None
    api_errors: list[str] = field(default_factory=list)
    _loop: StepLoopTracker = field(default_factory=StepLoopTracker)
    _oscillation: OscillationAbortTracker = field(default_factory=OscillationAbortTracker)
    _outside_after_first_clear: bool = False
    _plan_attempt_after_clear: bool = False

    def __post_init__(self) -> None:
        self._oscillation.stop = self.stop

    def wrap(self, client):
        return CountingClient(client, self.api_errors)

    def on_window(self, *, urgent: bool, alive: bool = True) -> None:
        now = self.clock()
        if self.started_at is None:
            self.started_at = now
        if self.stop is None or not alive:
            return
        if self.milestone_ok():
            self.stop.set()
        elif now - self.started_at >= self.target_seconds:
            self.stop.set()

    def on_death(self) -> None:
        self.deaths += 1
        if self.stop is not None:
            self.stop.set()

    def on_level_clear(self, ceremony: dict) -> None:
        level = ceremony.get("level_number")
        if isinstance(level, bool) or not isinstance(level, int):
            return
        self.cleared_levels.add(int(level))

    def on_oscillation(self, event: dict) -> None:
        self._oscillation.on_oscillation(event)
        self.oscillation_ticks = self._oscillation.oscillation_ticks
        self.pacing_give_up_ticks = self._oscillation.pacing_give_up_ticks
        self.oscillation_abort = self._oscillation.oscillation_abort

    def before_tick(
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
    ) -> None:
        if w.lives is not None:
            self.lives_seen = w.lives
        note_retreat_miss(self, w, policy, params, state=state)
        if intents and state == "Recover":
            note_recover_withdraws(self, w, intents)
        if intents is not None:
            self._loop.note(w, reason, intents)
            self.loop_detected = self._loop.loop_detected
        self._note_level_attempt(w, m)

    def _note_level_attempt(self, w: WorldModel, m: Memory) -> None:
        if not self.cleared_levels:
            return
        if m.goal_op is not None and m.goal_op.get("op") in ("enter_level", "fight_boss"):
            self._plan_attempt_after_clear = True
        if self._plan_attempt_after_clear:
            self.next_level_attempted = True
            return
        if inside_level(w):
            if self._outside_after_first_clear:
                self.next_level_attempted = True
            return
        self._outside_after_first_clear = True

    def milestone_ok(self) -> bool:
        return bool(self.cleared_levels) and self.next_level_attempted

    def failures(self, *, full_run: bool = True) -> list[str]:
        out: list[str] = []
        if self.deaths:
            out.append(f"{self.deaths} death(s) during run")
        if self.retreat_misses:
            out.append(f"{self.retreat_misses} tick(s) should_retreat held outside a survival state")
        if self.recover_unsafe:
            out.append(f"{self.recover_unsafe} Recover withdraw(s) from a cell not known safe")
        if self.loop_detected:
            out.append(f"loop: {LOOP_STEP_LIMIT} Steps in a row at one cell with one reason")
        if self.oscillation_abort:
            out.append(self.oscillation_abort)
        if full_run and not self.cleared_levels:
            out.append("no level_clear_ceremony seen")
        if full_run and self.cleared_levels and not self.next_level_attempted:
            out.append(
                f"cleared level(s) {sorted(self.cleared_levels)} but never attempted the next "
                "(re-enter a level or stack enter_level/fight_boss after the clear)"
            )
        if self.api_errors:
            out.append(f"{len(self.api_errors)} API error(s): {', '.join(sorted(set(self.api_errors)))}")
        return out

    def summary_lines(self) -> list[str]:
        return [
            f"levels cleared: {sorted(self.cleared_levels) or 'none'}",
            f"next level attempted: {self.next_level_attempted}",
            f"deaths: {self.deaths}",
            f"retreat misses: {self.retreat_misses}",
            f"recover withdraws: {self.recover_withdraws} (from a cell not known safe: {self.recover_unsafe})",
            f"loop detected: {self.loop_detected}",
            f"oscillation events: {len(self.oscillation_ticks)} (gave up a target: {len(self.pacing_give_up_ticks)})",
            f"API errors: {len(self.api_errors)}",
            f"lives last seen: {self.lives_seen}",
        ]
