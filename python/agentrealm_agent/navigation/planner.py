"""A* over the M7 cost grid (A12).

See docs/PLAYABLE_AGENT_PLAN.md Navigation and getting unstuck.
"""

from __future__ import annotations

import heapq
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


@dataclass
class CostGridParams:
    """Per-search knobs for the cost grid."""

    avoid: set[Pos] = field(default_factory=set)  # impassable (rejected tiles, etc.)
    costly: set[Pos] = field(default_factory=set)  # passable at COSTLY_STEP extra (escape off hazards)
    break_nominated: set[Pos] = field(default_factory=set)  # inert until M9: impassable
    hostile_kinds: frozenset[str] = frozenset({"npc"})
    allow_goal_door: bool = False


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


def _search(w: WorldModel, goal: Pos, params: CostGridParams) -> tuple[list[Pos], int] | None:
    assert w.pos is not None
    start = w.pos
    if start == goal:
        return [], 0
    grid = _Grid(w, goal, params)
    if grid.cost(goal) is None:
        return None
    frontier: list[tuple[int, int, Pos]] = [(chebyshev(start, goal) * KNOWN_WALKABLE, 0, start)]
    came: dict[Pos, Pos] = {}
    cost: dict[Pos, int] = {start: 0}
    while frontier:
        _, g, cur = heapq.heappop(frontier)
        if cur == goal:
            out = [cur]
            while out[-1] in came and came[out[-1]] != start:
                out.append(came[out[-1]])
            return out[::-1], g
        if g > cost.get(cur, 10**9):
            continue
        for dx, dy in NEIGHBOURS:
            n = (cur[0] + dx, cur[1] + dy)
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
    return None


def cost_path(w: WorldModel, goal: Pos, params: CostGridParams | None = None) -> list[Pos] | None:
    """A* from ``w.pos`` to ``goal`` over the cost grid. Excludes the start."""
    found = _search(w, goal, params or CostGridParams())
    return found[0] if found else None


def nearest_target(
    w: WorldModel,
    targets: set[Pos],
    params: CostGridParams | None = None,
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
        found = _search(w, t, params)
        if found is None:
            continue
        p, c = found
        if best is None or c < best_cost:
            best, best_cost = (t, p), c
    return best
