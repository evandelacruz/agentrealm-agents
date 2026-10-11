"""Level: carry out the plan's ``enter_level`` op (A37).

Walks to the level's entrance at the op's cell and through it, then walks
the level's rooms toward unexplored doors, then frontier tiles. The op is
finished once inside the level there is no door or frontier step left.

Every target is committed (A71): the entrance, and inside, the door or
frontier picked, kept until reached, gone or given up. An entrance a look
filed as locked and needing an item (``needs``) that is not carried, with
one in sight, gets that item as a prerequisite stop before it: Level walks
to it and takes it first, then resumes toward the entrance.
"""

from __future__ import annotations

from .. import targets as targets_mod
from ..config import Policy
from ..healing import supply_matches
from ..loot import Pickup, loot_score
from ..pack import TAKE
from ..knowledge_base import KnowledgeBase, knowledge_items
from ..memory import Memory
from ..navigation import cost_path, doors_goal_path, nearest_target
from ..navigation import stuck as nav_stuck
from ..pathing import bounded_step, grid_params, guided_step, nav_search, next_step
from ..travel.knowledge import iter_entrances
from ..world import DOORS, Entity, Pos, WorldModel, chebyshev
from .base import PlayContext, State, StateOutcome, my_op, top_op
from .explore import plan_sets
from .intents import set_position, take
from .pickup import room_for

DOOR_GOAL, FRONTIER_GOAL, ENTRANCE_GOAL = "level:door", "level:frontier", "level:entrance"
PREREQUISITE_GOAL = "level:prerequisite"
GOALS = (DOOR_GOAL, FRONTIER_GOAL)
# The commitment of the door or frontier Level walks to inside a level (A71).
LEVEL_TARGET = "level"
# Takes sent for a prerequisite before its stop is given up (A71).
PREREQUISITE_TRIES = 3


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

        def params():
            return grid_params(policy, plan_avoid, plan_costly, allow_goal_door=True, m=m)

        def plan(att):
            return cost_path(w, target, params(), nav=nav_search(m, w, goal, target))

        step = guided_step(m, w, goal, target, plan_avoid, plan, params=params)
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
    frontier = nav_stuck.filter_frontiers(m.nav_stuck, w.map_id, w.view.frontier() - {w.pos}, w.tick)

    def keep(t: tuple[str, int | None, Pos]) -> bool:
        # Still a door not given up on, or still a frontier: kept even when
        # another came nearer while another walk had the move (A71).
        goal, mid, p = t
        if mid != w.map_id or p == w.pos:
            return False
        if goal == DOOR_GOAL:
            return w.view.tiles.get(p) in DOORS and not nav_stuck.backed_off(m, DOOR_GOAL, mid, p, w.tick)
        return p in frontier

    def pick() -> tuple[str, int | None, Pos] | None:
        if m.goal in GOALS:
            m.path, m.goal = [], ""
        # Doors and frontiers given up on stay out of the choice while backed off.
        path = doors_goal_path(w, knowledge, grid_params(policy, blocked, costly, allow_goal_door=True, m=m))
        if path and next_step(w, blocked, path) and not nav_stuck.backed_off(m, DOOR_GOAL, w.map_id, path[-1], w.tick):
            return DOOR_GOAL, w.map_id, path[-1]
        found = nearest_target(w, frontier, grid_params(policy, blocked, costly, m=m))
        if found and next_step(w, blocked, found[1]):
            return FRONTIER_GOAL, w.map_id, found[0]
        return None

    held = targets_mod.hold(m, w, LEVEL_TARGET, pick, keep)
    return (held[0], held[2]) if held is not None else None


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
    targets_mod.commit(m, w, ENTRANCE_GOAL, (w.map_id, door), top_op(ctx))
    if out := _prerequisite_step(w, ctx, door):
        return out
    _, plan_avoid, plan_costly = plan_sets(w, m, policy, ctx.knowledge)

    def params():
        return grid_params(policy, plan_avoid, plan_costly, allow_goal_door=True, m=m)

    def plan(att):
        return cost_path(w, door, params(), nav=nav_search(m, w, ENTRANCE_GOAL, door))

    step = guided_step(m, w, ENTRANCE_GOAL, door, plan_avoid, plan, params=params)
    if step is None:
        return StateOutcome(None, f"no path to level entrance {door}", state=LevelState.name)
    return StateOutcome([set_position(step)], f"level entrance → {door}", state=LevelState.name)


