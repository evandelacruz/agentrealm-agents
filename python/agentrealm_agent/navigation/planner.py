"""A* over the M7 cost grid (A12) with two-level search (A13).

See docs/PLAYABLE_AGENT_PLAN.md Navigation and getting unstuck.
"""

from __future__ import annotations

import heapq
from collections import defaultdict
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
# Node budgets per replan so search never stalls a tick (A13). Read at call
# time, so a caller's override (or a test's patch) takes effect.
COARSE_NODE_BUDGET = 48  # cache tiles expanded by the corridor search
FINE_NODE_BUDGET = 400  # cells expanded by each cell-level search


@dataclass
class CostGridParams:
    """Per-search knobs for the cost grid."""

    avoid: set[Pos] = field(default_factory=set)  # impassable (rejected tiles, etc.)
    costly: set[Pos] = field(default_factory=set)  # passable at COSTLY_STEP extra (escape off hazards)
    break_nominated: set[Pos] = field(default_factory=set)  # breakable cells considered for opening
    break_costs: dict[Pos, int] = field(default_factory=dict)  # passable at break time + 1 (+ tool price)
    # Which entities add danger: ``pathing.grid_params`` passes the agent's
    # one hostility test (``survival.is_hostile``), so townsfolk repel no path.
    is_hostile: Callable[[WorldModel, Entity], bool] = lambda w, e: e.kind == "npc"
    allow_goal_door: bool = False
    fog_cost: int = FOG  # A15 step 1 raises this to prefer known ground
    # Danger peak per hostile (kind, id), in place of HOSTILE_DANGER: Retreat
    # sets its pursuers' to 0, so it heads straight for safety (A9).
    danger_peaks: dict[tuple[str, int], int] = field(default_factory=dict)


@dataclass
class NavSearchState:
    """One goal's corridor search, resumed across replans (A13).

    The search runs backward from the goal's cache tile, so the tree stays
    rooted where it started however far we walk: each replan reads the
    corridor from the cache tile we stand in, and only re-aims the frontier.
    """

    goal: Pos
    map_id: int | None = None  # map the goal is on; the caller starts over when it changes (A26)
    origin: Pos | None = None  # cache tile the frontier's priorities aim at
    frontier: list[tuple[int, int, Pos]] = field(default_factory=list)
    came: dict[Pos, Pos] = field(default_factory=dict)  # cache tile -> next one toward the goal
    cost: dict[Pos, int] = field(default_factory=dict)
    closed: set[Pos] = field(default_factory=set)
    step: dict[Pos, int | None] = field(default_factory=dict)  # cost to enter each expanded tile, as searched
    # Cell estimates the window search has raised past the corridor's (``_fine_path``).
    learned: dict[Pos, int] = field(default_factory=dict)
    # The last ``cost_path`` answer is final for the map as known: False only
    # while the corridor is unfinished and the walk goes by straight line, so
    # its ``None`` means "no step yet", not "no path" (``retreat_step``).
    settled: bool = True

    def reset(self, goal: Pos) -> None:
        self.goal, self.origin = goal, None
        self.frontier, self.came, self.cost, self.closed, self.step = [], {}, {}, set(), {}
        self.learned = {}


def macro_cell(p: Pos) -> Pos:
    return p[0] // MACRO_SIZE, p[1] // MACRO_SIZE


def macro_center(m: Pos) -> Pos:
    return m[0] * MACRO_SIZE + MACRO_SIZE // 2, m[1] * MACRO_SIZE + MACRO_SIZE // 2


def known_prefix(path: list[Pos], view: MapView) -> list[Pos]:
    """The leading part of ``path`` on walkable or door tiles we have seen."""
    out: list[Pos] = []
    for p in path:
        block = view.tiles.get(p)
        if block is None or block == VOID or not (block in WALKABLE or block in DOORS):
            break
        out.append(p)
    return out


