"""A* over the M7 cost grid (A12) with two-level search (A13).

See docs/PLAYABLE_AGENT_PLAN.md Navigation and getting unstuck.
"""

from __future__ import annotations

import heapq
from collections.abc import Callable
from dataclasses import dataclass, field

from ..world import DOORS, NEIGHBOURS, VOID, WALKABLE, Entity, MapView, Pos, WorldModel, chebyshev

# Base step costs (PLAYABLE_AGENT_PLAN Navigation §1). Every step costs at
# least KNOWN_WALKABLE, so Chebyshev distance * KNOWN_WALKABLE is a lower
# bound on path cost.
KNOWN_WALKABLE = 1
FOG = 2
# High but finite: route around when a step is possible (occupants move).
OCCUPANT = 50
# Added at distance 0; falls off 5 per block of Chebyshev separation and
# stops at HOSTILE_DANGER_RADIUS.
HOSTILE_DANGER = 30
HOSTILE_DANGER_RADIUS = 6
# Extra cost of a step onto a `costly` tile, or onto fire/lava whose
# occupy_damage no read has named: worth a long detour to avoid one.
COSTLY_STEP = 100
HAZARDS = ("fire", "lava")

# Cache-tile size (docs/GAME_NOTES.md, API Reads).
MACRO_SIZE = 16
# Node budgets per replan window so search never stalls a tick (A13).
COARSE_NODE_BUDGET = 48
FINE_NODE_BUDGET = 400


@dataclass
class CostGridParams:
    """Per-search knobs for the cost grid."""

    avoid: set[Pos] = field(default_factory=set)  # impassable (rejected tiles, etc.)
    costly: set[Pos] = field(default_factory=set)  # passable at COSTLY_STEP extra (escape off hazards)
    break_nominated: set[Pos] = field(default_factory=set)  # inert until M9: impassable
    hostile_kinds: frozenset[str] = frozenset({"npc"})
    allow_goal_door: bool = False


@dataclass
class NavSearchState:
    """Resume data for an in-progress two-level search (A13)."""

    goal: Pos
    coarse_frontier: list[tuple[int, int, Pos]] = field(default_factory=list)
    coarse_came: dict[Pos, Pos] = field(default_factory=dict)
    coarse_cost: dict[Pos, int] = field(default_factory=dict)
    coarse_done: bool = False
    macro_path: list[Pos] | None = None


def macro_cell(p: Pos) -> Pos:
    return p[0] // MACRO_SIZE, p[1] // MACRO_SIZE


def macro_center(m: Pos) -> Pos:
    return m[0] * MACRO_SIZE + MACRO_SIZE // 2, m[1] * MACRO_SIZE + MACRO_SIZE // 2


def _macro_region(m: Pos) -> tuple[int, int, int, int]:
    mx, my = m
    return mx * MACRO_SIZE, my * MACRO_SIZE, mx * MACRO_SIZE + MACRO_SIZE, my * MACRO_SIZE + MACRO_SIZE


def known_prefix(path: list[Pos], view: MapView) -> list[Pos]:
    """The leading part of ``path`` on walkable or door tiles we have seen."""
    out: list[Pos] = []
    for p in path:
        block = view.tiles.get(p)
        if block is None or block == VOID or not (block in WALKABLE or block in DOORS):
            break
        out.append(p)
    return out


class _Grid:
    """One search's view of the cost grid, with per-search state precomputed."""

    def __init__(self, w: WorldModel, goal: Pos, params: CostGridParams):
        self.w, self.goal, self.params = w, goal, params
        self.occupied = w.occupied()
        self.hostiles: list[Entity] = [e for e in w.entities if e.kind in params.hostile_kinds]
        # Fog is unbounded, so the search is boxed to the known extent plus
        # start and goal, with a one-tile fog ring: any detour beyond the box
        # crosses only fog and is no cheaper than walking the ring.
        xs = [p[0] for p in w.view.tiles] + [w.pos[0], goal[0]]
        ys = [p[1] for p in w.view.tiles] + [w.pos[1], goal[1]]
        self.box = (min(xs) - 1, min(ys) - 1, max(xs) + 1, max(ys) + 1)

    def in_box(self, p: Pos) -> bool:
        x0, y0, x1, y1 = self.box
        return x0 <= p[0] <= x1 and y0 <= p[1] <= y1

    def cost(self, p: Pos) -> int | None:
        """Movement cost onto ``p``, or ``None`` when impassable."""
        params = self.params
        if p in params.avoid or p in params.break_nominated:
            return None
        block = self.w.view.tiles.get(p)
        if block is not None and block in DOORS:
            # Stepping onto a door warps, so a door is only ever the goal.
            return KNOWN_WALKABLE if p == self.goal and params.allow_goal_door else None
        if block is None:
            base = FOG
        elif block in WALKABLE:
            base = KNOWN_WALKABLE
        else:
            return None  # void, blocked, or an unknown type
        if block in HAZARDS:
            dmg = self.w.view.occupy_damage(p)
            base += COSTLY_STEP if dmg is None else dmg
        if p in params.costly:
            base += COSTLY_STEP
        if p in self.occupied:
            base += OCCUPANT
        for h in self.hostiles:
            d = chebyshev(p, h.pos)
            if d < HOSTILE_DANGER_RADIUS:
                base += max(0, HOSTILE_DANGER - d * 5)
        return base


