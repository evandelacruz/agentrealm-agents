"""Pathing helpers the states share: flee, next step, replan (M3, A12)."""

from __future__ import annotations

import dataclasses
import random
from typing import Callable, Collection

from .break_memory import break_costs_for_planning, nominate_on_path
from .clues import nearest_explore_target
from .config import Policy
from .knowledge_base import KnowledgeBase
from .memory import Memory
from .navigation import (
    CostGridParams,
    NavSearchState,
    alt_route_path,
    cost_path,
    doors_goal_path,
    known_prefix,
    nearest_target,
    route_first_leg,
)
from .navigation import stuck as nav_stuck
from .navigation import walk as nav_walk
from .navigation.stuck import Leg, NavAttempt
from .plan import (
    BOSS_PLAN_OPS,
    BREAK_PLAN_OPS,
    EXPLORE_ANYWHERE,
    EXPLORE_PATH_OPS,
    OP_STATE,
    PLAN_STALL_SECONDS,
    SHOP_PLAN_OPS,
    TRAVEL_PATHED,
    GoalOp,
    Plan,
    SOLVE_OPS,
    explore_targets,
)
from .travel.ops import travel_op_from_plan_goal
from .travel.resolve import at_destination, resolve_travel
from .world import DOORS, Entity, Pos, WorldModel, chebyshev


def hostiles_in_range(w: WorldModel, policy: Policy) -> list[Entity]:
    """Entities of a ``policy.hostile`` kind within ``policy.hostile_range``."""
    here = w.pos
    if here is None:
        return []
    return [e for e in w.entities if e.kind in policy.hostile and chebyshev(e.pos, here) <= policy.hostile_range]


def goto_target(w: WorldModel, policy: Policy) -> tuple[int | None, Pos] | None:
    """The ``goto`` in ``policy.goals`` as (map, cell), or None when there is none."""
    if policy.goto is None or "goto" not in policy.goals:
        return None
    dest_map = policy.goto_map if policy.goto_map is not None else w.map_id
    return dest_map, tuple(policy.goto)


def goto_key(policy: Policy) -> tuple[int | None, Pos] | None:
    """The ``goto`` in ``policy.goals`` as configured, (``goto_map``, cell), or None.

    Unlike ``goto_target`` it does not resolve an unset ``goto_map`` to the
    current map, so a door round trip never makes it look like a new target.
    """
    if policy.goto is None or "goto" not in policy.goals:
        return None
    return policy.goto_map, tuple(policy.goto)


def goto_satisfied(m: Memory, policy: Policy) -> bool:
    """The policy goto was stood on and has not changed since (A16, A58 run 8)."""
    key = goto_key(policy)
    return key is not None and m.goto_reached == key


def note_goto_reached(w: WorldModel, m: Memory, policy: Policy) -> None:
    """Remember that the agent stood on the goto target (A16, A58 run 8).

    Dispatch calls this once per decision, before any state runs. The record
    is the goto as configured (``goto_key``), so it holds across map changes,
    and only a new or changed ``policy.goto`` or ``policy.goto_map`` clears
    it: that target is owed until it is reached in turn.
    """
    key = goto_key(policy)
    if m.goto_reached is not None and m.goto_reached != key:
        m.goto_reached = None
    goto = goto_target(w, policy)
    if goto is not None and w.map_id == goto[0] and w.pos == goto[1]:
        m.goto_reached = key


def goto_navigation_pending(w: WorldModel, m: Memory, policy: Policy) -> bool:
    """True while the agent still owes the ``goto`` in ``policy.goals`` (M7 smoke, A58).

    While it is, the walk comes first: Loot, Shop, Investigate, Travel and
    OddBreak stay out, Break runs only for the goto's own stuck escalation,
    and the plan's moves and ``wait`` hold are skipped (``replan``, Explore).
    Heal is not deferred, so a hurt character still walks to safety.

    The goto is owed until the agent stands on its target. Once reached it
    is satisfied (``goto_satisfied``): stepping off or leaving the map does
    not owe it again, and Explore and the other states run as normal (A16,
    A58 run 8). A new or changed ``policy.goto`` or ``goto_map`` is owed again. An unreached goto that stuck detection gave
    up on is not owed while backed off, and is owed again when the backoff ends.
    """
    goto = goto_target(w, policy)
    if goto is None or goto_satisfied(m, policy):
        return False
    dest_map, target = goto
    if w.map_id != dest_map or w.pos is None or w.pos == target:
        return False
    if nav_stuck.backed_off(m, "goto", dest_map, target, w.tick):
        return False
    return True