def danger_map(hostiles: list[Entity], peaks: dict[tuple[str, int], int] | None = None) -> dict[Pos, int]:
    """The hostiles' share of the cost onto each cell: more the nearer they stand.

    Each hostile adds ``HOSTILE_DANGER`` minus 5 per block of Chebyshev
    distance, out to ``HOSTILE_DANGER_RADIUS``; ``peaks`` overrides
    ``HOSTILE_DANGER`` for the hostiles it names. Cells it leaves out cost
    nothing. Laid out once per search: a big map prices tens of thousands of
    cells, and a lookup each beats a pass over every hostile each (A23 Run 2).
    """
    out: dict[Pos, int] = {}
    for h in hostiles:
        peak = peaks.get((h.kind, h.id), HOSTILE_DANGER) if peaks else HOSTILE_DANGER
        hx, hy = h.pos
        for d, ring in enumerate(_RINGS):
            share = peak - d * 5
            if share <= 0:
                break
            for dx, dy in ring:
                p = (hx + dx, hy + dy)
                out[p] = out.get(p, 0) + share
    return out


# The cells at each Chebyshev distance inside HOSTILE_DANGER_RADIUS, as offsets.
_RINGS = [
    [(dx, dy) for dx in range(-d, d + 1) for dy in range(-d, d + 1) if max(abs(dx), abs(dy)) == d]
    for d in range(HOSTILE_DANGER_RADIUS)
]


class _Grid:
    """One search's view of the cost grid, with per-search state precomputed."""

    def __init__(self, w: WorldModel, goals: set[Pos], params: CostGridParams, *, same_world: _Grid | None = None):
        """``same_world``: a grid on this ``w`` and ``params`` whose prices
        this one reuses (``nearest_target`` searches twice, A23 Run 2)."""
        self.w, self.goals, self.params = w, goals, params
        # The single goal of a cost_path search (A13); None for a flood.
        self.goal: Pos | None = next(iter(goals)) if len(goals) == 1 else None
        self.tiles = w.view.tiles
        if same_world is not None:
            assert same_world.w is w and same_world.params is params
            self.occupied, self.hostiles = same_world.occupied, same_world.hostiles
            self.for_sale = same_world.for_sale
            self._danger, self._costs = same_world.danger_map(), same_world._costs
            self.known_box = same_world.known_box
        else:
            self.occupied = w.occupied()
            self.for_sale = w.for_sale()
            self.hostiles: list[Entity] = [e for e in w.entities if params.is_hostile(w, e)]
            self._danger: dict[Pos, int] | None = None  # built on first use (``danger_map``)
            # Each non-goal cell's cost, worked out once: a search prices a
            # cell from every neighbour it expands (A23 Run 2). A grid lives
            # for one search, so the world cannot change under it.
            self._costs: dict[Pos, int | None] = {}
            # Fog is unbounded, so the search is boxed to the known extent
            # plus start and goals, with a one-tile fog ring: any detour beyond
            # the box crosses only fog and is no cheaper than walking the ring.
            xs = [p[0] for p in self.tiles] + [w.pos[0]]
            ys = [p[1] for p in self.tiles] + [w.pos[1]]
            # The box without the goals: ``nearest_target`` checks they add nothing.
            self.known_box = (min(xs) - 1, min(ys) - 1, max(xs) + 1, max(ys) + 1)
        x0, y0, x1, y1 = self.known_box
        for gx, gy in goals:
            x0, y0, x1, y1 = min(x0, gx - 1), min(y0, gy - 1), max(x1, gx + 1), max(y1, gy + 1)
        self.box = (x0, y0, x1, y1)

    def in_box(self, p: Pos) -> bool:
        x0, y0, x1, y1 = self.box
        return x0 <= p[0] <= x1 and y0 <= p[1] <= y1

    def cost(self, p: Pos) -> int | None:
        """Movement cost onto ``p``, or ``None`` when impassable."""
        if p in self.goals:
            return self._price(p, goal=True)
        costs = self._costs
        if p in costs:
            return costs[p]
        out = costs[p] = self._price(p, goal=False)
        return out

    def _price(self, p: Pos, *, goal: bool) -> int | None:
        """``cost`` worked out. Whether ``p`` is a goal is the only thing it
        reads about the goals, so a non-goal cell's price holds for any goals
        and grids on one world share them (``same_world``)."""
        params = self.params
        if p in params.avoid:
            return None
        if p in params.break_costs:
            base = params.break_costs[p]
            block = self.tiles.get(p)
            if block is not None and block in DOORS:
                return None
            if p in self.occupied:
                base += OCCUPANT
            return base + self.danger(p)
        if p in params.break_nominated:
            return None
        if p in self.for_sale and not goal:
            # Walking onto a priced supply buys it: only a ``Take`` buys (A21).
            # As the goal it is where Shop walks up to; no Step lands on it.
            return None
        block = self.tiles.get(p)
        if block is not None and block in DOORS:
            # Stepping onto a door warps, so a door is only ever the goal.
            return KNOWN_WALKABLE if goal and params.allow_goal_door else None
        if block is None:
            base = params.fog_cost
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
        return base + self.danger(p)

    def danger(self, p: Pos) -> int:
        return self.danger_map().get(p, 0)

    def danger_map(self) -> dict[Pos, int]:
        if self._danger is None:
            self._danger = danger_map(self.hostiles, self.params.danger_peaks)
        return self._danger


