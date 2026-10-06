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
  moving) are counted and reported, never an abort. A Heal or Loot walk the
  guard gives up counts like any other.

Heal actions are reported but not gated: a character that is never hurt has
nothing to heal.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .acceptance_survival import LOOP_STEP_LIMIT, SurvivalAcceptanceMetrics, withdraw_cells
from .config import Policy
from .knowledge_base import KnowledgeBase
from .memory import Memory
from .world import Pos, WorldModel, chebyshev

TARGET_SECONDS = 3600.0
TARGET_DISTANCE = 150
# Sustained pacing: the guard gave up a target more than this many times in
# this many ticks (10 minutes at 10 ticks/s); events that gave nothing up do
# not count. Its backoffs double (30 s, 60 s, 120 s),
# so a target that keeps making the agent pace trips this within minutes.
OSCILLATION_ABORT_COUNT = 3
OSCILLATION_ABORT_TICKS = 6000

__all__ = [
    "LOOP_STEP_LIMIT",
    "OSCILLATION_ABORT_COUNT",
    "OSCILLATION_ABORT_TICKS",
    "TARGET_DISTANCE",
    "TARGET_SECONDS",
    "M7AcceptanceMetrics",
    "withdraw_cells",
]


@dataclass(kw_only=True)
class M7AcceptanceMetrics(SurvivalAcceptanceMetrics):
    """Counts survival, navigation and API faults while the runner plays one hour.

    ``target`` is the overworld cell the agent is sent to (``policy.goto``),
    ``origin`` where it started. ``stop`` is set once ``target_seconds`` have
    passed by ``clock`` with the character alive.
    """

    overworld_map_id: int
    origin: Pos
    target: Pos
    target_seconds: float = TARGET_SECONDS
    max_distance: int = 0
    target_reached: bool = False
    target_give_up: str | None = None  # stuck detection's reason for giving up on the target
    other_give_ups: int = 0  # give-ups on any other goal: reported, never a pass
    lives_seen: int | None = None
    regen: str | None = None  # "yes" or "no" once measured
    oscillation_ticks: list[int] = field(default_factory=list)  # each guard event's tick
    pacing_give_up_ticks: list[int] = field(default_factory=list)  # ticks of events that gave up a target
    oscillation_abort: str | None = None  # why the run was stopped for pacing
    _seen_give_ups: set[tuple[str, int]] = field(default_factory=set)

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
        acted_op: dict | None = None,
    ) -> None:
        if w.lives is not None:
            self.lives_seen = w.lives
        self._note_navigation(w)
        self._note_give_ups(m)
        regen = self.note_survival_tick(
            w,
            m,
            state=state,
            reason=reason,
            intents=intents,
            policy=policy,
            params=params,
            knowledge=knowledge,
            track_regen=True,
        )
        self.regen = regen or self.regen

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

    def navigation_ok(self) -> bool:
        """Stood on the target, or stuck detection gave up on that target with a reason."""
        return self.target_reached or self.target_give_up is not None

    def failures(self, *, full_hour: bool = True) -> list[str]:
        """What fails the run. Navigation and regen are judged only on a full hour."""
        out = list(self.survival_failures())
        if self.oscillation_abort:
            out.append(self.oscillation_abort)
        if full_hour and not self.navigation_ok():
            out.append(f"target {self.target} neither reached nor given up on (max distance {self.max_distance})")
        if full_hour and self.regen is None:
            out.append("safe-zone regen never measured")
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
            *self.survival_summary_lines(),
            f"safe-zone regen: {self.regen or 'not measured'}",
            f"oscillation events: {len(self.oscillation_ticks)} (gave up a target: {len(self.pacing_give_up_ticks)})",
            f"lives last seen: {self.lives_seen}",
        ]