def _macro_step_cost(w: WorldModel, m: Pos, params: CostGridParams) -> int | None:
    """Cost to cross a 16×16 cache tile at macro granularity."""
    x0, y0, x1, y1 = _macro_region(m)
    known_walk = known_block = unknown = 0
    for x in range(x0, x1):
        for y in range(y0, y1):
            block = w.view.tiles.get((x, y))
            if block is None:
                unknown += 1
            elif block in WALKABLE or block in DOORS:
                known_walk += 1
            else:
                known_block += 1
    if known_walk == 0 and unknown == 0:
        return None
    if known_walk == 0 and unknown > 0:
        step = FOG
    else:
        total = known_walk + known_block + unknown
        step = (known_walk * KNOWN_WALKABLE + known_block * (KNOWN_WALKABLE + COSTLY_STEP) + unknown * FOG) // total
        step = max(KNOWN_WALKABLE, min(step, FOG + 1))
    return MACRO_SIZE * step


def _reconstruct(came: dict[Pos, Pos], start: Pos, goal: Pos) -> list[Pos]:
    out = [goal]
    while out[-1] in came and came[out[-1]] != start:
        out.append(came[out[-1]])
    return out[::-1]


@dataclass
class _SearchResult:
    path: list[Pos] | None
    cost: int
    budget_hit: bool


def _search(
    w: WorldModel,
    goal: Pos,
    params: CostGridParams,
    *,
    max_nodes: int = 10**9,
    in_bounds: set[Pos] | None = None,
    bounds_fn: Callable[[Pos], bool] | None = None,
) -> _SearchResult:
    assert w.pos is not None
    start = w.pos
    if start == goal:
        return _SearchResult([], 0, False)
    grid = _Grid(w, goal, params)
    if grid.cost(goal) is None:
        return _SearchResult(None, 0, False)
    frontier: list[tuple[int, int, Pos]] = [(chebyshev(start, goal) * KNOWN_WALKABLE, 0, start)]
    came: dict[Pos, Pos] = {}
    cost: dict[Pos, int] = {start: 0}
    expanded = 0
    while frontier and expanded < max_nodes:
        _, g, cur = heapq.heappop(frontier)
        if cur == goal:
            return _SearchResult(_reconstruct(came, start, goal), g, False)
        if g > cost.get(cur, 10**9):
            continue
        expanded += 1
        for dx, dy in NEIGHBOURS:
            n = (cur[0] + dx, cur[1] + dy)
            if bounds_fn is not None and not bounds_fn(n):
                continue
            if in_bounds is not None and n not in in_bounds:
                continue
            if not grid.in_box(n):
                continue
            sc = grid.cost(n)
            if sc is None:
                continue
            ng = g + sc
            if ng < cost.get(n, 10**9):
                cost[n] = ng
                came[n] = cur
                heapq.heappush(frontier, (ng + chebyshev(n, goal) * KNOWN_WALKABLE, ng, n))
    return _SearchResult(None, 0, bool(frontier))


def _coarse_search(
    w: WorldModel,
    goal: Pos,
    params: CostGridParams,
    state: NavSearchState | None,
    max_nodes: int,
) -> list[Pos] | None:
    """Macro-cell corridor from ``w.pos`` to ``goal``; may resume ``state``."""
    assert w.pos is not None
    start_m = macro_cell(w.pos)
    goal_m = macro_cell(goal)
    if start_m == goal_m:
        return [start_m]

    if state is not None and state.coarse_done and state.macro_path is not None:
        return state.macro_path

    if state is not None and state.coarse_frontier:
        frontier = list(state.coarse_frontier)
        came = dict(state.coarse_came)
        cost = dict(state.coarse_cost)
    else:
        frontier = [(chebyshev(start_m, goal_m) * MACRO_SIZE, 0, start_m)]
        came = {}
        cost = {start_m: 0}

    expanded = 0
    while frontier and expanded < max_nodes:
        _, g, cur = heapq.heappop(frontier)
        if cur == goal_m:
            path = _reconstruct(came, start_m, goal_m)
            if not path or path[0] != start_m:
                path = [start_m, *path] if path else [start_m]
            if state is not None:
                state.coarse_done = True
                state.macro_path = path
                state.coarse_frontier = []
            return path
        if g > cost.get(cur, 10**9):
            continue
        expanded += 1
        for dx, dy in NEIGHBOURS:
            n = (cur[0] + dx, cur[1] + dy)
            sc = _macro_step_cost(w, n, params)
            if sc is None:
                continue
            ng = g + sc
            if ng < cost.get(n, 10**9):
                cost[n] = ng
                came[n] = cur
                heapq.heappush(frontier, (ng + chebyshev(n, goal_m) * MACRO_SIZE, ng, n))

    if state is not None:
        state.coarse_frontier = frontier
        state.coarse_came = came
        state.coarse_cost = cost
        state.coarse_done = False
        state.macro_path = None
    return None