class _MacroCosts:
    """Cost to cross each 16×16 cache tile, from the same cost grid as the cells.

    Cells nobody has said anything about are fog. The rest (known tiles,
    avoided, costly and break-nominated cells, occupants, and cells in a
    hostile's danger radius) are priced by ``_Grid.cost``. Crossing costs
    MACRO_SIZE times the mean passable step, divided by the passable share,
    so a tile that is mostly wall or mostly hazard is a long way round. An
    open known tile costs MACRO_SIZE, the least possible, so Chebyshev
    distance * MACRO_SIZE stays a lower bound.
    """

    def __init__(self, grid: _Grid):
        self.grid = grid
        self.memo: dict[Pos, int | None] = {}
        cells: dict[Pos, set[Pos]] = defaultdict(set)
        params = grid.params
        for src in (
            grid.w.view.tiles,
            params.avoid,
            params.costly,
            params.break_nominated,
            params.break_costs,
            grid.occupied,
        ):
            for p in src:
                cells[macro_cell(p)].add(p)
        r = HOSTILE_DANGER_RADIUS - 1
        for h in grid.hostiles:
            for dx in range(-r, r + 1):
                for dy in range(-r, r + 1):
                    p = (h.pos[0] + dx, h.pos[1] + dy)
                    cells[macro_cell(p)].add(p)
        self.cells = cells

    def __call__(self, m: Pos) -> int | None:
        if m in self.memo:
            return self.memo[m]
        n = MACRO_SIZE * MACRO_SIZE
        special = self.cells.get(m, ())
        passable = n - len(special)
        total = passable * self.grid.params.fog_cost
        for p in special:
            c = self.grid.cost(p)
            if c is not None:
                passable += 1
                total += c
        out = None if passable == 0 else max(MACRO_SIZE * KNOWN_WALKABLE, round(MACRO_SIZE * total * n / passable**2))
        self.memo[m] = out
        return out


def _unwind(came: dict[Pos, Pos], start: Pos, end: Pos) -> list[Pos]:
    out = [end]
    while out[-1] in came and came[out[-1]] != start:
        out.append(came[out[-1]])
    return out[::-1]


@dataclass
class _SearchResult:
    path: list[Pos] | None
    cost: int
    budget_hit: bool


def _astar(grid: _Grid, max_nodes: int) -> _SearchResult:
    w, goal = grid.w, grid.goal
    assert w.pos is not None
    start = w.pos
    if start == goal:
        return _SearchResult([], 0, False)
    if grid.cost(goal) is None:
        return _SearchResult(None, 0, False)
    frontier: list[tuple[int, int, Pos]] = [(chebyshev(start, goal) * KNOWN_WALKABLE, 0, start)]
    came: dict[Pos, Pos] = {}
    cost: dict[Pos, int] = {start: 0}
    expanded = 0
    while frontier and expanded < max_nodes:
        _, g, cur = heapq.heappop(frontier)
        if cur == goal:
            return _SearchResult(_unwind(came, start, goal), g, False)
        if g > cost.get(cur, 10**9):
            continue
        expanded += 1
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
    return _SearchResult(None, 0, bool(frontier))