# How far a committed flee run reaches past its first step when no safe tile
# is known (A9, A58). Long enough to leave two hostiles' reach, short enough
# that the run is replanned before the map around it goes stale.
FLEE_RUN_STEPS = 6


def _nearest(p: Pos, hostiles: list[Entity]) -> int:
    return min(chebyshev(p, h.pos) for h in hostiles)


def outruns(route: list[Pos], hostiles: list[Entity]) -> bool:
    """Every cell of ``route`` after its first is reached before any hostile could get there.

    ``route`` starts one step from the agent, so its cell ``i`` is ``i + 1``
    steps away; a hostile that is no further than that would get there no
    later than the agent (A9).
    """
    return all(_nearest(p, hostiles) > i + 1 for i, p in enumerate(route) if i)


def flee_step(
    w: WorldModel, hostiles: list[Entity], blocked: set[Pos], safes: Collection[Pos] = frozenset()
) -> Pos | None:
    """The best single step away from ``hostiles``, or None when standing still is best.

    Ranked by distance to the nearest hostile, then by distance to the
    nearest known safe tile when ``safes`` holds any, else by the summed
    distance to every hostile, then by the cell. Standing still is one of the
    candidates, so a cornered agent waits instead of stepping closer.
    """
    here = w.pos
    options = w.open_neighbours(here, blocked) + [here]

    def safety(p: Pos) -> tuple[int, int, Pos]:
        if safes:
            return _nearest(p, hostiles), -min(chebyshev(p, s) for s in safes), p
        return _nearest(p, hostiles), sum(chebyshev(p, h.pos) for h in hostiles), p

    best = max(options, key=safety)
    return None if best == here else best


