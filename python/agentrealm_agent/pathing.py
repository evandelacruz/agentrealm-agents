"""Pathing helpers the states share: flee, next step, replan (M3, A12)."""

from __future__ import annotations

import random

from .config import Policy
from .knowledge_base import KnowledgeBase
from .memory import Memory
from .navigation import (
    CostGridParams,
    NavSearchState,
    doors_goal_path,
    known_prefix,
    nearest_target,
    route_first_leg,
)
from .plan import (
    EXPLORE_ANYWHERE,
    EXPLORE_PATH_OPS,
    OP_STATE,
    PLAN_STALL_SECONDS,
    TRAVEL_PATHED,
    GoalOp,
    Plan,
    explore_targets,
)
from .world import DOORS, Entity, Pos, WorldModel, chebyshev


def hostiles_in_range(w: WorldModel, policy: Policy) -> list[Entity]:
    """Entities of a ``policy.hostile`` kind within ``policy.hostile_range``."""
    here = w.pos
    if here is None:
        return []
    return [e for e in w.entities if e.kind in policy.hostile and chebyshev(e.pos, here) <= policy.hostile_range]


def flee_step(w: WorldModel, hostiles: list[Entity], blocked: set[Pos]) -> Pos | None:
    here = w.pos
    options = w.open_neighbours(here, blocked) + [here]

    def safety(p: Pos) -> tuple[int, int]:
        nearest = min(chebyshev(p, h.pos) for h in hostiles)
        total = sum(chebyshev(p, h.pos) for h in hostiles)
        return nearest, total

    best = max(options, key=lambda p: (safety(p), p))
    return None if best == here else best


def step_open(w: WorldModel, blocked: set[Pos], p: Pos) -> bool:
    if chebyshev(w.pos, p) > w.movement:
        return False
    if p in blocked:
        return False
    if w.view.tiles.get(p) in DOORS:
        return True
    return w.view.walkable(p) and p not in w.occupied()


def next_step(w: WorldModel, blocked: set[Pos], path: list[Pos] | None) -> Pos | None:
    """The path's first step when it is seen and open, else None."""
    prefix = known_prefix(path or [], w.view)
    if prefix and step_open(w, blocked, prefix[0]):
        return prefix[0]
    return None


def path_for_plan_op(
    op: GoalOp,
    w: WorldModel,
    m: Memory,
    policy: Policy,
    blocked: set[Pos],
    costly: set[Pos],
    knowledge: KnowledgeBase | None,
) -> tuple[list[Pos], str] | None:
    """A cost-grid path for an ``explore_area`` or ``travel`` op, and its goal label (A34)."""
    if op["op"] == "explore_area":
        targets = explore_targets(op, w)
        center = (op["x"], op["y"])
        if not targets and op["radius"] < EXPLORE_ANYWHERE and chebyshev(w.pos, center) > op["radius"]:
            targets = {center}
        found = nearest_target(w, targets, grid_params(policy, blocked, costly))
        return (found[1], "explore_area") if found and found[1] else None
    if op["op"] != "travel":
        return None
    params = grid_params(policy, blocked, costly, allow_goal_door=True)
    if op["to"] == "point":
        target = (op["x"], op["y"])
        dest_map = op.get("map_id", w.map_id)
        nav = nav_search(m, w, "plan_goto", target) if dest_map == w.map_id else None
        path = route_first_leg(w, knowledge, dest_map, target, params, nav=nav)
        return (path, "plan_travel") if path else None
    if op["to"] == "entrance":
        path = doors_goal_path(w, knowledge, params)
        return (path, "plan_entrance") if path else None
    if op["to"] == "town":
        for map_id, pos in w.respawn_anchors:
            if map_id == w.map_id:
                path = route_first_leg(w, knowledge, map_id, pos, params, nav=nav_search(m, w, "plan_town", pos))
                if path:
                    return path, "plan_town"
    return None