def _search(w: WorldModel, goal: Pos, params: CostGridParams, max_nodes: int = 10**9) -> _SearchResult:
    return _astar(_Grid(w, {goal}, params), max_nodes)


def no_way(w: WorldModel, goal: Pos, params: CostGridParams | None = None) -> bool:
    """True only when ``goal`` is walled in: the passable cells joined to it
    run out within ``FINE_NODE_BUDGET`` without reaching ``w.pos``, or the
    goal itself is impassable. Only the goal's side proves it: a search from
    our side that runs out says where we stand is shut, which a move ends,
    not where the goal is."""
    assert w.pos is not None
    if w.pos == goal:
        return False
    grid = _Grid(w, {goal}, params or CostGridParams())
    return grid.cost(goal) is None or _walled_in(grid, goal, w.pos, FINE_NODE_BUDGET)


def _walled_in(grid: _Grid, goal: Pos, start: Pos, max_cells: int) -> bool:
    """The passable cells joined to ``goal`` number at most ``max_cells`` and
    do not touch ``start``."""
    seen, todo = {goal}, [goal]
    while todo:
        cur = todo.pop()
        for dx, dy in NEIGHBOURS:
            n = (cur[0] + dx, cur[1] + dy)
            if n == start:
                return False
            if n in seen or not grid.in_box(n) or grid.cost(n) is None:
                continue
            seen.add(n)
            if len(seen) > max_cells:
                return False
            todo.append(n)
    return True


def _corridor(nav: NavSearchState, start_m: Pos, goal_m: Pos) -> list[Pos]:
    out = [start_m]
    while out[-1] != goal_m:
        out.append(nav.came[out[-1]])
    return out


def _coarse_search(grid: _Grid, macro: _MacroCosts, nav: NavSearchState, max_nodes: int) -> list[Pos] | None:
    """Cache-tile corridor from where we stand to the goal, or ``None`` while unfinished.

    Resumes ``nav``. A finished corridor is re-priced on every call and the
    search starts over when a tile on it got dearer or impassable.
    """
    assert grid.w.pos is not None
    start_m, goal_m = macro_cell(grid.w.pos), macro_cell(grid.goal)
    if start_m in nav.closed:
        path = _corridor(nav, start_m, goal_m)
        if all((c := macro(m)) is not None and c <= (nav.step.get(m) or 0) for m in path[1:]):
            return path
        nav.reset(nav.goal)
    if not nav.cost:
        nav.cost = {goal_m: 0}
        nav.frontier = [(0, 0, goal_m)]
        nav.origin = None

    def h(m: Pos) -> int:
        return chebyshev(m, start_m) * MACRO_SIZE * KNOWN_WALKABLE

    if nav.origin != start_m:
        nav.origin = start_m
        nav.frontier = [(g + h(m), g, m) for _, g, m in nav.frontier]
        heapq.heapify(nav.frontier)
    # Fog is unbounded: keep to the cache tiles round the cell search's box.
    (bx0, by0), (bx1, by1) = macro_cell(grid.box[:2]), macro_cell(grid.box[2:])
    expanded = 0
    while nav.frontier and expanded < max_nodes:
        _, g, cur = heapq.heappop(nav.frontier)
        if cur in nav.closed or g > nav.cost.get(cur, 10**9):
            continue
        nav.closed.add(cur)
        expanded += 1
        sc = nav.step[cur] = macro(cur)
        if sc is not None:
            for dx, dy in NEIGHBOURS:
                n = (cur[0] + dx, cur[1] + dy)
                if not (bx0 - 1 <= n[0] <= bx1 + 1 and by0 - 1 <= n[1] <= by1 + 1):
                    continue
                ng = g + sc
                if n not in nav.closed and ng < nav.cost.get(n, 10**9):
                    nav.cost[n] = ng
                    nav.came[n] = cur
                    heapq.heappush(nav.frontier, (ng + h(n), ng, n))
        if cur == start_m:
            return _corridor(nav, start_m, goal_m)
    return None


