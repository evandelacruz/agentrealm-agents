"""Pathing helpers the states share: flee, next step, replan (M3, A12)."""

from __future__ import annotations

import random
from typing import Callable

from .config import Policy
from .knowledge_base import KnowledgeBase
from .memory import Memory
from .navigation import (
    CostGridParams,
    NavSearchState,
    cost_path,
    doors_goal_path,
    known_prefix,
    nearest_target,
    route_first_leg,
)
from .navigation import stuck as nav_stuck
from .navigation.stuck import NavAttempt
from .plan import (
    EXPLORE_ANYWHERE,
    EXPLORE_PATH_OPS,
    OP_STATE,
    PLAN_STALL_SECONDS,
    TRAVEL_PATHED,
    GoalOp,
    Plan,
    SOLVE_OPS,
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


def plan_op_goal(op: GoalOp) -> str:
    """The ``Memory.goal`` label a path for ``op`` carries, or "" when no path serves it (A34)."""
    if op["op"] == "explore_area":
        return "explore_area"
    if op["op"] == "travel" and op["to"] in ("point", "entrance", "town"):
        return {"point": "plan_travel", "entrance": "plan_entrance", "town": "plan_town"}[op["to"]]
    return ""


def path_owned_by_plan(plan: Plan | None, m: Memory, policy_goals: list[str] | tuple[str, ...] = ()) -> bool:
    """False when ``m.path`` was set for something other than the plan's head op.

    A path left by a ``policy.goals`` round, from before a ``goals`` reload, or
    for an earlier op of the same kind with another target must not keep
    driving movement once the stack's head is a different op (A34).

    While the head is stalled (no path yet), ``policy.goals`` have the move,
    so a path ``replan`` set from one of them is kept until it goes stale
    rather than re-rolled every window.
    """
    op = plan.current() if plan is not None else None
    if op is None:
        return True
    if m.goal == "":
        return False
    if plan.stalled_since_tick is not None and m.goal_op is None and m.goal in policy_goals:
        return True
    return m.goal == plan_op_goal(op) and m.goal_op == op


def path_for_plan_op(
    op: GoalOp,
    w: WorldModel,
    m: Memory,
    policy: Policy,
    blocked: set[Pos],
    costly: set[Pos],
    knowledge: KnowledgeBase | None,
) -> tuple[list[Pos], str, Pos | None] | None:
    """A cost-grid path for an ``explore_area`` or ``travel`` op, its goal label,
    and the target on this map stuck detection tracks (None across maps, A15).

    Targets given up on and still backed off are skipped (A15).
    """
    label = plan_op_goal(op)
    if op["op"] == "explore_area":
        targets = nav_stuck.filter_frontiers(m.nav_stuck, w.map_id, explore_targets(op, w), w.tick)
        center = (op["x"], op["y"])
        if not targets and op["radius"] < EXPLORE_ANYWHERE and chebyshev(w.pos, center) > op["radius"]:
            targets = {center}
            if nav_stuck.backed_off(m, label, w.map_id, center, w.tick):
                return None
        found = nearest_target(w, targets, grid_params(policy, blocked, costly, m=m))
        return (found[1], label, found[0]) if found and found[1] else None
    if op["op"] != "travel":
        return None
    params = grid_params(policy, blocked, costly, allow_goal_door=True, m=m)
    if op["to"] == "point":
        target = (op["x"], op["y"])
        dest_map = op.get("map_id", w.map_id)
        if nav_stuck.backed_off(m, label, dest_map, target, w.tick):
            return None
        nav = nav_search(m, w, label, target) if dest_map == w.map_id else None
        path = route_first_leg(w, knowledge, dest_map, target, params, nav=nav)
        return (path, label, target if dest_map == w.map_id else None) if path else None
    if op["to"] == "entrance":
        path = doors_goal_path(w, knowledge, params)
        if not path or nav_stuck.backed_off(m, label, w.map_id, path[-1], w.tick):
            return None
        return path, label, path[-1]
    if op["to"] == "town":
        for map_id, pos in w.respawn_anchors:
            if map_id == w.map_id:
                if nav_stuck.backed_off(m, label, map_id, pos, w.tick):
                    continue
                path = route_first_leg(w, knowledge, map_id, pos, params, nav=nav_search(m, w, label, pos))
                if path:
                    return path, label, pos
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
    ``compose`` and ``use_block`` belong to **Solve**, which drops them itself
    when they stall (A39), so they are left on the stack here.
    """
    while True:
        plan.advance(w)
        op = plan.current()
        if op is None:
            return False
        if op["op"] in SOLVE_OPS:
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
            _store_path(m, w, found[1], found[0], found[2])
            m.goal_op = dict(op)
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
) -> tuple[str, Pos, bool] | None:
    """Take the first goal whose path starts on a seen, open step.

    A path whose first step lies in fog is skipped like an unreachable goal,
    so a later goal (explore, say) gets the move while terrain reads catch up.
    When a plan is active, its current op is tried before ``policy.goals``.
    When no goal gets a step, returns the first goal with a target on this map,
    its target, and whether a route was found (its first step was not open),
    for the caller's stuck detection (A15).
    """
    m.path, m.goal, m.goal_op = [], "", None
    if plan is not None and plan_step(plan, w, m, policy, blocked, costly, knowledge):
        return None
    missed: tuple[str, Pos, bool] | None = None
    for goal in policy.goals:
        found, target = plan_goal(goal, w, m, policy, rng, blocked, costly, knowledge)
        if next_step(w, blocked, found):
            _store_path(m, w, goal, found, target)
            return None
        if missed is None and target is not None and target != w.pos:
            missed = (goal, target, bool(found))
    return missed


def nav_search(m: Memory, w: WorldModel, plan: str, goal: Pos) -> NavSearchState:
    """The corridor search for ``plan``, started over when its goal or map changed (A13)."""
    nav = m.corridors.get(plan)
    if nav is None or nav.goal != goal or nav.map_id != w.map_id:
        nav = m.corridors[plan] = NavSearchState(goal=goal, map_id=w.map_id)
    return nav


def grid_params(
    policy: Policy,
    avoid: set[Pos],
    costly: set[Pos],
    allow_goal_door: bool = False,
    m: Memory | None = None,
) -> CostGridParams:
    base = CostGridParams(
        avoid=set(avoid),
        costly=set(costly),
        hostile_kinds=frozenset(policy.hostile),
        allow_goal_door=allow_goal_door,
    )
    return nav_stuck.planning_params(m, base) if m is not None else base


def _store_path(m: Memory, w: WorldModel, goal: str, path: list[Pos], target: Pos | None) -> None:
    """Keep ``path`` for ``goal``; a target on this map becomes the active stuck attempt (A15)."""
    m.path, m.goal = path, goal
    att = nav_stuck.track(m, w, goal, target) if target is not None else None
    if att is None:
        m.nav_stuck.active = None
    else:
        nav_stuck.observe(att, w, path)


# A plan for one attempt's target, under the attempt's escalation level.
AttemptPlan = Callable[[NavAttempt], "list[Pos] | None"]


def _walk(m: Memory, w: WorldModel, att: NavAttempt, avoid: set[Pos], path: list[Pos]) -> Pos | None:
    m.path, m.goal = path, att.goal
    nav_stuck.observe(att, w, path)
    return next_step(w, avoid, path)


def _wait(m: Memory, att: NavAttempt, path: list[Pos]) -> None:
    """A route exists but its first step is taken (an occupant, A14): no move.

    The level is not failed yet; its window runs out if the way stays shut.
    """
    m.path, m.goal = path, att.goal
    return None


def escalation_step(
    m: Memory,
    w: WorldModel,
    att: NavAttempt,
    avoid: set[Pos],
    plan: AttemptPlan,
    reason: str | None,
) -> Pos | None:
    """Climb the A15 ladder from a failed window (``reason``), or carry on a reveal.

    Each level is tried before the next: a cautious replan (step 1) that finds
    a path is walked, one whose first step is taken waits out its window, and
    one that finds none fails at once and reveal (step 3) starts. Reveal moves
    until a plan shorter than any seen before appears, or gives up (step 5)
    when its budget runs out or no frontier is reachable. Returns the move, or
    None when waiting or given up.
    """
    while True:
        if att.level == nav_stuck.REVEAL:
            found = plan(att)
            if next_step(w, avoid, found) and nav_stuck.reveal_found_way(att, w, found):
                return _walk(m, w, att, avoid, found)
            step = nav_stuck.reveal_step(w, att, avoid)
            if step is None:
                spent = att.reveal_left <= 0
                nav_stuck.give_up(m, w, att, "reveal_spent" if spent else "no_frontier")
                return None
            m.path, m.goal = [], att.goal
            return step
        if not nav_stuck.escalate(m, w, att, reason or "stuck"):
            return None
        if att.level == nav_stuck.CAUTIOUS:
            found = plan(att)
            if next_step(w, avoid, found):
                return _walk(m, w, att, avoid, found)
            if found:
                return _wait(m, att, found)
            reason = "no_path"


def guided_step(
    m: Memory,
    w: WorldModel,
    goal: str,
    target: Pos,
    avoid: set[Pos],
    plan: AttemptPlan,
) -> Pos | None:
    """One move toward ``target`` on this map, with stuck detection and escalation (A15).

    Keeps the current path for ``goal`` while it has an open first step, else
    plans with ``plan``. None when we stand on the target, or it is backed off
    or was just given up on, so the caller yields the round.
    """
    if nav_stuck.backed_off(m, goal, w.map_id, target, w.tick):
        if m.goal == goal:
            m.path, m.goal = [], ""
        return None
    att = nav_stuck.track(m, w, goal, target)
    if att is None:
        return None
    if w.pos == target:
        nav_stuck.finish(m, att)
        return None
    reason = None if att.level == nav_stuck.REVEAL else nav_stuck.stuck_reason(att, w.tick)
    if reason is None and att.level != nav_stuck.REVEAL:
        if m.goal == goal and next_step(w, avoid, m.path):
            nav_stuck.observe(att, w, m.path)
            return next_step(w, avoid, m.path)
        found = plan(att)
        if next_step(w, avoid, found):
            return _walk(m, w, att, avoid, found)
        if found:
            return _wait(m, att, found)
        reason = "no_path"
    return escalation_step(m, w, att, avoid, plan, reason)


def attempt_plan(
    m: Memory,
    w: WorldModel,
    policy: Policy,
    avoid: set[Pos],
    costly: set[Pos],
) -> AttemptPlan:
    """Plan straight to an attempt's target on this map, at its escalation's fog price."""

    def plan(att: NavAttempt) -> list[Pos] | None:
        params = grid_params(policy, avoid, costly, allow_goal_door=True, m=m)
        return cost_path(w, att.target, params, nav=nav_search(m, w, att.goal, att.target))

    return plan


def plan_goal(
    goal: str,
    w: WorldModel,
    m: Memory,
    policy: Policy,
    rng: random.Random,
    blocked: set[Pos],
    costly: set[Pos],
    knowledge: KnowledgeBase | None = None,
) -> tuple[list[Pos] | None, Pos | None]:
    """A path for one ``policy.goals`` entry, and its target on this map (None
    across maps or when it has none). Targets backed off after a give-up are
    skipped (A15)."""
    view = w.view
    if goal == "hold":
        return None, None
    if goal == "wander":
        options = w.open_neighbours(w.pos, blocked)
        return ([rng.choice(sorted(options))] if options else None), None
    if goal == "goto":
        # config.load guarantees goto is set when the goal is listed.
        dest_map = policy.goto_map if policy.goto_map is not None else w.map_id
        target = tuple(policy.goto)
        if nav_stuck.backed_off(m, "goto", dest_map, target, w.tick):
            return None, None
        params = grid_params(policy, blocked, costly, allow_goal_door=True, m=m)
        nav = nav_search(m, w, "goto", target) if dest_map == w.map_id else None
        path = route_first_leg(w, knowledge, dest_map, target, params, nav=nav) or None
        return path, (target if dest_map == w.map_id else None)
    if goal == "doors":
        path = doors_goal_path(w, knowledge, grid_params(policy, blocked, costly, allow_goal_door=True, m=m))
        if not path or nav_stuck.backed_off(m, "doors", w.map_id, path[-1], w.tick):
            return None, None
        return path, path[-1]
    if goal == "explore":
        targets = nav_stuck.filter_frontiers(m.nav_stuck, w.map_id, view.frontier() - {w.pos}, w.tick)
        found = nearest_target(w, targets, grid_params(policy, blocked, costly, m=m))
        return (found[1], found[0]) if found and found[1] else (None, None)
    return None, None
