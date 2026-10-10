"""Pathing helpers the states share: flee, next step, replan (M3, A12)."""

from __future__ import annotations

import dataclasses
from typing import Callable, Collection

from .break_memory import break_costs_for_planning, nominate_on_path
from .clues import direction_hint, nearest_explore_target, on_hint_side
from .config import Policy
from .knowledge_base import KnowledgeBase
from .memory import SAFE_EXPLORE_GOAL, Memory
from .navigation import (
    CostGridParams,
    NavSearchState,
    alt_route_path,
    cost_path,
    known_prefix,
    nearest_target,
    no_way,
)
from .navigation import stuck as nav_stuck
from .navigation import walk as nav_walk
from .navigation.stuck import Leg, NavAttempt
from .healing import hurt
from . import targets as targets_mod
from .plan import EXPLORE_ANYWHERE, GoalOp, explore_targets
from .survival import is_hostile, pursuer_peaks, reach_by_hostile, safe_goals, town_cell
from .executor.movement import step_landing
from .world import DOORS, Entity, Pos, WorldModel, chebyshev


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
    occupied = w.occupied() | w.for_sale() | blocked
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
    return w.view.walkable(p) and p not in w.occupied() and p not in w.for_sale()


def remaining_walk_cells(w: WorldModel, m: Memory) -> list[Pos]:
    """Tiles the held walk queue still steps onto, from tracked position.

    Only the queue on the server counts: the plan past its horizon is
    replanned when the queue runs out anyway.
    """
    if w.pos is None or m.pending_intents is None:
        return []
    pos = w.pos
    cells: list[Pos] = []
    for i in range(m.pending_next_index, len(m.pending_intents)):
        intent = m.pending_intents[i]
        if intent.get("verb") != "Step":
            continue
        pos = step_landing(pos, intent["direction"])
        cells.append(pos)
    return cells


def route_ahead(w: WorldModel, m: Memory) -> list[Pos]:
    """Every cell the walk under way still steps onto, from where we stand:
    the held queue's Steps (``remaining_walk_cells``), then ``m.path``.

    The executor cuts the cells it queues off ``m.path``, so ``m.path``
    alone starts up to a queue ahead of us; anything priced by steps from
    here reads this instead (Detour, free-play run 4).
    """
    return remaining_walk_cells(w, m) + list(m.path)


def next_step(w: WorldModel, blocked: set[Pos], path: list[Pos] | None) -> Pos | None:
    """The path's first step when it is seen and open, else None."""
    prefix = known_prefix(path or [], w.view)
    if prefix and step_open(w, blocked, prefix[0]):
        return prefix[0]
    return None


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
    targets = explore_area_targets(op, w, m)
    center = (op["x"], op["y"])
    if targets == {center} and nav_stuck.backed_off(m, label, w.map_id, center, w.tick):
        return None
    params = grid_params(policy, blocked, costly, m=m, w=w, knowledge=knowledge)
    found = nearest_explore_target(w, targets, params, knowledge)
    leg = Leg(found[0]) if found and found[1] else None
    path, leg = commit_explore(m, w, label, targets, leg, found[1] if leg else None, params, knowledge, op=op)
    return (path, label, leg) if path else None


def explore_area_targets(op: GoalOp, w: WorldModel, m: Memory) -> set[Pos]:
    """The cells an ``explore_area`` op may walk to: its frontier not backed off,
    else its centre while we stand outside the area."""
    targets = nav_stuck.filter_frontiers(m.nav_stuck, w.map_id, explore_targets(op, w), w.tick)
    center = (op["x"], op["y"])
    if not targets and op["radius"] < EXPLORE_ANYWHERE and chebyshev(w.pos, center) > op["radius"]:
        return {center}
    return targets


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

    Either walk keeps the frontier it is heading for (``commit_explore``),
    so a reveal never turns it round. A path whose first step lies in fog
    does not count. When there is no
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


def commit_walk(
    m: Memory,
    w: WorldModel,
    goal: str,
    target: Pos,
    found: list[Pos] | None,
    params: CostGridParams,
) -> list[Pos] | None:
    """The path to walk toward ``target``: the one ``goal`` is already on, or ``found``.

    The walker commits to its path (``navigation.walk``, A15): it is kept
    until it is walked, a cell on it turns out blocked or a step on it is
    rejected, the target changes, or ``found`` (to the same target) is
    cheaper by more than ``walk.SWITCH_GAIN``; and never dropped for a path
    that steps straight back to the cell just left while it is still open.

    ``params`` must be the grid the planner searched ``found`` on: the kept
    path and ``found`` are both priced on it, so the comparison sees the
    same hazards, hostiles and fog price the search did.
    """
    path, walk = nav_walk.commit(m.walks.get(goal), w, goal, target, found, params)
    if walk is None:
        nav_walk.drop(m, goal)
    else:
        m.walks[goal] = walk
    return path


