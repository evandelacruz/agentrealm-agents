"""Pathing helpers the states share: flee, next step, replan (M3, A12)."""

from __future__ import annotations

import dataclasses
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
    known_prefix,
)
from .navigation import stuck as nav_stuck
from .navigation.stuck import Leg, NavAttempt
from .healing import hurt
from .plan import EXPLORE_ANYWHERE, GoalOp, explore_targets
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


# ``Memory.goal`` of the safe default's walk (no plan op, PLAN.md Architecture).
SAFE_EXPLORE_GOAL = "explore"


def plan_op_goal(op: GoalOp) -> str:
    """The ``Memory.goal`` label a path for ``op`` carries, or "" when no path serves it (A34)."""
    return "explore_area" if op["op"] == "explore_area" else ""


def path_owned_by(op: GoalOp | None, m: Memory) -> bool:
    """True when ``m.path`` was set for ``op`` (None: the safe default's walk).

    A path from before a ``goals`` reload, for another state, or for an
    earlier op of the same kind with another target must not keep driving
    movement once the stack's head is a different op (A34).
    """
    if op is None:
        return m.goal == SAFE_EXPLORE_GOAL
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
    """A cost-grid path for an ``explore_area`` op, its goal label, and what
    stuck detection tracks (A15). Frontiers given up on and still backed off
    are skipped. **Travel** walks ``travel`` ops itself (A27)."""
    if op["op"] != "explore_area":
        return None
    label = plan_op_goal(op)
    targets = nav_stuck.filter_frontiers(m.nav_stuck, w.map_id, explore_targets(op, w), w.tick)
    center = (op["x"], op["y"])
    if not targets and op["radius"] < EXPLORE_ANYWHERE and chebyshev(w.pos, center) > op["radius"]:
        targets = {center}
        if nav_stuck.backed_off(m, label, w.map_id, center, w.tick):
            return None
    found = nearest_explore_target(
        w, targets, grid_params(policy, blocked, costly, m=m, w=w, knowledge=knowledge), knowledge
    )
    return (found[1], label, Leg(found[0])) if found and found[1] else None


def replan(
    w: WorldModel,
    m: Memory,
    policy: Policy,
    blocked: set[Pos],
    costly: set[Pos],
    knowledge: KnowledgeBase | None = None,
    op: GoalOp | None = None,
) -> tuple[str, Leg, bool] | None:
    """Set ``m.path`` for the ``explore_area`` ``op``, or with no op for the
    safe default: the nearest safe frontier (``safe_explore_path``).

    A path whose first step lies in fog does not count. When there is no
    step, returns the goal, its leg on this map, and whether a route was
    found (its first step was not open), for the caller's stuck detection (A15).
    """
    m.path, m.goal, m.goal_op = [], "", None
    if op is not None:
        found = path_for_plan_op(op, w, m, policy, blocked, costly, knowledge)
        path, goal, leg = found if found else (None, plan_op_goal(op), None)
    else:
        path, leg = safe_explore_path(w, m, policy, blocked, costly, knowledge)
        goal = SAFE_EXPLORE_GOAL
    if next_step(w, blocked, path):
        _store_path(m, w, goal, path, leg)
        m.goal_op = dict(op) if op is not None else None
        return None
    if leg is not None and leg.target != w.pos:
        return goal, leg, bool(path)
    return None


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
) -> Pos | None:
    """One move toward ``target`` on this map, with stuck detection and escalation (A15).

    Keeps the current path for ``goal`` while it has an open first step, else
    plans with ``plan``. None when we stand on the target, or it is backed off
    or was just given up on, so the caller yields the round.

    A cross-map ``Leg`` is backed off, and given up, by its ultimate destination.
    """
    leg = target if isinstance(target, Leg) else Leg(target)
    backoff = leg.backoff_key or nav_stuck.goal_key(goal, w.map_id, leg.target)
    if nav_stuck.is_backed_off(m.nav_stuck, backoff, w.tick):
        if m.goal == goal:
            m.path, m.goal = [], ""
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
        found = plan(att)
        if next_step(w, avoid, found):
            return _walk(m, w, att, avoid, found)
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
    decisions (``nav_stuck.resume``).
    """
    if nav_stuck.backed_off(m, goal, w.map_id, at, w.tick):
        return None
    att = nav_stuck.attempt(m, w, goal, at)
    if att is None:
        return None
    nav_stuck.resume(m, att, w.tick)
    if not (m.goal == goal and m.path and m.path[-1] == at and next_step(w, avoid, m.path)):
        found = plan()
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


def safe_explore_path(
    w: WorldModel,
    m: Memory,
    policy: Policy,
    blocked: set[Pos],
    costly: set[Pos],
    knowledge: KnowledgeBase | None = None,
) -> tuple[list[Pos] | None, Leg | None]:
    """The safe default's walk: a path to the nearest frontier in safe ground,
    and what stuck detection tracks (None when there is none).

    Safe ground keeps off ``avoid_blocks`` hazards and Step rejections
    (``blocked``) and away from hostiles in ``hostile_range``. Hurt, it is
    first only safe-zone ground (``is_safe_ish``), so the agent heals while
    it looks around; once that is all explored (or out of reach), it pushes
    the boundary: the nearest frontier outside, still away from hazards and
    hostiles, with the survival reflexes (Retreat, Flee, Heal) above it as
    always. So the safe default never loops "look around" while unexplored
    ground is reachable. Frontiers backed off after a give-up are skipped (A15).
    """
    from .states.gather_safe import hostiles_near, is_safe_ish

    targets = nav_stuck.filter_frontiers(m.nav_stuck, w.map_id, w.view.frontier() - {w.pos}, w.tick)
    targets = {p for p in targets if w.view.tiles.get(p) not in policy.avoid_blocks}
    targets = {p for p in targets if not hostiles_near(w, p, policy)}
    params = grid_params(policy, blocked, costly, m=m, w=w, knowledge=knowledge)
    found = None
    if hurt(w):
        found = nearest_explore_target(w, {p for p in targets if is_safe_ish(w, p, policy)}, params, knowledge)
    if not (found and found[1]):
        found = nearest_explore_target(w, targets, params, knowledge)
    return (found[1], Leg(found[0])) if found and found[1] else (None, None)
