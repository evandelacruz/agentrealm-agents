"""Cross-map routing over known door warps, then A* on each map (A26)."""

from __future__ import annotations

import heapq
from dataclasses import dataclass

from ..knowledge_base import KnowledgeBase
from ..knowledge_maps import door_warp_known, iter_doors, view_from_kb
from ..world import DOORS, MapView, Pos, WorldModel, chebyshev
from .planner import CostGridParams, _search, cost_path


@dataclass(frozen=True)
class _State:
    map_id: int
    pos: Pos


def _segment_world(w: WorldModel, kb: KnowledgeBase, map_id: int, pos: Pos) -> WorldModel:
    """A world model for cost-grid search on ``map_id`` at ``pos``."""
    sw = WorldModel(w.character_id, map_id=map_id, pos=pos, perception=w.perception, movement=w.movement)
    if map_id == w.map_id:
        sw.maps = w.maps
        sw.entities = w.entities
    else:
        sw.maps[map_id] = view_from_kb(kb, map_id)
    return sw


def _path_cost(
    w: WorldModel, kb: KnowledgeBase, map_id: int, start: Pos, goal: Pos, params: CostGridParams
) -> tuple[list[Pos], int] | None:
    if start == goal:
        return [], 0
    sw = _segment_world(w, kb, map_id, start)
    return _search(sw, goal, params)


def _landmarks(kb: KnowledgeBase, w: WorldModel, dest_map: int, dest: Pos) -> dict[int, set[Pos]]:
    """Door cells and the destination on each map."""
    out: dict[int, set[Pos]] = {}
    seen_maps = {int(k) for k in kb.maps} | set(w.maps)
    for map_id in seen_maps:
        points: set[Pos] = set()
        for d in iter_doors(kb, map_id):
            points.add((int(d["x"]), int(d["y"])))
        if map_id == w.map_id:
            for p, block in w.view.tiles.items():
                if block in DOORS:
                    points.add(p)
        if map_id == dest_map:
            points.add(dest)
        if points:
            out[map_id] = points
    if dest_map not in out:
        out[dest_map] = {dest}
    return out


def _heuristic(state: _State, goal: _State) -> int:
    if state.map_id != goal.map_id:
        return 0
    return chebyshev(state.pos, goal.pos)


def _waypoint_route(
    w: WorldModel,
    kb: KnowledgeBase,
    dest_map: int,
    dest: Pos,
    params: CostGridParams,
) -> list[_State] | None:
    """Shortest route over known door warps between maps."""
    assert w.map_id is not None and w.pos is not None
    start = _State(w.map_id, w.pos)
    goal = _State(dest_map, dest)
    if start == goal:
        return [start]

    landmarks = _landmarks(kb, w, dest_map, dest)
    doors_by_map: dict[int, dict[Pos, dict]] = {}
    for map_id in landmarks:
        doors_by_map[map_id] = {(int(d["x"]), int(d["y"])): d for d in iter_doors(kb, map_id)}

    frontier: list[tuple[int, int, _State]] = [(0, 0, start)]
    cost: dict[_State, int] = {start: 0}
    came: dict[_State, _State] = {}
    seq = 0
    while frontier:
        _, g, state = heapq.heappop(frontier)
        if g > cost.get(state, 10**9):
            continue
        if state == goal:
            out = [state]
            while out[-1] in came:
                out.append(came[out[-1]])
            return out[::-1]
        for target in landmarks.get(state.map_id, ()):
            if target == state.pos:
                continue
            found = _path_cost(w, kb, state.map_id, state.pos, target, params)
            if found is None:
                continue
            _, seg = found
            nxt = _State(state.map_id, target)
            ng = g + seg
            if ng < cost.get(nxt, 10**9):
                cost[nxt] = ng
                came[nxt] = state
                seq += 1
                heapq.heappush(frontier, (ng + _heuristic(nxt, goal), ng, nxt))
        door = doors_by_map.get(state.map_id, {}).get(state.pos)
        if door and door_warp_known(door):
            landing = _State(int(door["to_map_id"]), (int(door["to_x"]), int(door["to_y"])))
            ng = g
            if ng < cost.get(landing, 10**9):
                cost[landing] = ng
                came[landing] = state
                seq += 1
                heapq.heappush(frontier, (ng + _heuristic(landing, goal), ng, landing))
    return None


def route_first_leg(
    w: WorldModel,
    kb: KnowledgeBase | None,
    dest_map: int,
    dest: Pos,
    params: CostGridParams,
) -> list[Pos] | None:
    """Path on the current map toward ``dest_map:dest``, using the door graph when needed."""
    if w.map_id is None or w.pos is None:
        return None
    if kb is None:
        if w.map_id == dest_map:
            return cost_path(w, dest, params)
        return None
    if w.map_id == dest_map:
        direct = cost_path(w, dest, params)
        if direct is not None:
            return direct
    route = _waypoint_route(w, kb, dest_map, dest, params)
    if route is None or len(route) < 2:
        if w.map_id == dest_map:
            return cost_path(w, dest, params)
        return None
    next_wp = route[1]
    if next_wp.map_id != w.map_id:
        return None
    found = _path_cost(w, kb, w.map_id, w.pos, next_wp.pos, params)
    return found[0] if found else None


def unvisited_doors_on_map(kb: KnowledgeBase | None, w: WorldModel, map_id: int) -> set[Pos]:
    """Doors whose warp destination is not recorded yet."""
    if kb is None:
        return set()
    view = w.view if map_id == w.map_id else view_from_kb(kb, map_id)
    by_pos = {(int(d["x"]), int(d["y"])): d for d in iter_doors(kb, map_id)}
    out: set[Pos] = set()
    for p, block in view.tiles.items():
        if block not in DOORS:
            continue
        record = by_pos.get(p)
        if record is None or not door_warp_known(record):
            out.add(p)
    return out


def doors_goal_path(
    w: WorldModel,
    kb: KnowledgeBase | None,
    params: CostGridParams,
) -> list[Pos] | None:
    """Walk toward an unvisited door first, else the nearest door on this map."""
    assert w.map_id is not None
    unvisited = unvisited_doors_on_map(kb, w, w.map_id)
    doors = {p for p, b in w.view.tiles.items() if b in DOORS}
    targets = unvisited or doors
    if not targets:
        return None
    from .planner import nearest_target

    found = nearest_target(w, targets, params)
    return found[1] if found and found[1] else None