def clue_redirects(
    w: WorldModel,
    m: Memory,
    policy: Policy,
    blocked: set[Pos],
    costly: set[Pos],
    knowledge: KnowledgeBase | None,
    op: GoalOp | None = None,
) -> bool:
    """A direction clue (A32) names a side the explore walk could go to, and the walk heads elsewhere.

    The walk is the ``explore_area`` ``op``'s, or with no op the safe
    default's. Its targets are the planner's own (``explore_area_targets``,
    ``safe_explore_targets``): only when one on the clue's side is reachable
    is the walk dropped, with its path when it is the current one, so the
    next plan takes that side (``nearest_explore_target``). A clue is a
    target change. A side the planner could not pick never drops the walk,
    so it is not remade every decision while the clue lasts. Explore calls
    this each decision before it keeps a path. True when it dropped the walk.
    """
    goal = plan_op_goal(op) if op is not None else SAFE_EXPLORE_GOAL
    hint = direction_hint(w, knowledge)
    walk = m.walks.get(goal)
    if hint is None or walk is None or on_hint_side(hint, walk.target):
        return False
    targets = explore_area_targets(op, w, m) if op is not None else safe_explore_targets(w, m, policy)
    side = {t for t in targets if on_hint_side(hint, t)}
    params = grid_params(policy, blocked, costly, m=m, w=w, knowledge=knowledge)
    if not side or nearest_target(w, side, params) is None:
        return False
    nav_walk.drop(m, goal)
    targets_mod.release(m, goal)  # a clue is new information: the next pick takes its side
    if m.goal == goal:
        m.path, m.goal = [], ""
    return True


def commit_explore(
    m: Memory,
    w: WorldModel,
    goal: str,
    targets: set[Pos],
    leg: Leg | None,
    found: list[Pos] | None,
    params: CostGridParams,
    knowledge: KnowledgeBase | None = None,
    *,
    op: GoalOp | None = None,
) -> tuple[list[Pos] | None, Leg | None]:
    """``commit_walk`` for an explore walk (``explore``, ``explore_area``), and the leg it walks.

    ``targets`` are the frontier cells the planner chose ``leg`` from;
    ``leg`` and ``found`` are its choice, None when it found no frontier.
    The frontier the walk heads for is committed (``targets``, A71): it is
    kept while it is still a frontier cell, or still unseen and not backed
    off, even when another frontier becomes nearer or another state walked
    the character off the path; a path to it is planned again then. A reveal
    that takes the cell off the frontier is reaching it: the walk re-aims at
    the nearest frontier ahead (``walk.follow_frontier``) and commits to
    that. Only a target proven unreachable (``no_way``) gives way to the
    planner's pick.

    A direction clue (A32) is new information: Explore calls
    ``clue_redirects`` first each decision, which drops a walk and its
    target heading off the clue's side; the next pick prefers that side
    (``nearest_explore_target``).
    """
    def ahead(back: Pos | None) -> tuple[Pos, list[Pos]] | None:
        found = nearest_explore_target(
            w, targets, dataclasses.replace(params, avoid=params.avoid | {back} - {None}), knowledge
        )
        # Only a way on that starts on a seen, open step; one through fog is no way on yet.
        return found if found and next_step(w, params.avoid, found[1]) else None

    walk = nav_walk.follow_frontier(m.walks.get(goal), w, goal, targets, ahead)
    if walk is None:
        nav_walk.drop(m, goal)
    else:
        m.walks[goal] = walk
        targets_mod.commit(m, w, goal, (w.map_id, walk.target), op)
    here_map = w.map_id

    def keep(t: tuple[int | None, Pos]) -> bool:
        mid, p = t
        if mid != here_map:
            return False
        return p in targets or (p not in w.view.tiles and not nav_stuck.backed_off(m, goal, mid, p, w.tick))

    held = targets_mod.hold(m, w, goal, lambda: (here_map, leg.target) if leg else None, keep, op)
    if held is None:
        return None, None
    target = held[1]
    if leg is None or leg.target != target:
        path = commit_walk(m, w, goal, target, None, params)
        if not path:
            again = nearest_target(w, {target}, params)
            path = commit_walk(m, w, goal, target, again[1], params) if again and again[1] else None
        if path:
            targets_mod.reached_way(m, goal)
            return path, Leg(target)
        # No path this decision: stuck detection escalates it (A15), up to
        # ``MISS_LIMIT`` decisions in a row; walled in, it goes at once.
        if not no_way(w, target, params) and not targets_mod.missed(m, goal):
            return None, Leg(target)
        targets_mod.release(m, goal)
        if leg is None:
            return None, None
        targets_mod.commit(m, w, goal, (here_map, leg.target), op)
    return commit_walk(m, w, goal, leg.target, found, params), leg


