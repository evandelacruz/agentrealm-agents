"""A* over the M7 cost grid (A12).

See docs/PLAYABLE_AGENT_PLAN.md Navigation and getting unstuck.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field

from ..world import DOORS, NEIGHBOURS, VOID, WALKABLE, Entity, MapView, Pos, WorldModel, chebyshev

# Base step costs (PLAYABLE_AGENT_PLAN Navigation §1).
KNOWN_WALKABLE = 1
FOG = 2
# High but finite: route around when a step is possible (occupants move).
OCCUPANT = 50
# Added at distance 0; falls off one per block of Chebyshev separation.
HOSTILE_DANGER = 30
HOSTILE_DANGER_RADIUS = 6


@dataclass
class CostGridParams:
    """Per-search knobs for the cost grid."""

    avoid: set[Pos] = field(default_factory=set)  # impassable (rejected tiles, etc.)
    break_nominated: set[Pos] = field(default_factory=set)  # inert until M9: impassable
    hostile_kinds: frozenset[str] = frozenset({"npc"})
    allow_goal_door: bool = False


def known_prefix(path: list[Pos], view: MapView) -> list[Pos]:
    """The part of ``path`` that lies on ground we have already seen."""
    out: list[Pos] = []
    for p in path:
        block = view.tiles.get(p)
        if block is None:
            break
        if block == VOID:
            break
        if block in WALKABLE or (block in DOORS):
            out.append(p)
            continue
        break
    return out


def _hostiles(w: WorldModel, kinds: frozenset[str]) -> list[Entity]:
    return [e for e in w.entities if e.kind in kinds]


def _cell_state(view: MapView, p: Pos) -> str:
    """``fog`` | ``void`` | ``walkable`` | ``door`` | ``blocked``."""
    block = view.tiles.get(p)
    if block is None:
        return "fog"
    if block == VOID:
        return "void"
    if block in DOORS:
        return "door"
    if block in WALKABLE:
        return "walkable"
    return "blocked"


def step_cost(
    w: WorldModel,
    p: Pos,
    params: CostGridParams,
    *,
    goal: Pos,
    hostiles: list[Entity],
) -> int | None:
    """Movement cost onto ``p``, or ``None`` when impassable."""
    if p in params.avoid or p in params.break_nominated:
        return None
    state = _cell_state(w.view, p)
    if state == "void" or state == "blocked":
        return None
    if state == "door":
        if p == goal and params.allow_goal_door:
            return KNOWN_WALKABLE
        return None
    base = FOG if state == "fog" else KNOWN_WALKABLE
    block = w.view.tiles.get(p)
    if block in ("fire", "lava"):
        base += w.view.occupy_damage(p)
    for ent in w.entities:
        if ent.kind in ("character", "npc") and ent.pos == p:
            base += OCCUPANT
            break
    for h in hostiles:
        d = chebyshev(p, h.pos)
        if d < HOSTILE_DANGER_RADIUS:
            base += max(0, HOSTILE_DANGER - d * 5)
    return base


def cost_path(w: WorldModel, goal: Pos, params: CostGridParams | None = None) -> list[Pos] | None:
    """A* from ``w.pos`` to ``goal`` over the cost grid. Excludes the start."""
    assert w.pos is not None
    params = params or CostGridParams()
    start = w.pos
    if start == goal:
        return []
    hostiles = _hostiles(w, params.hostile_kinds)
    if step_cost(w, goal, params, goal=goal, hostiles=hostiles) is None:
        return None

    frontier: list[tuple[int, int, Pos]] = [(chebyshev(start, goal), 0, start)]
    came: dict[Pos, Pos] = {}
    cost: dict[Pos, int] = {start: 0}
    while frontier:
        _, g, cur = heapq.heappop(frontier)
        if cur == goal:
            out = [cur]
            while out[-1] in came and came[out[-1]] != start:
                out.append(came[out[-1]])
            return out[::-1]
        if g > cost.get(cur, 10**9):
            continue
        for dx, dy in NEIGHBOURS:
            n = (cur[0] + dx, cur[1] + dy)
            sc = step_cost(w, n, params, goal=goal, hostiles=hostiles)
            if sc is None:
                continue
            ng = g + sc
            if ng < cost.get(n, 10**9):
                cost[n] = ng
                came[n] = cur
                heapq.heappush(frontier, (ng + chebyshev(n, goal), ng, n))
    return None


def path_cost(w: WorldModel, path: list[Pos], params: CostGridParams | None = None) -> int:
    """Sum of step costs along ``path`` (for nearest-target comparison)."""
    params = params or CostGridParams()
    hostiles = _hostiles(w, params.hostile_kinds)
    total = 0
    for p in path:
        sc = step_cost(w, p, params, goal=path[-1] if path else p, hostiles=hostiles)
        if sc is None:
            return 10**9
        total += sc
    return total


def nearest_target(
    w: WorldModel,
    targets: set[Pos],
    params: CostGridParams | None = None,
) -> tuple[Pos, list[Pos]] | None:
    """Closest target by cost-grid path cost, with its path."""
    assert w.pos is not None
    params = params or CostGridParams()
    best: tuple[Pos, list[Pos]] | None = None
    best_cost = 0
    for t in sorted(targets, key=lambda p: chebyshev(w.pos, p)):
        if best is not None and chebyshev(w.pos, t) >= best_cost:
            break
        p = cost_path(w, t, params)
        if p is None:
            continue
        c = path_cost(w, p, params)
        if best is None or c < best_cost:
            best, best_cost = (t, p), c
    return best