def _needs(w: WorldModel, ctx: PlayContext, door: Pos) -> str | None:
    """The item a look filed as needed at the locked entrance ``door``, when none is carried."""
    for mid, pos, row in iter_entrances(ctx.knowledge):
        if (mid, pos) != (w.map_id, door) or not row.get("locked") or not isinstance(row.get("needs"), str):
            continue
        needs = row["needs"]
        carried = [s.code for s in w.held_supplies + w.chest_supplies] + [w.armed_code or ""]
        return None if any(supply_matches(needs, code) for code in carried if code) else needs
    return None


def _prerequisite_step(w: WorldModel, ctx: PlayContext, door: Pos) -> StateOutcome | None:
    """Take the item the entrance needs before walking to it (A71): a stop
    inserted before the committed entrance, which stays the target.

    The stop is set when the needed item is in sight and not carried, and
    is finished once the item is carried or seen gone. None when there is
    no stop to walk, or no step toward it now: the entrance walk goes on.
    """
    m = ctx.memory
    here = w.pos
    assert here is not None
    needs = _needs(w, ctx, door)
    stop = targets_mod.next_stop(m, ENTRANCE_GOAL)
    if stop is not None:
        item = _supply(w, stop.supply_id)
        if needs is None or (item is None and chebyshev(stop.pos, here) <= w.perception):
            targets_mod.finish_stop(m, ENTRANCE_GOAL, stop)
            stop = None
    if stop is None and needs is not None:
        found = [e for e in w.entities if e.kind == "supply" and e.gem_price is None and supply_matches(needs, e.code or "")]
        if found:
            item = min(found, key=lambda e: (chebyshev(e.pos, here), e.id))
            stop = targets_mod.Stop(item.pos, f"prerequisite: {needs}", item.id)
            targets_mod.add_stop(m, ENTRANCE_GOAL, stop)
    if stop is None:
        return None
    if chebyshev(stop.pos, here) <= 1 and stop.supply_id is not None:
        item = _supply(w, stop.supply_id)
        items = knowledge_items(ctx.knowledge)
        fits = item is not None and room_for(w, ctx, Pickup(item.id, item.code, item.pos, None, loot_score(item.code, items))).kind == TAKE
        if not fits or stop.tries >= PREREQUISITE_TRIES:
            # No room, or refused every time: the entrance walk goes on without it.
            targets_mod.finish_stop(m, ENTRANCE_GOAL, stop, given_up=True)
            return None
        stop.tries += 1
        return StateOutcome([take(stop.supply_id)], f"take {needs} before entrance {door}", state=LevelState.name)
    policy = ctx.policy
    _, plan_avoid, plan_costly = plan_sets(w, m, policy, ctx.knowledge)

    def params():
        return grid_params(policy, plan_avoid, plan_costly, m=m)

    def plan():
        return cost_path(w, stop.pos, params(), nav=nav_search(m, w, PREREQUISITE_GOAL, stop.pos))

    step = bounded_step(m, w, PREREQUISITE_GOAL, stop.pos, plan_avoid, plan, params=params)
    if step is None:
        if nav_stuck.backed_off(m, PREREQUISITE_GOAL, w.map_id, stop.pos, w.tick):
            targets_mod.finish_stop(m, ENTRANCE_GOAL, stop, given_up=True)  # the entrance walk goes on
        return None
    return StateOutcome([set_position(step)], f"{needs} → {stop.pos} before entrance {door}", state=LevelState.name)


def _supply(w: WorldModel, supply_id: int | None) -> Entity | None:
    return next((e for e in w.entities if e.kind == "supply" and e.id == supply_id), None)