# Safe cells path-checked per decision, nearest first, before the town cell
# is tried as the fallback (free-play run 2: Park, Retreat and Heal aimed at
# safe tiles walled off by bush and wall, and Park timed out).
SAFE_GOAL_CHECKS = 4
# A safe cell a path check found no way to is skipped this long, then tried again.
SAFE_UNREACHABLE_TICKS = nav_stuck.BACKOFF_BASE_TICKS
# A safe cell seen in a known hostile's reach is skipped this long, so the
# pick does not turn back for it the moment the pack drops out of view.
SAFE_THREATENED_TICKS = nav_stuck.BACKOFF_BASE_TICKS
# Heal's walks to safe ground: a cell either gave up on is skipped by every safe walk.
SAFE_WALK_GOALS = ("heal_rest", "heal_measure")
# Paths to safe ground: one of these that ends on a cell needs no search to it.
SAFE_PATH_GOALS = ("safe", *SAFE_WALK_GOALS)
# The commitments of the walks to safe ground (A71): Retreat's, which Park
# shares (both walk ``retreat_step``), and Heal's rest and measure walks.
# Retreat runs from a hostile and Heal does not, so each picks by its own
# rules and keeps its own cell.
RETREAT_TARGET = "safe"
HEAL_TARGET = "heal_safe"


def committed_safe(m: Memory, w: WorldModel, key: str) -> Pos | None:
    """The safe cell ``key``'s walks committed to on this map, or None (A71).

    Standing on it is reaching it: the commitment is released.
    """
    c = targets_mod.committed(m, w, key)
    if c is None or c.target[0] != w.map_id:
        return None
    if c.target[1] == w.pos:
        targets_mod.release(m, key)
        return None
    return c.target[1]


def commit_safe(m: Memory, w: WorldModel, key: str, cell: Pos | None) -> None:
    """Commit ``key``'s walks to ``cell``; None, or the cell we stand on, releases them."""
    if cell is None or cell == w.pos:
        targets_mod.release(m, key)
    else:
        targets_mod.commit(m, w, key, (w.map_id, cell))


def reachable_safe_goal(
    m: Memory,
    w: WorldModel,
    candidates: list[Pos],
    params: CostGridParams,
    town: Pos | None,
    reach: dict[tuple[str, int], set[Pos]] | None = None,
    skip: Collection[tuple[str, int]] = (),
    prefer: Pos | None = None,
) -> Pos | None:
    """The first of ``candidates`` (in the caller's order) a path reaches,
    else ``town`` when a path reaches it, else None. The cell we stand on is
    taken as is.

    ``prefer`` is the cell the walk committed to (``committed_safe``, A71): it
    is kept while it is still a candidate or ``town`` and none of the skips
    below rules it out, however near the others have come.

    Skipped: a cell in the ``reach`` of a known hostile not in ``skip``
    (``survival.reach_by_hostile``; A63 run 4: Heal walked to a safe tile
    beside a hostile pack and died), or seen there within
    ``SAFE_THREATENED_TICKS`` (``Memory.safe_threatened``, kept per hostile so
    a caller's ``skip`` also covers the marks), since ``w.entities`` holds only
    what is in view and a pick that forgot the pack once it left view would
    turn back toward it; ``town`` is still the last resort. Also a
    cell ``params`` avoids, one a check found no way to within
    ``SAFE_UNREACHABLE_TICKS`` (``Memory.safe_unreachable``), and one a
    Heal walk gave up on and still backs off. The first ``SAFE_GOAL_CHECKS``
    left are searched, then the nearest of the rest; a cell a kept safe
    walk (``SAFE_PATH_GOALS``) already ends on needs no search. "No way" is a
    search that proved it (``navigation.no_way``): one cut short by its
    budget still counts as a way, since it walks toward the cell. A Retreat
    or Park walk also marks a cell its search proved no path to, or one it
    found no step toward for a stuck window (``retreat_step``), the town
    cell too.
    """
    if w.pos is None or w.map_id is None:
        return None
    if w.pos in candidates:
        return w.pos
    mid = w.map_id

    for key, cells in (reach or {}).items():
        for p in cells.intersection(candidates):
            m.safe_threatened.setdefault((mid, p), {})[key] = w.tick

    def skipped(p: Pos) -> bool:
        return p in params.avoid or safe_ruled_out(m, w, p)

    def reaches(p: Pos) -> bool:
        if m.goal in SAFE_PATH_GOALS and m.path and m.path[-1] == p:
            return True
        if no_way(w, p, params):
            m.safe_unreachable[(mid, p)] = w.tick
            return False
        return True

    def recently_threatened(p: Pos) -> bool:
        marks = m.safe_threatened.get((mid, p))
        if not marks:
            return False
        live = {key: seen for key, seen in marks.items() if w.tick - seen < SAFE_THREATENED_TICKS}
        if live:
            m.safe_threatened[(mid, p)] = live
        else:
            del m.safe_threatened[(mid, p)]  # lapsed marks are dropped, so a long run does not pile them up
        return any(key not in skip for key in live)

    if (
        prefer is not None
        and (prefer in candidates or prefer == town)
        and not recently_threatened(prefer)
        and not skipped(prefer)
        and reaches(prefer)
    ):
        return prefer
    left = [p for p in candidates if not recently_threatened(p) and not skipped(p)]
    here = w.pos
    first = left[:SAFE_GOAL_CHECKS]
    rest = left[SAFE_GOAL_CHECKS:]
    if rest:
        first.append(min(rest, key=lambda p: (chebyshev(p, here), p)))
    for p in first:
        if reaches(p):
            return p
    if town is not None and town not in first and not skipped(town) and reaches(town):
        return town
    return None


