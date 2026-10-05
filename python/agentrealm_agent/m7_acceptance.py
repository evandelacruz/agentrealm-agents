"""M7 acceptance metrics (A16): an hour of survival and one long walk on the overworld.

The M7 done-when (docs/PLAYABLE_AGENT_PLAN.md, Milestones) and what this
module checks for each clause:

- Survives an hour: no ``Died`` event, alive when the hour ends.
- Retreats in time: no tick where ``should_retreat`` held on the world the
  decision saw while a non-survival state was running.
- Recovers only when safe: every ``WithdrawFromChest`` Recover sends runs while
  the agent stands on a known safe tile (a ``get_zone`` read said so). The cell
  is where the queue puts the agent when that intent runs, not where it stands now.
- Measures safe-zone regen: ``regen_known`` has an answer ("yes" or "no"). A
  "yes" an earlier run saved to the knowledge base counts.
- Reaches a point 150 blocks away, or gives up with a reason: the smoke script
  gives the agent one ``goto`` target on the overworld. It passes only when the
  agent stands on that target, or stuck detection gave up on that very target
  (the reason is recorded). Give-ups on other goals, such as frontier cells
  while exploring, are counted but never pass navigation.
- Never loops: no run of ``LOOP_STEP_LIMIT`` Step-sending decisions in a row at
  the same cell with the same reason. A wait (Heal resting in a safe zone) is
  not movement, so it never counts as a loop.

- Does not pace: the oscillation guard (``navigation/oscillation.py``) gives
  up a target the agent paces toward. More than ``OSCILLATION_ABORT_COUNT``
  of those give-ups within ``OSCILLATION_ABORT_TICKS`` ends the run at once,
  so a live hour never burns its time walking back and forth. Guard events
  that gave nothing up (survival states such as Fight and Retreat doing the
  moving) are counted and reported, never an abort.

Heal actions are reported but not gated: a character that is never hurt has
nothing to heal.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from .acceptance import AcceptanceHooks, CountingClient
from .config import Policy
from .executor.movement import step_landing
from .healing import regen_known
from .knowledge_base import KnowledgeBase
from .memory import Memory
from .survival import should_retreat
from .world import Pos, WorldModel, chebyshev
from .zone_discovery import safe_tiles

TARGET_SECONDS = 3600.0
TARGET_DISTANCE = 150
LOOP_STEP_LIMIT = 24  # Step-sending decisions in a row at one cell with one reason
# Sustained pacing: the guard gave up a target more than this many times in
# this many ticks (10 minutes at 10 ticks/s); events that gave nothing up do
# not count. Its backoffs double (30 s, 60 s, 120 s),
# so a target that keeps making the agent pace trips this within minutes.
OSCILLATION_ABORT_COUNT = 3
OSCILLATION_ABORT_TICKS = 6000

# States that are already the right answer when should_retreat holds.
SURVIVAL_STATES = ("Sync", "Downed", "Escape", "Retreat", "Heal", "Flee")


@dataclass
class M7AcceptanceMetrics(AcceptanceHooks):
    """Counts survival, navigation and API faults while the runner plays one hour.

    ``target`` is the overworld cell the agent is sent to (``policy.goto``),
    ``origin`` where it started. ``stop`` is set once ``target_seconds`` have
    passed by ``clock`` with the character alive.
    """

    overworld_map_id: int
    origin: Pos
    target: Pos
    target_seconds: float = TARGET_SECONDS
    stop: threading.Event | None = None
    clock: Callable[[], float] = time.monotonic
    started_at: float | None = None
    max_distance: int = 0
    target_reached: bool = False
    target_give_up: str | None = None  # stuck detection's reason for giving up on the target
    other_give_ups: int = 0  # give-ups on any other goal: reported, never a pass
    deaths: int = 0
    lives_seen: int | None = None
    retreat_misses: int = 0
    recover_withdraws: int = 0
    recover_unsafe: int = 0
    heal_actions: int = 0
    regen: str | None = None  # "yes" or "no" once measured
    loop_detected: bool = False
    oscillation_ticks: list[int] = field(default_factory=list)  # each guard event's tick
    pacing_give_up_ticks: list[int] = field(default_factory=list)  # ticks of events that gave up a target
    oscillation_abort: str | None = None  # why the run was stopped for pacing
    api_errors: list[str] = field(default_factory=list)
    _seen_give_ups: set[tuple[str, int]] = field(default_factory=set)
    _loop_key: tuple[Pos, str] | None = None
    _loop_streak: int = 0

    def wrap(self, client):
        return CountingClient(client, self.api_errors)

    def on_window(self, *, urgent: bool, alive: bool = True) -> None:
        now = self.clock()
        if self.started_at is None:
            self.started_at = now
        if self.stop is not None and alive and now - self.started_at >= self.target_seconds:
            self.stop.set()

    def on_death(self) -> None:
        """A death already fails the run, so end it rather than play on."""
        self.deaths += 1
        if self.stop is not None:
            self.stop.set()

    def on_oscillation(self, event: dict) -> None:
        """Count the guard's events; sustained give-ups for pacing end the run.

        Only an event that gave up a target (it carries ``goal``) counts
        toward the abort; survival states pacing on their own do not.
        """
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
        self._note_navigation(w)
        self._note_give_ups(m)
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
        self.regen = regen_known(knowledge, m) or self.regen
        if intents is not None:
            self._note_loop(w, reason, intents)

    def _note_navigation(self, w: WorldModel) -> None:
        if w.pos is None or w.map_id != self.overworld_map_id:
            return
        self.max_distance = max(self.max_distance, chebyshev(w.pos, self.origin))
        if w.pos == self.target:
            self.target_reached = True

    def _note_give_ups(self, m: Memory) -> None:
        """Read stuck signals before the strategist drains them (it does so at the next window)."""
        for sig in m.nav_stuck.stuck_signals:
            seen = (str(sig.get("goal_key")), int(sig.get("tick") or 0))
            if seen in self._seen_give_ups:
                continue
            self._seen_give_ups.add(seen)
            on_target = sig.get("map_id") == self.overworld_map_id and tuple(sig.get("target") or ()) == self.target
            if on_target and self.target_give_up is None:
                self.target_give_up = str(sig.get("reason") or "stuck")
            elif not on_target:
                self.other_give_ups += 1

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

    def navigation_ok(self) -> bool:
        """Stood on the target, or stuck detection gave up on that target with a reason."""
        return self.target_reached or self.target_give_up is not None

    def failures(self, *, full_hour: bool = True) -> list[str]:
        """What fails the run. Navigation and regen are judged only on a full hour."""
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
        if full_hour and not self.navigation_ok():
            out.append(f"target {self.target} neither reached nor given up on (max distance {self.max_distance})")
        if full_hour and self.regen is None:
            out.append("safe-zone regen never measured")
        if self.api_errors:
            out.append(f"{len(self.api_errors)} API error(s): {', '.join(sorted(set(self.api_errors)))}")
        return out

    def summary_lines(self) -> list[str]:
        if self.target_reached:
            nav = "reached"
        elif self.target_give_up is not None:
            nav = f"gave up ({self.target_give_up})"
        else:
            nav = "neither"
        return [
            f"navigation target {self.target} from {self.origin}: {nav}",
            f"max chebyshev distance from origin: {self.max_distance}",
            f"give-ups on other goals: {self.other_give_ups}",
            f"deaths: {self.deaths}",
            f"retreat misses: {self.retreat_misses}",
            f"recover withdraws: {self.recover_withdraws} (from a cell not known safe: {self.recover_unsafe})",
            f"heal actions: {self.heal_actions}",
            f"safe-zone regen: {self.regen or 'not measured'}",
            f"loop detected: {self.loop_detected}",
            f"oscillation events: {len(self.oscillation_ticks)} (gave up a target: {len(self.pacing_give_up_ticks)})",
            f"API errors: {len(self.api_errors)}",
            f"lives last seen: {self.lives_seen}",
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
