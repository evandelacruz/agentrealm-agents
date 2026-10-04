"""Level: walk rooms toward unexplored doors inside a level (A37)."""

from __future__ import annotations

from ..config import Policy
from ..knowledge_base import KnowledgeBase
from ..memory import Memory
from ..navigation import doors_goal_path, nearest_target
from ..navigation import stuck as nav_stuck
from ..pathing import grid_params, next_step
from ..world import Pos, WorldModel
from .base import PlayContext, State, StateOutcome
from .explore import plan_sets, reflex_outcome
from .intents import set_position

GOAL = "level"


def inside_level(w: WorldModel) -> bool:
    """True when the position read names a positive ``level`` (Manual §5.3).

    Interior maps carry ``level``; the overworld omits it or sends zero.
    """
    if not w.alive or w.map_id is None:
        return False
    return w.map_level is not None and w.map_level > 0


def level_outcome(
    w: WorldModel,
    m: Memory,
    policy: Policy,
    *,
    never_attack: list[str],
    knowledge: KnowledgeBase | None = None,
    state: str = "Level",
) -> StateOutcome:
    """Doors first, then frontier tiles, with fight and pickup reflexes (PLAN.md)."""
    if w.pos is None:
        return StateOutcome(None, "position unknown", state=state)
    _, plan_avoid, plan_costly = plan_sets(w, m, policy, knowledge)
    reflex = reflex_outcome(w, policy, never_attack=never_attack, state=state)
    if reflex is not None:
        if m.goal == GOAL:
            m.path, m.goal = [], ""
        return reflex

    step = next_step(w, plan_avoid, m.path) if m.goal == GOAL else None
    if step is None:
        _replan_level(w, m, policy, plan_avoid, plan_costly, knowledge)
        step = next_step(w, plan_avoid, m.path) if m.goal == GOAL else None
    if step is not None:
        return StateOutcome([set_position(step)], f"level → {m.path[-1]}", state=state)

    return StateOutcome(None, "no level step", state=state)


def _replan_level(
    w: WorldModel,
    m: Memory,
    policy: Policy,
    blocked: set[Pos],
    costly: set[Pos],
    knowledge: KnowledgeBase | None,
) -> None:
    if m.goal == GOAL:
        m.path, m.goal = [], ""
    params = grid_params(policy, blocked, costly, allow_goal_door=True)
    # Same targets the doors and explore goals skip while backed off (A15).
    path = doors_goal_path(w, knowledge, params)
    if path and next_step(w, blocked, path) and not nav_stuck.backed_off(m, "doors", w.map_id, path[-1], w.tick):
        m.path, m.goal = path, GOAL
        return
    targets = nav_stuck.filter_frontiers(m.nav_stuck, w.map_id, w.view.frontier() - {w.pos}, w.tick)
    found = nearest_target(w, targets, grid_params(policy, blocked, costly))
    if found and next_step(w, blocked, found[1]):
        m.path, m.goal = found[1], GOAL


class LevelState(State):
    """Priority 5, below Travel and above Explore. Active on level interior maps.

    A ``travel:*`` directive that resolves outranks it (PLAN.md A37); with
    no door or frontier step it sends nothing and dispatch falls through (A44).
    """

    name = "Level"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        return (
            ctx.policy.kind == "scripted"
            and world.alive
            and world.pos is not None
            and inside_level(world)
        )

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not self.guard(world, ctx)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        return level_outcome(
            world,
            ctx.memory,
            ctx.policy,
            never_attack=ctx.never_attack,
            knowledge=ctx.knowledge,
            state=self.name,
        )