def flee_run(w: WorldModel, hostiles: list[Entity], blocked: set[Pos], first: Pos) -> list[Pos]:
    """``first``, then a route of up to ``FLEE_RUN_STEPS`` more seen, open cells away from the hostiles.

    A breadth-first search from ``first`` that never steps back onto the
    agent's own cell and only enters cells the agent reaches before any
    hostile could (the ``outruns`` rule: each cell further from every hostile
    than the steps it takes to get there). The run ends on the cell it found
    furthest from the nearest hostile, preferring the longer run on ties.
    """
    occupied = w.occupied() | blocked
    came: dict[Pos, Pos | None] = {first: None}
    depth = {first: 1}
    frontier = [first]
    while frontier:
        nxt_frontier = []
        for cur in frontier:
            d = depth[cur] + 1
            if d > FLEE_RUN_STEPS + 1:
                continue
            for n in w.neighbours(cur):
                if n in came or n == w.pos or n in occupied or not w.view.walkable(n):
                    continue
                if _nearest(n, hostiles) <= d:
                    continue
                came[n], depth[n] = cur, d
                nxt_frontier.append(n)
        frontier = nxt_frontier

    end = max(came, key=lambda p: (_nearest(p, hostiles), depth[p], p))
    run = []
    while end is not None:
        run.append(end)
        end = came[end]
    return run[::-1]


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
    if op["op"] == "travel" and op["to"] in ("point", "entrance", "town", "shop"):
        return {
            "point": "plan_travel",
            "entrance": "plan_entrance",
            "town": "plan_town",
            "shop": "plan_shop",
        }[op["to"]]
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
) -> tuple[list[Pos], str, Leg | None] | None:
    """A cost-grid path for an ``explore_area`` or ``travel`` op, its goal label,
    and what stuck detection tracks on this map (the goal, or the door at the
    end of a cross-map first leg, A15).

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
        params = grid_params(policy, blocked, costly, m=m, w=w, knowledge=knowledge)
        found = nearest_explore_target(w, targets, params, knowledge)
        leg = Leg(found[0]) if found and found[1] else None
        path, leg = commit_explore(m, w, label, targets, leg, found[1] if leg else None, params)
        return (path, label, leg) if path else None
    if op["op"] != "travel":
        return None
    params = grid_params(
        policy, blocked, costly, allow_goal_door=True, m=m, w=w, knowledge=knowledge
    )
    if op["to"] == "point":
        target = (op["x"], op["y"])
        dest_map = op.get("map_id", w.map_id)
        if nav_stuck.backed_off(m, label, dest_map, target, w.tick):
            return None
        nav = nav_search(m, w, label, target) if dest_map == w.map_id else None
        path = route_first_leg(w, knowledge, dest_map, target, params, nav=nav)
        return (path, label, nav_stuck.leg_toward(m, w, label, dest_map, target, path)) if path else None
    if op["to"] == "entrance":
        path = doors_goal_path(w, knowledge, params)
        if not path or nav_stuck.backed_off(m, label, w.map_id, path[-1], w.tick):
            return None
        return path, label, Leg(path[-1])
    if op["to"] == "town":
        for map_id, pos in w.respawn_anchors:
            if map_id == w.map_id:
                if nav_stuck.backed_off(m, label, map_id, pos, w.tick):
                    continue
                path = route_first_leg(w, knowledge, map_id, pos, params, nav=nav_search(m, w, label, pos))
                if path:
                    return path, label, Leg(pos)
    if op["to"] == "shop":
        dest = resolve_travel(travel_op_from_plan_goal(op), w, knowledge, m.strength)
        if dest is None:
            return None
        target, dest_map = dest.pos, dest.map_id
        if nav_stuck.backed_off(m, label, dest_map, target, w.tick):
            return None
        nav = nav_search(m, w, label, target) if dest_map == w.map_id else None
        path = route_first_leg(w, knowledge, dest_map, target, params, nav=nav)
        if path:
            return path, label, nav_stuck.leg_toward(m, w, label, dest_map, target, path)
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
    A ``buy`` belongs to **Shop**, which clears the stall when it steps or takes,
    and a ``break_block`` to **Break**, which clears it when it acts on the op.
    ``compose`` and ``use_block`` belong to **Solve**, which drops them itself
    when they stall (A39), so they are left on the stack here.
    """
    while True:
        plan.advance(w, m)
        op = plan.current()
        if op is None:
            return False
        if op["op"] in SOLVE_OPS:
            return False
        if op["op"] not in EXPLORE_PATH_OPS:
            if op["op"] in BOSS_PLAN_OPS:
                return False
            if op["op"] in SHOP_PLAN_OPS:
                # Shop runs `buy`; while it has nothing in sight to take, the
                # op stalls here and is dropped like any other (A21).
                if plan.note_stalled(w.tick):
                    plan.drop_current(f"nothing to buy for {PLAN_STALL_SECONDS}s", memory=m)
                    continue
                return False
            if op["op"] in BREAK_PLAN_OPS:
                # Break runs `break_block`; while it has no tool or no way to
                # the block, the op stalls here the same way (A28, A36).
                if plan.note_stalled(w.tick):
                    plan.drop_current(f"nothing to break for {PLAN_STALL_SECONDS}s", memory=m)
                    continue
                return False
            plan.drop_current(f"no {OP_STATE.get(op['op']) or 'executor'} state yet", memory=m)
            continue
        if op["op"] == "wait":
            return True
        if op["op"] == "travel" and op["to"] not in TRAVEL_PATHED:
            plan.drop_current(f"no path to a {op['to']} yet", memory=m)
            continue
        if op["op"] == "travel" and op["to"] == "shop":
            # A known shop cell stays listed when bought out (A27), so standing
            # on the resolved cell is arrival even with nothing priced in sight.
            dest = resolve_travel(travel_op_from_plan_goal(op), w, knowledge, m.strength)
            if dest is not None and at_destination(w, dest):
                plan.finish_current("at shop cell", memory=m)
                continue
        found = path_for_plan_op(op, w, m, policy, blocked, costly, knowledge)
        if found and found[2] is not None and op["op"] != "explore_area":
            # explore_area is committed where its frontier is chosen (commit_explore).
            # The grid path_for_plan_op searched on: only ``travel`` may end on a door.
            door = op["op"] == "travel"
            params = grid_params(policy, blocked, costly, allow_goal_door=door, m=m, w=w, knowledge=knowledge)
            found = (commit_walk(m, w, found[1], found[2].target, found[0], params), *found[1:])
        if found and next_step(w, blocked, found[0]):
            plan.note_progress()
            _store_path(m, w, found[1], found[0], found[2])
            m.goal_op = dict(op)
            return True
        if plan.note_stalled(w.tick):
            plan.drop_current(f"no path for {PLAN_STALL_SECONDS}s", memory=m)
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
) -> tuple[str, Leg, bool] | None:
    """Take the first goal whose path starts on a seen, open step.

    A goal keeps the path it is walking while that stays the best way to its
    target (``commit_walk``): so a kept path whose next cell is still fog
    waits for it to be seen rather than turning round for another route.
    A path whose first step lies in fog is skipped like an unreachable goal,
    so a later goal (explore, say) gets the move while terrain reads catch up.
    An owed ``goto`` is the exception (below).
    When a plan is active, its current op is tried before ``policy.goals``.
    When no goal gets a step, returns the first goal with a target on this map,
    its leg, and whether a route was found (its first step was not open),
    for the caller's stuck detection (A15).

    While the ``goto`` is owed (A16 goto first), it is the only goal tried:
    a goto with no open step is returned as missed, so stuck detection
    escalates it and gives it up, rather than a later goal taking the move
    and leaving the goto neither reached nor given up (A58 run 7).
    """
    m.path, m.goal, m.goal_op = [], "", None
    walking_goto = goto_navigation_pending(w, m, policy)
    if plan is not None and not walking_goto and plan_step(plan, w, m, policy, blocked, costly, knowledge):
        return None
    missed: tuple[str, Leg, bool] | None = None
    for goal in ["goto"] if walking_goto else policy.goals:
        found, leg = plan_goal(goal, w, m, policy, rng, blocked, costly, knowledge)
        if leg is not None and goal != "explore":
            # explore is committed where its frontier is chosen (commit_explore).
            # The grid plan_goal searched on: only ``goto`` and ``doors`` may end on a door.
            door = goal in ("goto", "doors")
            params = grid_params(policy, blocked, costly, allow_goal_door=door, m=m, w=w, knowledge=knowledge)
            found = commit_walk(m, w, goal, leg.target, found, params)
        if next_step(w, blocked, found):
            _store_path(m, w, goal, found, leg)
            return None
        if missed is None and leg is not None and leg.target != w.pos:
            missed = (goal, leg, bool(found))
    return missed