def _perception_bounds(w: WorldModel) -> tuple[int, int, int, int]:
    x0, y0, width, height = w.perception_rect()
    return x0, y0, x0 + width, y0 + height


def _in_rect(p: Pos, x0: int, y0: int, x1: int, y1: int) -> bool:
    return x0 <= p[0] < x1 and y0 <= p[1] < y1


def _corridor_index(corridor: list[Pos]) -> dict[Pos, int]:
    """Corridor tiles and the tiles beside them, each with its corridor position.

    Two diagonal corridor tiles meet at a single corner cell, so the cell
    search may also use the tiles round the corridor.
    """
    index = {m: i for i, m in enumerate(corridor)}
    beside: dict[Pos, int] = {}
    for i, m in enumerate(corridor):
        for dx, dy in NEIGHBOURS:
            n = (m[0] + dx, m[1] + dy)
            if n not in index:
                beside[n] = min(beside.get(n, i), i)
    return {**beside, **index}


def _toward(goal: Pos, corridor: list[Pos] | None, came: dict[Pos, Pos] | None = None) -> Callable[[Pos], int]:
    """Estimated steps left to ``goal``, along the corridor search's tree when there is one.

    A cell heads for the next tile toward the goal in ``came`` (the tree the
    corridor search grew back from the goal's tile): its centre, or the goal
    itself when that tile holds it. From there it follows the tree. A cell
    in a tile beside the corridor that the tree has not reached heads for
    the best tile next to it that it has.

    Each cell's estimate depends on the tree alone, never on the tile we
    stand in, so a step that lowered it is never undone by the next
    decision's plan from the new cell (free-play run 4: a corridor read from
    each side of a tile edge, or a fall back to straight-line, priced two
    cells each below the other, and Travel paced between them).
    """
    if not corridor:
        return lambda p: chebyshev(p, goal)
    goal_m = macro_cell(goal)
    tree = dict(came or {})
    for a, b in zip(corridor, corridor[1:]):
        tree.setdefault(a, b)

    def point(m: Pos) -> Pos:
        return goal if m == goal_m else macro_center(m)

    rest: dict[Pos, int | None] = {goal_m: 0}

    def rest_from(m: Pos) -> int | None:
        """Estimated steps from ``point(m)`` to the goal along the tree; None off it."""
        chain: list[Pos] = []
        cur: Pos | None = m
        while cur is not None and cur not in rest and cur not in chain:
            chain.append(cur)
            cur = tree.get(cur)
        tail = rest.get(cur) if cur is not None else None
        for t in reversed(chain):
            nxt = tree.get(t)
            tail = None if tail is None or nxt is None else tail + chebyshev(point(t), point(nxt))
            rest[t] = tail
        return rest[m]

    def via(p: Pos, m: Pos) -> int | None:
        r = rest_from(m)
        return None if r is None else chebyshev(p, point(m)) + r

    def h(p: Pos) -> int:
        m = macro_cell(p)
        if m == goal_m:
            return chebyshev(p, goal)
        nxt = tree.get(m)
        if nxt is not None and (out := via(p, nxt)) is not None:
            return out
        near = [v for dx, dy in NEIGHBOURS if (v := via(p, (m[0] + dx, m[1] + dy))) is not None]
        return min(near) if near else chebyshev(p, goal)

    return h


