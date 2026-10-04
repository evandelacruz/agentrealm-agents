"""Map terrain and door warps in the per-world knowledge base (A26).

Shape, under ``kb.maps["<map_id>"]``:

- ``"terrain"``: ``{"x,y": block_type}`` for every revealed tile.
- ``"doors"``: ``[{"x", "y", "block_type"}]`` sorted by cell, plus
  ``"to_map_id"``, ``"to_x"``, ``"to_y"`` once a step onto it has been
  observed to land somewhere, and ``"locked": true`` once a Step onto it
  was refused ``door_locked`` (A14). The API never names a door's destination.
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


def _entry(kb: KnowledgeBase, map_id: int) -> dict[str, Any]:
    """The map's entry, created if missing. Caller holds ``kb.lock``."""
    entry = kb.maps.setdefault(str(map_id), {})
    entry.setdefault(TERRAIN, {})
    entry.setdefault(DOORS_KEY, [])
    return entry


def sync_tiles(kb: KnowledgeBase, map_id: int, tiles: dict[Pos, str]) -> None:
    """Merge these tiles of one map into the knowledge base, adding any new door."""
    with kb.lock:
        entry = _entry(kb, map_id)
        terrain: dict[str, str] = entry[TERRAIN]
        doors: list[dict[str, Any]] = entry[DOORS_KEY]
        known = {(int(d["x"]), int(d["y"])) for d in doors}
        added = False
        for p, block in tiles.items():
            terrain[_cell_key(p)] = block
            if block in DOORS and p not in known:
                doors.append({"x": p[0], "y": p[1], "block_type": block})
                known.add(p)
                added = True
        if added:
            doors.sort(key=lambda d: (d["x"], d["y"]))


def sync_map_from_view(kb: KnowledgeBase, map_id: int, view: MapView) -> None:
    """Merge a whole map view into the knowledge base."""
    sync_tiles(kb, map_id, view.tiles)


def sync_world_maps(kb: KnowledgeBase, w: WorldModel) -> None:
    """Merge every map this character has seen into the knowledge base."""
    for map_id, view in w.maps.items():
        sync_map_from_view(kb, map_id, view)


def view_from_kb(kb: KnowledgeBase, map_id: int) -> MapView:
    """Build a map view from stored terrain (empty when unknown)."""
    with kb.lock:
        entry = kb.maps.get(str(map_id), {})
        raw = dict(entry.get(TERRAIN) or {})
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
    with kb.lock:
        doors: list[dict[str, Any]] = _entry(kb, from_map)[DOORS_KEY]
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


def record_locked_door(kb: KnowledgeBase, map_id: int, pos: Pos, block_type: str) -> None:
    """Mark a door locked after a ``door_locked`` Step rejection (A14)."""
    with kb.lock:
        doors: list[dict[str, Any]] = _entry(kb, map_id)[DOORS_KEY]
        for d in doors:
            if int(d["x"]) == pos[0] and int(d["y"]) == pos[1]:
                d["locked"] = True
                return
        doors.append({"x": pos[0], "y": pos[1], "block_type": block_type, "locked": True})
        doors.sort(key=lambda d: (d["x"], d["y"]))


def door_warp_known(d: dict[str, Any]) -> bool:
    return "to_map_id" in d and "to_x" in d and "to_y" in d


def iter_doors(kb: KnowledgeBase, map_id: int) -> list[dict[str, Any]]:
    with kb.lock:
        entry = kb.maps.get(str(map_id), {})
        return list(entry.get(DOORS_KEY) or [])