def commit_walk(
    m: Memory,
    w: WorldModel,
    goal: str,
    target: Pos,
    found: list[Pos] | None,
    params: CostGridParams,
    *,
    any_target: bool = False,
) -> list[Pos] | None:
    """The path to walk toward ``target``: the one ``goal`` is already on, or ``found``.

    The walker commits to its path (``navigation.walk``, A15): it is kept
    until it is walked, a cell on it turns out blocked or a step on it is
    rejected, the target changes, or ``found`` is cheaper by more than
    ``walk.SWITCH_GAIN``; and never dropped for a path that steps straight
    back to the cell just left while it is still open.

    ``params`` must be the grid the planner searched ``found`` on: the kept
    path and ``found`` are both priced on it, so the comparison sees the
    same hazards, hostiles and fog price the search did.
    """
    path, walk = nav_walk.commit(m.walks.get(goal), w, goal, target, found, params, any_target=any_target)
    if walk is None:
        nav_walk.drop(m, goal)
    else:
        m.walks[goal] = walk
    return path


def commit_explore(
    m: Memory,
    w: WorldModel,
    goal: str,
    targets: set[Pos],
    leg: Leg | None,
    found: list[Pos] | None,
    params: CostGridParams,
) -> tuple[list[Pos] | None, Leg | None]:
    """``commit_walk`` for an explore walk (``explore``, ``explore_area``), and the leg it walks.

    ``targets`` are the frontier cells the planner chose ``leg`` from. The
    walk keeps heading for the frontier it was exploring
    (``walk.follow_frontier``), so a reveal that makes another frontier the
    nearest never turns it round; only a route there cheaper by more than
    ``walk.SWITCH_GAIN`` does, or the kept one being blocked. ``leg`` and
    ``found`` are the planner's choice, None when it found no frontier.
    """

    def ahead(back: Pos | None) -> tuple[Pos, list[Pos]] | None:
        return nearest_target(w, targets, dataclasses.replace(params, avoid=params.avoid | {back} - {None}))

    walk = nav_walk.follow_frontier(m.walks.get(goal), w, goal, targets, ahead)
    if walk is None:
        nav_walk.drop(m, goal)
    else:
        m.walks[goal] = walk
    path = commit_walk(m, w, goal, leg.target if leg else w.pos, found, params, any_target=True)
    kept = m.walks.get(goal)
    return path, (Leg(kept.target) if kept is not None else leg)


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
    *,
    w: WorldModel | None = None,
    knowledge: KnowledgeBase | None = None,
    break_goal: Pos | None = None,
) -> CostGridParams:
    base = CostGridParams(
        avoid=set(avoid),
        costly=set(costly),
        hostile_kinds=frozenset(policy.hostile),
        allow_goal_door=allow_goal_door,
    )
    params = nav_stuck.planning_params(m, base) if m is not None else base
    if w is None or w.pos is None:
        return params
    # Breakables are priced only at break time (A15 step 2, A28): for **Break**
    # walking to the cell it opens (``break_goal``), or for an attempt at the
    # BREAK level. Below it they stay impassable, so a plan never routes into
    # a block nothing is going to open.
    goal = break_goal
    if goal is None:
        att = nav_stuck.active(m, w) if m is not None else None
        if att is None or att.level != nav_stuck.BREAK:
            return params
        goal = att.target
    costs = break_costs_for_planning(w, knowledge, goal)
    if not costs:
        return params
    return dataclasses.replace(params, break_nominated=set(costs), break_costs=costs)