def _perception_bounds(w: WorldModel) -> tuple[int, int, int, int]:
    x0, y0, width, height = w.perception_rect()
    return x0, y0, x0 + width, y0 + height


def _in_rect(p: Pos, x0: int, y0: int, x1: int, y1: int) -> bool:
    return x0 <= p[0] < x1 and y0 <= p[1] < y1


def _fine_target(w: WorldModel, goal: Pos, macro_path: list[Pos] | None) -> Pos:
    """Goal, or the farthest corridor point still inside perception."""
    x0, y0, x1, y1 = _perception_bounds(w)
    if _in_rect(goal, x0, y0, x1, y1):
        return goal
    assert w.pos is not None
    best: Pos | None = None
    best_d = -1
    if macro_path:
        for m in macro_path:
            for pt in (macro_center(m), (m[0] * MACRO_SIZE, m[1] * MACRO_SIZE)):
                if not _in_rect(pt, x0, y0, x1, y1):
                    continue
                d = chebyshev(w.pos, pt)
                if d > best_d:
                    best, best_d = pt, d
    if best is not None:
        return best
    # Aim at the perception edge toward the goal.
    gx, gy = goal
    px, py = w.pos
    tx = min(max(gx, x0), x1 - 1)
    ty = min(max(gy, y0), y1 - 1)
    if (tx, ty) == (px, py):
        for dx, dy in NEIGHBOURS:
            n = (px + dx, py + dy)
            if _in_rect(n, x0, y0, x1, y1):
                return n
    return tx, ty


def _needs_coarse(w: WorldModel, goal: Pos) -> bool:
    assert w.pos is not None
    return chebyshev(w.pos, goal) > w.perception


def cost_path(
    w: WorldModel,
    goal: Pos,
    params: CostGridParams | None = None,
    *,
    nav: NavSearchState | None = None,
) -> list[Pos] | None:
    """Route from ``w.pos`` to ``goal``; excludes the start (A12, A13)."""
    params = params or CostGridParams()
    assert w.pos is not None
    if w.pos == goal:
        return []

    if nav is not None and nav.goal != goal:
        nav.goal = goal
        nav.coarse_done = False
        nav.macro_path = None
        nav.coarse_frontier = []

    direct = _search(w, goal, params, max_nodes=FINE_NODE_BUDGET)
    if direct.path is not None:
        return direct.path
    if not direct.budget_hit:
        return None

    if not _needs_coarse(w, goal):
        return None

    macro_path = _coarse_search(w, goal, params, nav, COARSE_NODE_BUDGET)
    if macro_path is None:
        return None

    fine_goal = _fine_target(w, goal, macro_path)
    x0, y0, x1, y1 = _perception_bounds(w)

    def in_perception(p: Pos) -> bool:
        return _in_rect(p, x0, y0, x1, y1)

    found = _search(
        w,
        fine_goal,
        params,
        max_nodes=FINE_NODE_BUDGET,
        bounds_fn=in_perception,
    )
    return found.path


def nearest_target(
    w: WorldModel,
    targets: set[Pos],
    params: CostGridParams | None = None,
    *,
    nav: NavSearchState | None = None,
) -> tuple[Pos, list[Pos]] | None:
    """Closest target by cost-grid path cost, with its path.

    Tries targets in straight-line order and stops once the lower bound on
    the next target's cost (Chebyshev distance * KNOWN_WALKABLE) cannot beat
    the best path cost found.
    """
    assert w.pos is not None
    params = params or CostGridParams()
    best: tuple[Pos, list[Pos]] | None = None
    best_cost = 0
    for t in sorted(targets, key=lambda p: chebyshev(w.pos, p)):
        if best is not None and chebyshev(w.pos, t) * KNOWN_WALKABLE >= best_cost:
            break
        p = cost_path(w, t, params, nav=None)
        if p is None:
            continue
        # Approximate cost by length; exact cost would need _search return value.
        c = len(p) * KNOWN_WALKABLE
        if best is None or c < best_cost:
            best, best_cost = (t, p), c
    return best

