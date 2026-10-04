"""Cross-map routing over known door warps, then A* on each map (A26).

The graph's nodes are the start, the destination, every known door cell and
every warp landing. Walking edges come from one cost-grid flood per node
(``cost_flood``); a door cell with a recorded warp has one zero-cost edge to
its landing. Stepping onto a door warps, so a door reached on foot is only
ever left through its warp, and one whose warp is unknown is a dead end.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, replace

from ..knowledge_base import KnowledgeBase
from ..knowledge_maps import door_warp_known, iter_doors, view_from_kb
from ..world import DOORS, MapView, Pos, WorldModel
from .planner import CostGridParams, cost_flood, cost_path, nearest_target


@dataclass(frozen=True, order=True)
class _State:
    map_id: int
    pos: Pos
    on_door: bool = False  # reached a door on foot: the only way on is its warp


class _Graph:
    """Per-replan cache of map views and warps, so each is built once."""

    def __init__(self, w: WorldModel, kb: KnowledgeBase | None, params: CostGridParams):
        self.w, self.kb = w, kb
        # Door cells are flood targets, so they must be enterable.
        self.here_params = replace(params, allow_goal_door=True)
        # avoid/costly are cells on the current map; they mean nothing elsewhere.
        self.away_params = replace(self.here_params, avoid=set(), costly=set())
        self._views: dict[int, MapView] = {}
        self._warps: dict[int, dict[Pos, _State]] = {}

    def view(self, map_id: int) -> MapView:
        if map_id not in self._views:
            live = self.w.maps.get(map_id)
            if live is not None:
                self._views[map_id] = live
            elif self.kb is not None:
                self._views[map_id] = view_from_kb(self.kb, map_id)
            else:
                self._views[map_id] = MapView()
        return self._views[map_id]

    def warps(self, map_id: int) -> dict[Pos, _State]:
        if map_id not in self._warps:
            records = iter_doors(self.kb, map_id) if self.kb is not None else []
            self._warps[map_id] = {
                (int(d["x"]), int(d["y"])): _State(int(d["to_map_id"]), (int(d["to_x"]), int(d["to_y"])))
                for d in records
                if door_warp_known(d)
            }
        return self._warps[map_id]

    def doors(self, map_id: int) -> set[Pos]:
        seen = {p for p, b in self.view(map_id).tiles.items() if b in DOORS}
        return seen | set(self.warps(map_id))

    def flood(self, at: _State, targets: set[Pos]) -> dict[Pos, tuple[list[Pos], int]]:
        here = at.map_id == self.w.map_id
        sw = WorldModel(self.w.character_id, map_id=at.map_id, pos=at.pos, perception=self.w.perception)
        sw.maps[at.map_id] = self.view(at.map_id)
        if here:
            sw.entities = self.w.entities
        return cost_flood(sw, targets, self.here_params if here else self.away_params)


def _route(w: WorldModel, kb: KnowledgeBase | None, dest_map: int, dest: Pos, params: CostGridParams) -> list[Pos] | None:
    """First-leg path on the current map of the cheapest route to ``dest_map:dest``.

    Dijkstra over the door graph. The start walks out even when it stands on
    a door: that door did not warp us (or we just landed on it).
    """
    assert w.map_id is not None and w.pos is not None
    g = _Graph(w, kb, params)
    start = _State(w.map_id, w.pos)
    frontier: list[tuple[int, _State]] = [(0, start)]
    cost: dict[_State, int] = {start: 0}
    came: dict[_State, _State] = {}
    first_leg: dict[_State, list[Pos]] = {}
    while frontier:
        c, state = heapq.heappop(frontier)
        if c > cost.get(state, 10**9):
            continue
        if (state.map_id, state.pos) == (dest_map, dest):
            while came.get(state, start) != start:
                state = came[state]
            return first_leg.get(state, [])
        if state.on_door:
            landing = g.warps(state.map_id).get(state.pos)
            if landing is not None and c < cost.get(landing, 10**9):
                cost[landing], came[landing] = c, state
                heapq.heappush(frontier, (c, landing))
            continue
        doors = g.doors(state.map_id)
        targets = doors | ({dest} if state.map_id == dest_map else set())
        for p, (path, seg) in g.flood(state, targets - {state.pos}).items():
            nxt = _State(state.map_id, p, on_door=p in doors)
            if c + seg < cost.get(nxt, 10**9):
                cost[nxt], came[nxt] = c + seg, state
                if state == start:
                    first_leg[nxt] = path
                heapq.heappush(frontier, (c + seg, nxt))
    return None


def route_first_leg(
    w: WorldModel,
    kb: KnowledgeBase | None,
    dest_map: int,
    dest: Pos,
    params: CostGridParams,
) -> list[Pos] | None:
    """Path on the current map toward ``dest_map:dest``, through known door warps when needed.

    None when no known route reaches it, so the goal yields like any
    unreachable one. Without a knowledge base no warp is known, so only a
    destination on the current map can be reached.
    """
    if w.map_id is None or w.pos is None:
        return None
    if w.map_id == dest_map:
        direct = cost_path(w, dest, params)
        if direct is not None:
            return direct
    return _route(w, kb, dest_map, dest, params)


def unvisited_doors_on_map(kb: KnowledgeBase | None, w: WorldModel) -> set[Pos]:
    """Doors on the current map whose warp destination is not recorded yet."""
    doors = {p for p, b in w.view.tiles.items() if b in DOORS}
    if kb is None or w.map_id is None:
        return doors
    known = {(int(d["x"]), int(d["y"])) for d in iter_doors(kb, w.map_id) if door_warp_known(d)}
    return doors - known


def doors_goal_path(
    w: WorldModel,
    kb: KnowledgeBase | None,
    params: CostGridParams,
) -> list[Pos] | None:
    """Walk toward the nearest unvisited door, else the nearest door on this map."""
    if w.map_id is None or w.pos is None:
        return None
    doors = {p for p, b in w.view.tiles.items() if b in DOORS}
    found = nearest_target(w, unvisited_doors_on_map(kb, w), params) or nearest_target(w, doors, params)
    return found[1] if found and found[1] else None