def _store_path(m: Memory, w: WorldModel, goal: str, path: list[Pos], leg: Leg | None) -> None:
    """Keep ``path`` for ``goal``; its leg on this map becomes the active stuck attempt (A15)."""
    m.path, m.goal = path, goal
    att = nav_stuck.track(m, w, goal, leg) if leg is not None else None
    if att is None:
        m.nav_stuck.active = None
    else:
        nav_stuck.observe(att, w, path)


def _walk_grid(params: Callable[[], CostGridParams] | None, avoid: set[Pos]) -> CostGridParams:
    """The grid a walk's plan searched on: ``params()``, else the default grid with ``avoid``."""
    return params() if params is not None else CostGridParams(avoid=set(avoid), allow_goal_door=True)


# A plan for one attempt's target, under the attempt's escalation level.
AttemptPlan = Callable[[NavAttempt], "list[Pos] | None"]


def _walk(m: Memory, w: WorldModel, att: NavAttempt, avoid: set[Pos], path: list[Pos]) -> Pos | None:
    """Walk an escalation level's plan: a new walk, since the old path is the one that got stuck."""
    m.path, m.goal = path, att.goal
    walk = nav_walk.start(w, att.goal, att.target, path)
    if walk is None:
        nav_walk.drop(m, att.goal)
    else:
        m.walks[att.goal] = walk
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
    knowledge: KnowledgeBase | None = None,
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
        if att.level == nav_stuck.BREAK:
            return None
        if att.level == nav_stuck.ALT_ROUTE:
            if reason:
                if not nav_stuck.escalate(m, w, att, reason):
                    return None
                reason = None
                continue
            found = plan(att)
            if next_step(w, avoid, found):
                att.level = nav_stuck.WALK
                nav_stuck.clear_break_target(att)
                return _walk(m, w, att, avoid, found)
            if found:
                return _wait(m, att, found)
            if not nav_stuck.escalate(m, w, att, "no_path"):
                return None
            continue
        if att.level == nav_stuck.REVEAL:
            found = plan(att)
            if next_step(w, avoid, found) and nav_stuck.reveal_found_way(att, w, found):
                return _walk(m, w, att, avoid, found)
            step = nav_stuck.reveal_step(w, att, avoid)
            if step is None:
                spent = att.reveal_left <= 0
                if not nav_stuck.escalate(m, w, att, "reveal_spent" if spent else "no_frontier"):
                    return None
                reason = "no_path"
                continue
            m.path, m.goal = [], att.goal
            return step
        if not nav_stuck.escalate(m, w, att, reason or "stuck"):
            return None
        if att.level == nav_stuck.BREAK:
            choice = nominate_on_path(w, knowledge, w.pos, att.target) if w.pos else None
            if choice is not None:
                att.break_x, att.break_y, att.break_cap = choice.pos[0], choice.pos[1], choice.capability
            else:
                if not nav_stuck.escalate(m, w, att, "no_break"):
                    return None
                continue
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
    target: Pos | Leg,
    avoid: set[Pos],
    plan: AttemptPlan,
    knowledge: KnowledgeBase | None = None,
    *,
    params: Callable[[], CostGridParams] | None = None,
) -> Pos | None:
    """One move toward ``target`` on this map, with stuck detection and escalation (A15).

    Keeps the current path for ``goal`` while it has an open first step, else
    plans with ``plan``. None when we stand on the target, or it is backed off
    or was just given up on, so the caller yields the round.

    A cross-map ``Leg`` is backed off, and given up, by its ultimate destination.

    ``params`` builds the grid ``plan`` searches on, so the walk prices its
    kept path on the same one (``commit_walk``); None for a plan on the
    default grid with ``avoid`` (tests).
    """
    leg = target if isinstance(target, Leg) else Leg(target)
    backoff = leg.backoff_key or nav_stuck.goal_key(goal, w.map_id, leg.target)
    if nav_stuck.is_backed_off(m.nav_stuck, backoff, w.tick):
        if m.goal == goal:
            m.path, m.goal = [], ""
        nav_walk.drop(m, goal, leg.target)
        return None
    att = nav_stuck.track(m, w, goal, leg)
    if att is None:
        return None
    if w.pos == leg.target:
        nav_stuck.finish(m, att)
        return None
    reason = None if att.level in (nav_stuck.REVEAL, nav_stuck.BREAK) else nav_stuck.stuck_reason(att, w.tick)
    if reason is None and att.level not in (nav_stuck.REVEAL, nav_stuck.BREAK):
        if m.goal == goal and next_step(w, avoid, m.path):
            nav_stuck.observe(att, w, m.path)
            return next_step(w, avoid, m.path)
        found = commit_walk(m, w, goal, leg.target, plan(att), _walk_grid(params, avoid))
        if next_step(w, avoid, found):
            m.path, m.goal = found, goal
            nav_stuck.observe(att, w, found)
            return next_step(w, avoid, found)
        if found:
            return _wait(m, att, found)
        reason = "no_path"
    return escalation_step(m, w, att, avoid, plan, reason, knowledge)


