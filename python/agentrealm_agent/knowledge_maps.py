"""Map terrain and door warps in the per-world knowledge base (A26).

Door records live under ``kb.maps[<map_id>]["doors"]``; revealed tiles under
``["terrain"]`` as ``"x,y"`` keys. See ``tests/test_knowledge_base.py`` for
the on-disk shape.
"""

from __future__ import annotations

from typing import Any

from .knowledge_base import KnowledgeBase
from .world import DOORS, MapView, Pos, WorldModel

TERRAIN = "terrain"
DOORS_KEY = "doors"


def _cell_key(p: Pos) -> str:
    return f"{p[0]},{p[1]}"


def _parse_cell(key: str) -> Pos:
    x, y = key.split(",", 1)
    return int(x), int(y)


def map_entry(kb: KnowledgeBase, map_id: int) -> dict[str, Any]:
    with kb.lock:
        entry = kb.maps.setdefault(str(map_id), {})
        entry.setdefault(TERRAIN, {})
        entry.setdefault(DOORS_KEY, [])
        return entry


def sync_map_from_view(kb: KnowledgeBase, map_id: int, view: MapView) -> None:
    """Merge a live map view into the knowledge base."""
    entry = map_entry(kb, map_id)
    terrain: dict[str, str] = entry[TERRAIN]
    doors: list[dict[str, Any]] = entry[DOORS_KEY]
    door_index = {(int(d["x"]), int(d["y"])): d for d in doors}
    with kb.lock:
        for p, block in view.tiles.items():
            terrain[_cell_key(p)] = block
            if block in DOORS:
                door_index.setdefault(
                    p,
                    {"x": p[0], "y": p[1], "block_type": block},
                )
        entry[DOORS_KEY] = sorted(door_index.values(), key=lambda d: (d["x"], d["y"]))


def sync_world_maps(kb: KnowledgeBase, w: WorldModel) -> None:
    """Merge every map this character has seen into the knowledge base."""
    for map_id, view in w.maps.items():
        sync_map_from_view(kb, map_id, view)


def view_from_kb(kb: KnowledgeBase, map_id: int) -> MapView:
    """Build a map view from stored terrain (empty when unknown)."""
    with kb.lock:
        entry = kb.maps.get(str(map_id), {})
        raw = entry.get(TERRAIN) or {}
    view = MapView()
    for key, block in raw.items():
        view.tiles[_parse_cell(key)] = block
    return view


def record_warp(
    kb: KnowledgeBase,
    from_map: int,
    from_pos: Pos,
    block_type: str,
    to_map: int,
    to_pos: Pos,
) -> None:
    """Record where stepping onto a door landed (A26)."""
    entry = map_entry(kb, from_map)
    doors: list[dict[str, Any]] = entry[DOORS_KEY]
    with kb.lock:
        for d in doors:
            if int(d["x"]) == from_pos[0] and int(d["y"]) == from_pos[1]:
                d["block_type"] = block_type
                d["to_map_id"] = to_map
                d["to_x"] = to_pos[0]
                d["to_y"] = to_pos[1]
                return
        doors.append(
            {
                "x": from_pos[0],
                "y": from_pos[1],
                "block_type": block_type,
                "to_map_id": to_map,
                "to_x": to_pos[0],
                "to_y": to_pos[1],
            }
        )
        doors.sort(key=lambda d: (d["x"], d["y"]))


def door_warp_known(d: dict[str, Any]) -> bool:
    return "to_map_id" in d and "to_x" in d and "to_y" in d


def iter_doors(kb: KnowledgeBase, map_id: int) -> list[dict[str, Any]]:
    with kb.lock:
        entry = kb.maps.get(str(map_id), {})
        return list(entry.get(DOORS_KEY) or [])