def _fine_path(
    grid: _Grid,
    h: Callable[[Pos], int],
    corridor: set[Pos] | None,
    max_nodes: int,
    learned: dict[Pos, int] | None = None,
) -> list[Pos] | None:
    """Best path inside the perception window, kept to ``corridor`` tiles when given.

    ``corridor`` is the corridor's tiles and those beside them. Ends on the
    goal when the search reaches it. Otherwise it ends where the walk can go
    on: a cell beside the window's edge with a passable cell past it, or one
    the node budget left unexpanded. Never in a dead end the search saw all
    round: a pocket in sight is no end, however near the goal it looks
    (free-play run 6: Park walked 2 cells into one and stood there).

    With ``learned`` (the corridor branch, ``NavSearchState.learned``) it is
    real-time search that learns (LSS-LRTA*): it ends on the end with the
    least cost so far plus estimate, and raises the estimate of every cell
    it searched to what it learned the way on costs. A dead end then looks
    as dear as the way out of it, so the next decision walks away from it
    and none walks back in (free-play run 6).

    Without it, an end scores its cost so far plus twice ``h``, which
    favours progress over an exactly cheapest prefix, and the start competes
    on the same score: ``None`` when no end beats where we stand, so a dead
    end next to a goal that cannot be reached is "no path", never a step
    away and back (A58 run 7, Heal pacing beside unreachable food).
    """
    w, goal = grid.w, grid.goal
    assert w.pos is not None
    start = w.pos
    x0, y0, x1, y1 = _perception_bounds(w)

    def est(p: Pos) -> int:
        return max(h(p), learned.get(p, 0)) if learned is not None else h(p)

    frontier: list[tuple[int, int, Pos]] = [(est(start), 0, start)]
    came: dict[Pos, Pos] = {}
    cost: dict[Pos, int] = {start: 0}
    expanded: set[Pos] = set()
    # Past the window's edge: cost to get there, and the cell in the window it is entered from.
    past: dict[Pos, tuple[int, Pos]] = {}
    while frontier and len(expanded) < max_nodes:
        _, g, cur = heapq.heappop(frontier)
        if g > cost.get(cur, 10**9) or cur in expanded:
            continue
        if cur == goal:
            return _unwind(came, start, goal)
        expanded.add(cur)
        for dx, dy in NEIGHBOURS:
            n = (cur[0] + dx, cur[1] + dy)
            if not grid.in_box(n) or (corridor is not None and macro_cell(n) not in corridor):
                continue
            sc = grid.cost(n)
            if sc is None:
                continue
            ng = g + sc
            if not _in_rect(n, x0, y0, x1, y1):
                if ng < past.get(n, (10**9, cur))[0]:
                    past[n] = (ng, cur)
                continue
            if ng < cost.get(n, 10**9):
                cost[n] = ng
                came[n] = cur
                heapq.heappush(frontier, (ng + est(n), ng, n))
    # Each end: its cost from here, and the last cell of the path to it inside the window.
    ends = {p: (cost[p], p) for p in cost.keys() - expanded}
    ends.update(past)
    if learned is not None:
        if not ends:
            return None  # walled in inside the window
        _learn(grid, expanded, ends, est, learned)
        best = min(ends, key=lambda p: (ends[p][0] + est(p), est(p), p))
        last = ends[best][1]
        return None if last == start else _unwind(came, start, last)
    candidates = {last for _, last in ends.values()} | {start}
    best = min(candidates, key=lambda p: (cost[p] + 2 * h(p), h(p), p != start, p))
    return None if best == start else _unwind(came, start, best)


def _learn(
    grid: _Grid, searched: set[Pos], ends: dict[Pos, tuple[int, Pos]], est: Callable[[Pos], int], learned: dict[Pos, int]
) -> None:
    """Raise each searched cell's estimate to the cheapest way from it to an
    end, plus that end's estimate: one Dijkstra back from the ends."""
    best = {p: est(p) for p in ends}
    queue = [(v, p) for p, v in best.items()]
    heapq.heapify(queue)
    while queue:
        v, cur = heapq.heappop(queue)
        if v > best.get(cur, 10**9):
            continue
        sc = grid.cost(cur)
        if sc is None:
            continue
        for dx, dy in NEIGHBOURS:
            n = (cur[0] + dx, cur[1] + dy)
            if n not in searched or n in ends:
                continue
            nv = v + sc
            if nv < best.get(n, 10**9):
                best[n] = nv
                heapq.heappush(queue, (nv, n))
    for p in searched:
        if p in best and best[p] > learned.get(p, 0):
            learned[p] = best[p]