def bounded_step(
    m: Memory,
    w: WorldModel,
    goal: str,
    at: Pos,
    avoid: set[Pos],
    plan: Callable[[], "list[Pos] | None"],
    *,
    params: Callable[[], CostGridParams] | None = None,
) -> Pos | None:
    """One move toward ``at`` on a walk that gives up instead of escalating (A15).

    For the short walks to something in sight: Heal to food or a safe tile
    (A10), Loot to a pickup (A20). Each is a stuck attempt like any walk, so
    the oscillation guard can give it up too, but it skips the escalation
    ladder: nothing in sight is worth a block broken or a reveal walk. No
    path, or a window with no progress (moves, time or pacing), gives ``at``
    up through step 5 with its backoff, and a target still backed off is
    skipped. A route whose first step is taken or unseen waits, as in A15,
    and is given up once it has waited ``PROGRESS_TICK_LIMIT`` with no step
    in between; a window left over from walking earlier is never judged
    there. The path for ``goal`` is kept while it still ends on ``at`` with
    an open first step, else planned again with ``plan``.

    None when there is no move now: the caller tries something else. Only a
    walk that moves makes its attempt active, so one with no move never
    restarts the window of the walk that does (``nav_stuck.track``). The
    window and the wait hold only while the walk is pursued on consecutive
    decisions (``nav_stuck.resume``). ``params`` is the grid ``plan``
    searches on, as in ``guided_step``.
    """
    if nav_stuck.backed_off(m, goal, w.map_id, at, w.tick):
        return None
    att = nav_stuck.attempt(m, w, goal, at)
    if att is None:
        return None
    nav_stuck.resume(m, att, w.tick)
    if not (m.goal == goal and m.path and m.path[-1] == at and next_step(w, avoid, m.path)):
        found = commit_walk(m, w, goal, at, plan(), _walk_grid(params, avoid))
        if not found or not next_step(w, avoid, found):
            if att.waiting_since is None:
                att.waiting_since = w.tick
            if not found:
                nav_stuck.give_up(m, w, att, "no_path")
            elif w.tick - att.waiting_since >= nav_stuck.PROGRESS_TICK_LIMIT:
                nav_stuck.give_up(m, w, att, "time")
            return None
        m.path, m.goal = found, goal
    nav_stuck.track(m, w, goal, at)
    att.waiting_since = None
    nav_stuck.observe(att, w, m.path)
    if reason := nav_stuck.stuck_reason(att, w.tick):
        nav_stuck.give_up(m, w, att, reason)
        return None
    return next_step(w, avoid, m.path)