def safe_ruled_out(m: Memory, w: WorldModel, p: Pos) -> bool:
    """A check or a walk found no way to safe cell ``p`` within
    ``SAFE_UNREACHABLE_TICKS`` (``Memory.safe_unreachable``), or a Heal walk
    gave up on it and still backs off."""
    seen = m.safe_unreachable.get((w.map_id, p))
    if seen is not None and w.tick - seen < SAFE_UNREACHABLE_TICKS:
        return True
    return any(nav_stuck.backed_off(m, goal, w.map_id, p, w.tick) for goal in SAFE_WALK_GOALS)


def retreat_safe_goal(
    m: Memory, w: WorldModel, policy: Policy, knowledge: KnowledgeBase | None, avoid: set[Pos], costly: set[Pos]
) -> Pos | None:
    """Where Retreat, Park and Fight's retreat tail walk: the nearest known safe
    cell a path reaches and out of reach of a hostile it is not running from,
    else the town cell (``reachable_safe_goal``). Once picked, the cell is
    committed (``RETREAT_TARGET``, A71) and kept while it stays valid.

    The hostiles it runs from (``pursuer_peaks``) do not rule a cell out:
    they follow anyway, and are usually right beside the nearest safe cell.
    """
    params = grid_params(policy, avoid, costly)
    reach = reach_by_hostile(w, policy)
    goal = reachable_safe_goal(
        m,
        w,
        safe_goals(w, knowledge),
        params,
        town_cell(w, knowledge),
        reach,
        skip=pursuer_peaks(w, policy),
        prefer=committed_safe(m, w, RETREAT_TARGET),
    )
    commit_safe(m, w, RETREAT_TARGET, goal)
    return goal


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
        is_hostile=lambda w, e: is_hostile(w, policy, e),
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
                nav_stuck.walk_again(att, w, nav_stuck.ALT_ROUTE)
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


def safe_explore_targets(w: WorldModel, m: Memory, policy: Policy, *, push: bool = False) -> set[Pos]:
    """The frontier cells the safe default may walk to (``safe_explore_path``).

    ``push`` drops the hurt rule (safe-zone ground only): the frontier just
    outside safe ground, still off hazards and away from hostiles.
    """
    from .states.gather_safe import hostiles_near, is_safe_ish

    targets = nav_stuck.filter_frontiers(m.nav_stuck, w.map_id, w.view.frontier() - {w.pos}, w.tick)
    targets = {p for p in targets if w.view.tiles.get(p) not in policy.avoid_blocks}
    if hurt(w) and not push:
        return {p for p in targets if is_safe_ish(w, p, policy)}
    return {p for p in targets if not hostiles_near(w, p, policy)}


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
    targets = safe_explore_targets(w, m, policy)
    params = grid_params(policy, blocked, costly, m=m, w=w, knowledge=knowledge)
    found = nearest_explore_target(w, targets, params, knowledge)
    if not (found and found[1]) and hurt(w):
        # Hurt-safe ground all explored or out of reach: push the boundary.
        targets = safe_explore_targets(w, m, policy, push=True)
        found = nearest_explore_target(w, targets, params, knowledge)
    leg = Leg(found[0]) if found and found[1] else None
    path, leg = commit_explore(m, w, SAFE_EXPLORE_GOAL, targets, leg, found[1] if leg else None, params, knowledge)
    return (path, leg) if path else (None, None)