def path_cost(w: WorldModel, path: list[Pos], goal: Pos, params: CostGridParams | None = None) -> int | None:
    """What walking ``path`` costs on today's cost grid, plus the straight-line
    rest from its end to ``goal``; None when a cell on it is impassable now.

    Prices a kept path and a new plan alike, so a walk can tell whether the
    new one is really cheaper (``navigation.walk``, A15).
    """
    grid = _Grid(w, {goal}, params or CostGridParams())
    total = 0
    for p in path:
        c = grid.cost(p)
        if c is None:
            return None
        total += c
    end = path[-1] if path else w.pos
    return total + chebyshev(end, goal) * KNOWN_WALKABLE


def hostile_cost(w: WorldModel, path: list[Pos], params: CostGridParams | None = None) -> int:
    """The hostiles' share of what walking ``path`` costs (``_Grid.danger``)."""
    params = params or CostGridParams()
    hostiles = [e for e in w.entities if params.is_hostile(w, e)]
    shares = danger_map(hostiles, params.danger_peaks)
    return sum(shares.get(p, 0) for p in path)


def cost_flood(
    w: WorldModel, targets: set[Pos], params: CostGridParams | None = None
) -> dict[Pos, tuple[list[Pos], int]]:
    """Cheapest path and cost from ``w.pos`` to every reachable target, in one flood (A26).

    Dijkstra over the same cost grid as ``cost_path``. A door target is
    entered (with ``allow_goal_door``) but never walked through, since
    stepping onto it warps. Paths exclude the start.
    """
    assert w.pos is not None
    params = params or CostGridParams()
    start = w.pos
    out: dict[Pos, tuple[list[Pos], int]] = {}
    if start in targets:
        out[start] = ([], 0)
    remaining = set(targets) - {start}
    if remaining:
        out.update(_flood(_Grid(w, remaining, params), remaining))
    return out


def _flood(grid: _Grid, targets: set[Pos], *, cheapest_only: bool = False) -> dict[Pos, tuple[list[Pos], int]]:
    """``cost_flood`` on ``grid``, whose goals are ``targets``.

    ``cheapest_only`` stops once the cheapest targets are found, so only
    those that tie for the least cost come back, and the flood reads no
    further than their cost (``nearest_target``).
    """
    w = grid.w
    assert w.pos is not None
    start = w.pos
    out: dict[Pos, tuple[list[Pos], int]] = {}
    remaining = set(targets)
    frontier: list[tuple[int, Pos]] = [(0, start)]
    came: dict[Pos, Pos] = {}
    cost: dict[Pos, int] = {start: 0}
    least: int | None = None
    x0, y0, x1, y1 = grid.box
    step_cost = grid.cost
    while frontier and remaining:
        g, cur = heapq.heappop(frontier)
        if least is not None and g > least:
            break
        if g > cost.get(cur, 10**9):
            continue
        if cur in remaining:
            remaining.discard(cur)
            out[cur] = (_unwind(came, start, cur), g)
            if cheapest_only:
                least = g
            if w.view.tiles.get(cur) in DOORS:
                continue
        cx, cy = cur
        for dx, dy in NEIGHBOURS:
            nx, ny = cx + dx, cy + dy
            if not (x0 <= nx <= x1 and y0 <= ny <= y1):  # ``grid.in_box``, inlined: the hot loop
                continue
            n = (nx, ny)
            sc = step_cost(n)
            if sc is None:
                continue
            ng = g + sc
            if ng < cost.get(n, 10**9):
                cost[n] = ng
                came[n] = cur
                heapq.heappush(frontier, (ng, n))
    return out