def plan_step(
    plan: Plan,
    w: WorldModel,
    m: Memory,
    policy: Policy,
    blocked: set[Pos],
    costly: set[Pos],
    knowledge: KnowledgeBase | None,
) -> bool:
    """Set ``m.path`` from the plan's current op. True when the plan decided the round.

    Ops no shipped state can run, and ``travel`` to a destination with no
    path yet, are dropped and logged. An op that finds no path for
    ``PLAN_STALL_SECONDS`` is dropped too, so the stack never stalls; until
    then ``policy.goals`` get the move. A ``wait`` decides the round with no move.
    """
    while True:
        plan.advance(w)
        op = plan.current()
        if op is None:
            return False
        if op["op"] not in EXPLORE_PATH_OPS:
            plan.drop_current(f"no {OP_STATE.get(op['op']) or 'executor'} state yet")
            continue
        if op["op"] == "wait":
            return True
        if op["op"] == "travel" and op["to"] not in TRAVEL_PATHED:
            plan.drop_current(f"no path to a {op['to']} yet")
            continue
        found = path_for_plan_op(op, w, m, policy, blocked, costly, knowledge)
        if found and next_step(w, blocked, found[0]):
            plan.stalled_since_tick = None
            m.path, m.goal = found
            return True
        if plan.note_stalled(w.tick):
            plan.drop_current(f"no path for {PLAN_STALL_SECONDS}s")
            continue
        return False


def replan(
    w: WorldModel,
    m: Memory,
    policy: Policy,
    rng: random.Random,
    blocked: set[Pos],
    costly: set[Pos],
    knowledge: KnowledgeBase | None = None,
    plan: Plan | None = None,
) -> None:
    """Take the first goal whose path starts on a seen, open step.

    A path whose first step lies in fog is skipped like an unreachable goal,
    so a later goal (explore, say) gets the move while terrain reads catch up.
    When a plan is active, its current op is tried before ``policy.goals``.
    """
    m.path, m.goal = [], ""
    if plan is not None and plan_step(plan, w, m, policy, blocked, costly, knowledge):
        return
    for goal in policy.goals:
        found = plan_goal(goal, w, m, policy, rng, blocked, costly, knowledge)
        if next_step(w, blocked, found):
            m.path, m.goal = found, goal
            return


def nav_search(m: Memory, w: WorldModel, plan: str, goal: Pos) -> NavSearchState:
    """The corridor search for ``plan``, started over when its goal or map changed (A13)."""
    nav = m.corridors.get(plan)
    if nav is None or nav.goal != goal or nav.map_id != w.map_id:
        nav = m.corridors[plan] = NavSearchState(goal=goal, map_id=w.map_id)
    return nav


def grid_params(policy: Policy, avoid: set[Pos], costly: set[Pos], allow_goal_door: bool = False) -> CostGridParams:
    return CostGridParams(
        avoid=set(avoid),
        costly=set(costly),
        hostile_kinds=frozenset(policy.hostile),
        allow_goal_door=allow_goal_door,
    )


def plan_goal(
    goal: str,
    w: WorldModel,
    m: Memory,
    policy: Policy,
    rng: random.Random,
    blocked: set[Pos],
    costly: set[Pos],
    knowledge: KnowledgeBase | None = None,
) -> list[Pos] | None:
    view = w.view
    if goal == "hold":
        return None
    if goal == "wander":
        options = w.open_neighbours(w.pos, blocked)
        return [rng.choice(sorted(options))] if options else None
    if goal == "goto":
        # config.load guarantees goto is set when the goal is listed.
        dest_map = policy.goto_map if policy.goto_map is not None else w.map_id
        params = grid_params(policy, blocked, costly, allow_goal_door=True)
        target = tuple(policy.goto)
        nav = nav_search(m, w, "goto", target) if dest_map == w.map_id else None
        return route_first_leg(w, knowledge, dest_map, target, params, nav=nav) or None
    if goal == "doors":
        return doors_goal_path(w, knowledge, grid_params(policy, blocked, costly, allow_goal_door=True))
    if goal == "explore":
        targets = view.frontier() - {w.pos}
        found = nearest_target(w, targets, grid_params(policy, blocked, costly))
        return found[1] if found and found[1] else None
    return None
