"""Level: walk rooms toward unexplored doors inside a level (A37)."""

from __future__ import annotations

from ..config import Policy
from ..knowledge_base import KnowledgeBase
from ..memory import Memory
from ..navigation import cost_path, doors_goal_path, nearest_target
from ..navigation import stuck as nav_stuck
from ..pathing import grid_params, guided_step, nav_search, next_step
from ..world import Pos, WorldModel
from .base import PlayContext, State, StateOutcome
from .explore import plan_sets, reflex_outcome
from .intents import set_position

DOOR_GOAL, FRONTIER_GOAL = "level:door", "level:frontier"
GOALS = (DOOR_GOAL, FRONTIER_GOAL)


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
    """Doors first, then frontier tiles, with fight and pickup reflexes (PLAN.md).

    Stuck detection and escalation drive the walk; a door or frontier given
    up on is backed off for Level and Explore alike (A15).
    """
    if w.pos is None:
        return StateOutcome(None, "position unknown", state=state)
    _, plan_avoid, plan_costly = plan_sets(w, m, policy, knowledge)
    reflex = reflex_outcome(w, policy, never_attack=never_attack, state=state)
    if reflex is not None:
        if m.goal in GOALS:
            m.path, m.goal = [], ""
        return reflex

    choice = _level_target(w, m, policy, plan_avoid, plan_costly, knowledge)
    if choice is not None:
        goal, target = choice

        def plan(att):
            params = grid_params(policy, plan_avoid, plan_costly, allow_goal_door=True, m=m)
            return cost_path(w, target, params, nav=nav_search(m, w, goal, target))

        step = guided_step(m, w, goal, target, plan_avoid, plan)
        if step is not None:
            note = nav_stuck.level_note(nav_stuck.active(m, w))
            return StateOutcome([set_position(step)], f"level → {target}{note}", state=state)

    return StateOutcome(None, "no level step", state=state)


def _level_target(
    w: WorldModel,
    m: Memory,
    policy: Policy,
    blocked: set[Pos],
    costly: set[Pos],
    knowledge: KnowledgeBase | None,
) -> tuple[str, Pos] | None:
    """The door or frontier Level walks to, and its goal label.

    The active Level attempt keeps its target while it escalates, so a walk
    that has lost its path still climbs the ladder to a give-up (A15).
    """
    att = nav_stuck.active(m, w)
    if att is not None and att.goal in GOALS:
        if not nav_stuck.done(att, w):
            return att.goal, att.target
        nav_stuck.finish(m, att)
    if m.goal in GOALS:
        m.path, m.goal = [], ""
    # Doors and frontiers given up on stay out of the choice while backed off.
    path = doors_goal_path(w, knowledge, grid_params(policy, blocked, costly, allow_goal_door=True, m=m))
    if path and next_step(w, blocked, path) and not nav_stuck.backed_off(m, DOOR_GOAL, w.map_id, path[-1], w.tick):
        return DOOR_GOAL, path[-1]
    targets = nav_stuck.filter_frontiers(m.nav_stuck, w.map_id, w.view.frontier() - {w.pos}, w.tick)
    found = nearest_target(w, targets, grid_params(policy, blocked, costly, m=m))
    if found and next_step(w, blocked, found[1]):
        return FRONTIER_GOAL, found[0]
    return None


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
