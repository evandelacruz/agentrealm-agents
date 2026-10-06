"""Level: carry out the plan's ``enter_level`` op (A37).

Walks to the level's entrance at the op's cell and through it, then walks
the level's rooms toward unexplored doors, then frontier tiles. The op is
finished once inside the level there is no door or frontier step left.
"""

from __future__ import annotations

from ..config import Policy
from ..knowledge_base import KnowledgeBase
from ..memory import Memory
from ..navigation import cost_path, doors_goal_path, nearest_target
from ..navigation import stuck as nav_stuck
from ..pathing import grid_params, guided_step, nav_search, next_step
from ..world import Pos, WorldModel
from .base import PlayContext, State, StateOutcome, my_op
from .explore import plan_sets
from .intents import set_position

DOOR_GOAL, FRONTIER_GOAL, ENTRANCE_GOAL = "level:door", "level:frontier", "level:entrance"
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
    knowledge: KnowledgeBase | None = None,
    state: str = "Level",
) -> StateOutcome:
    """Doors first, then frontier tiles (PLAN.md).

    Stuck detection and escalation drive the walk; a door or frontier given
    up on is backed off for Level and Explore alike (A15).
    """
    if w.pos is None:
        return StateOutcome(None, "position unknown", state=state)
    _, plan_avoid, plan_costly = plan_sets(w, m, policy, knowledge)
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
    """Executor for ``enter_level``."""

    name = "Level"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        if ctx.policy.kind != "scripted" or not world.alive or world.pos is None:
            return False
        return my_op(ctx, self.name) is not None

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not self.guard(world, ctx)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        m, policy = ctx.memory, ctx.policy
        op = my_op(ctx, self.name)
        assert op is not None and ctx.plan is not None
        if not inside_level(world):
            return _walk_to_entrance(world, ctx, (op["x"], op["y"]))
        out = level_outcome(world, m, policy, knowledge=ctx.knowledge, state=self.name)
        if out.intents is None:
            ctx.plan.finish_current("level walked", memory=m)
        return out


def _walk_to_entrance(w: WorldModel, ctx: PlayContext, door: Pos) -> StateOutcome:
    m, policy = ctx.memory, ctx.policy
    _, plan_avoid, plan_costly = plan_sets(w, m, policy, ctx.knowledge)

    def plan(att):
        params = grid_params(policy, plan_avoid, plan_costly, allow_goal_door=True, m=m)
        return cost_path(w, door, params, nav=nav_search(m, w, ENTRANCE_GOAL, door))

    step = guided_step(m, w, ENTRANCE_GOAL, door, plan_avoid, plan)
    if step is None:
        return StateOutcome(None, f"no path to level entrance {door}", state=LevelState.name)
    return StateOutcome([set_position(step)], f"level entrance → {door}", state=LevelState.name)