def cost_path(
    w: WorldModel,
    goal: Pos,
    params: CostGridParams | None = None,
    *,
    nav: NavSearchState | None = None,
    coarse_budget: int | None = None,
    fine_budget: int | None = None,
) -> list[Pos] | None:
    """Route from ``w.pos`` toward ``goal``; excludes the start (A12, A13).

    One A* over the cost grid, budgeted. When it finds the goal, or proves
    it unreachable, that is the answer. When the budget runs out instead,
    plan only inside the perception window: toward the goal when it is in
    sight, else along the coarse corridor once the search in ``nav`` has
    finished it, and straight toward the goal until then. That path ends at
    the goal only if the goal is in sight; the caller replans when it has
    walked it.
    """
    params = params or CostGridParams()
    coarse_budget = COARSE_NODE_BUDGET if coarse_budget is None else coarse_budget
    fine_budget = FINE_NODE_BUDGET if fine_budget is None else fine_budget
    assert w.pos is not None
    if w.pos == goal:
        return []
    grid = _Grid(w, {goal}, params)
    direct = _astar(grid, fine_budget)
    if nav is not None:
        nav.settled = True
    if direct.path is not None or not direct.budget_hit:
        return direct.path

    corridor = None
    if not _in_rect(goal, *_perception_bounds(w)):
        if nav is None:
            nav = NavSearchState(goal=goal)
        elif nav.goal != goal:
            nav.reset(goal)
        corridor = _coarse_search(grid, _MacroCosts(grid), nav, coarse_budget)
    if corridor:
        # The corridor's answer stands, a step or none: a straight-line
        # retry from a cell the corridor will not leave walks back to where
        # the corridor came from (free-play run 4). None is "no path", which
        # stuck detection escalates (A15).
        return _fine_path(
            grid, _toward(goal, corridor, nav.came), set(_corridor_index(corridor)), fine_budget, nav.learned
        )
    if nav is not None and not _in_rect(goal, *_perception_bounds(w)):
        nav.settled = False  # the corridor search goes on next call
    return _fine_path(grid, _toward(goal, None), None, fine_budget)


def nearest_target(
    w: WorldModel, targets: set[Pos], params: CostGridParams | None = None
) -> tuple[Pos, list[Pos]] | None:
    """Closest target by cost-grid path cost, with its path.

    Ties go to the target nearer in a straight line, then to the smaller
    cell, whatever order the set holds them in, so one map always gives the
    same target (A15). Targets are known tiles, so this stays the one
    unbudgeted search of A12 rather than the two-level search.

    One flood from where we stand (``_flood``, as ``cost_flood``) finds the target: it stops at the
    cheapest, so it reads no further than that target's cost, and a target
    nothing reaches costs no more than one flood however many there are.
    Then one A* to it gives the path ``cost_path`` would walk. A big map
    once took a whole A* per unreachable target, seconds a decision (A23 Run 2).
    """
    assert w.pos is not None
    params = params or CostGridParams()
    start = w.pos
    if start in targets:
        return start, []
    if not targets:
        return None
    grid = _Grid(w, set(targets), params)
    if grid.box != grid.known_box:
        # A target past the known ground widens the box the search may use,
        # and each target's own search sees only its own (``_search``).
        return _nearest_target_one_by_one(w, targets, params)
    cheapest = _flood(grid, targets, cheapest_only=True)
    if not cheapest:
        return None
    best = min(cheapest, key=lambda t: (chebyshev(start, t), t))
    # The A* path, not the flood's: the one ``cost_path`` walks to ``best``.
    found = _astar(_Grid(w, {best}, params, same_world=grid), 10**9)
    assert found.path is not None
    return best, found.path


def _nearest_target_one_by_one(
    w: WorldModel, targets: set[Pos], params: CostGridParams
) -> tuple[Pos, list[Pos]] | None:
    """``nearest_target`` by one A* per target, in straight-line order.

    Stops once the lower bound on the next target's cost (Chebyshev distance
    * KNOWN_WALKABLE) cannot beat the best path cost found.
    """
    assert w.pos is not None
    best: tuple[Pos, list[Pos]] | None = None
    best_cost = 0
    for t in sorted(targets, key=lambda p: (chebyshev(w.pos, p), p)):
        if best is not None and chebyshev(w.pos, t) * KNOWN_WALKABLE >= best_cost:
            break
        found = _search(w, t, params)
        if found.path is None:
            continue
        if best is None or found.cost < best_cost:
            best, best_cost = (t, found.path), found.cost
    return best