def attempt_plan(
    m: Memory,
    w: WorldModel,
    policy: Policy,
    avoid: set[Pos],
    costly: set[Pos],
    knowledge: KnowledgeBase | None = None,
) -> AttemptPlan:
    """Plan straight to an attempt's target on this map, at its escalation's fog price."""

    def plan(att: NavAttempt) -> list[Pos] | None:
        params = grid_params(
            policy,
            avoid,
            costly,
            allow_goal_door=True,
            m=m,
            w=w,
            knowledge=knowledge,
        )
        if att.level == nav_stuck.ALT_ROUTE:
            params = dataclasses.replace(params, break_costs={}, break_nominated=set())
            dest_map = att.map_id if att.map_id is not None else w.map_id
            if dest_map is None:
                return None
            return alt_route_path(w, knowledge, dest_map, att.target, params)
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
) -> tuple[list[Pos] | None, Leg | None]:
    """A path for one ``policy.goals`` entry, and what stuck detection tracks
    on this map (None when it has nothing). Targets backed off after a give-up
    are skipped (A15)."""
    view = w.view
    if goal == "hold":
        return None, None
    if goal == "wander":
        options = w.open_neighbours(w.pos, blocked)
        return ([rng.choice(sorted(options))] if options else None), None
    if goal == "goto":
        # config.load guarantees goto is set when the goal is listed.
        dest_map, target = goto_target(w, policy)
        if goto_satisfied(m, policy):  # reached: never walked back to (A16)
            return None, None
        if nav_stuck.backed_off(m, "goto", dest_map, target, w.tick):
            return None, None
        params = grid_params(policy, blocked, costly, allow_goal_door=True, m=m, w=w, knowledge=knowledge)
        nav = nav_search(m, w, "goto", target) if dest_map == w.map_id else None
        path = route_first_leg(w, knowledge, dest_map, target, params, nav=nav) or None
        return path, nav_stuck.leg_toward(m, w, "goto", dest_map, target, path)
    if goal == "doors":
        path = doors_goal_path(
            w,
            knowledge,
            grid_params(policy, blocked, costly, allow_goal_door=True, m=m, w=w, knowledge=knowledge),
        )
        if not path or nav_stuck.backed_off(m, "doors", w.map_id, path[-1], w.tick):
            return None, None
        return path, Leg(path[-1])
    if goal == "explore":
        targets = nav_stuck.filter_frontiers(m.nav_stuck, w.map_id, view.frontier() - {w.pos}, w.tick)
        params = grid_params(policy, blocked, costly, m=m, w=w, knowledge=knowledge)
        found = nearest_explore_target(w, targets, params, knowledge)
        leg = Leg(found[0]) if found and found[1] else None
        path, leg = commit_explore(m, w, "explore", targets, leg, found[1] if leg else None, params)
        return (path, leg) if path else (None, None)
    return None, None
