"""Door and entrance looks for Investigate (A30, A27 entrance marks).

Walk to a minimap entrance mark or an unseen door, read what the cell shows
(``block_type``, ``locked`` from terrain reads, Manual §9.2), and store it on
``kb.entrances`` and the map's door list (PLAYABLE_AGENT_PLAN Knowledge base).
"""

from __future__ import annotations

import math
from typing import Any

def look_key(map_id: int, pos: Pos) -> str:
    return f"look:{map_id}:{pos[0]},{pos[1]}"
from .knowledge_base import KnowledgeBase
from .knowledge_maps import iter_doors, record_locked_door, sync_tiles
from .world import DOORS, Pos, UNKNOWN, VOID, WorldModel, chebyshev

LOOK_GOAL = "investigate:look"

# Obstacles an entrance may sit behind (PLAYABLE_AGENT_PLAN odd-block types).
_BEHIND = frozenset({"water", "bush", "tree", "rock", "mountain", "wall"})


def _cell_key(p: Pos) -> str:
    return f"{p[0]},{p[1]}"


def _tile_from_kb(kb: KnowledgeBase | None, map_id: int, pos: Pos) -> str | None:
    if kb is None:
        return None
    with kb.lock:
        raw = kb.maps.get(str(map_id), {}).get("terrain", {})
    block = raw.get(_cell_key(pos)) if isinstance(raw, dict) else None
    return block if isinstance(block, str) and block not in ("", VOID) else None


def tile_at(w: WorldModel, kb: KnowledgeBase | None, map_id: int, pos: Pos) -> str | None:
    """Best known block type at ``map_id:pos`` from the live view or knowledge base."""
    if w.map_id == map_id:
        block = w.view.tiles.get(pos)
        if block not in (None, UNKNOWN, VOID, ""):
            return block
    return _tile_from_kb(kb, map_id, pos)


def _locked_on_view(w: WorldModel, map_id: int, pos: Pos) -> bool:
    if w.map_id != map_id:
        return False
    return bool(w.view.locked.get(pos))


def door_locked(w: WorldModel, kb: KnowledgeBase | None, map_id: int, pos: Pos) -> bool:
    if _locked_on_view(w, map_id, pos):
        return True
    if kb is None:
        return False
    for d in iter_doors(kb, map_id):
        if int(d["x"]) == pos[0] and int(d["y"]) == pos[1]:
            return bool(d.get("locked"))
    return False


def infer_needs(block: str | None, *, locked: bool) -> str | None:
    if block is None:
        return None
    if locked:
        return "key"
    if block in DOORS:
        return None
    if block == "water":
        return "cross_water"
    if block in _BEHIND:
        return "break"
    return "blocked"


def entrance_was_looked(kb: KnowledgeBase | None, key: str) -> bool:
    if kb is None:
        return False
    with kb.lock:
        row = kb.entrances.get(key)
    return isinstance(row, dict) and bool(row.get("looked"))


def _merge_entrance_row(kb: KnowledgeBase, key: str, patch: dict[str, Any]) -> None:
    with kb.lock:
        row = kb.entrances.setdefault(key, {})
        row.update(patch)


def _mark_door_looked(kb: KnowledgeBase, map_id: int, pos: Pos, patch: dict[str, Any]) -> None:
    with kb.lock:
        doors: list[dict[str, Any]] = kb.maps.setdefault(str(map_id), {}).setdefault("doors", [])
        for d in doors:
            if int(d["x"]) == pos[0] and int(d["y"]) == pos[1]:
                d.update(patch)
                d["looked"] = True
                return
        entry = {"x": pos[0], "y": pos[1], **patch, "looked": True}
        doors.append(entry)
        doors.sort(key=lambda d: (d["x"], d["y"]))


def apply_door_look(kb: KnowledgeBase | None, w: WorldModel, map_id: int, pos: Pos) -> bool:
    """Record what the door or entrance mark shows. True when the look finished."""
    if kb is None:
        return True
    block = tile_at(w, kb, map_id, pos)
    if block is None:
        return False
    locked = door_locked(w, kb, map_id, pos)
    needs = infer_needs(block, locked=locked)
    patch: dict[str, Any] = {"looked": True, "block_type": block}
    if locked:
        patch["locked"] = True
    if needs:
        patch["needs"] = needs
    key = _cell_key(pos)
    with kb.lock:
        is_entrance = key in kb.entrances
    if is_entrance:
        _merge_entrance_row(kb, key, {**patch, "map_id": map_id, "x": pos[0], "y": pos[1]})
    _mark_door_looked(kb, map_id, pos, patch)
    if w.map_id == map_id:
        sync_tiles(kb, map_id, {pos: block})
    if locked and block in DOORS:
        record_locked_door(kb, map_id, pos, block)
    return True


def _sight_range(w: WorldModel, map_id: int, at: Pos) -> int:
    fact = w.zones.get(map_id, {}).get(at)
    bright = fact.brightness if fact is not None else 1.0
    return max(1, min(w.perception, math.ceil(w.perception * bright)))


def _in_sight(w: WorldModel, map_id: int, at: Pos, target: Pos) -> bool:
    return chebyshev(at, target) <= _sight_range(w, map_id, at)


def ready_to_look(w: WorldModel, map_id: int, door_pos: Pos) -> bool:
    """True when standing close enough and the target cell is in readable sight."""
    if w.pos is None or w.map_id != map_id:
        return False
    block = w.view.tiles.get(door_pos)
    if block in (None, UNKNOWN, VOID):
        return False
    here = w.pos
    if not _in_sight(w, map_id, here, door_pos):
        return False
    if block in DOORS:
        return chebyshev(here, door_pos) <= 1
    return here == door_pos or chebyshev(here, door_pos) <= 1


def approach_pos(w: WorldModel, kb: KnowledgeBase | None, map_id: int, door_pos: Pos) -> Pos:
    """The cell Investigate walks toward to look at ``door_pos``."""
    block = tile_at(w, kb, map_id, door_pos)
    if block not in DOORS:
        return door_pos
    if w.map_id == map_id and w.pos and chebyshev(w.pos, door_pos) == 1:
        return w.pos
    if w.map_id == map_id:
        open_adj = [p for p in w.neighbours(door_pos) if w.view.walkable(p)]
        if open_adj and w.pos:
            return min(open_adj, key=lambda p: chebyshev(w.pos, p))
    return door_pos


def iter_unlooked_entrances(kb: KnowledgeBase | None) -> list[tuple[int, Pos, str]]:
    if kb is None:
        return []
    out: list[tuple[int, Pos, str]] = []
    with kb.lock:
        items = list(kb.entrances.items())
    for key, row in items:
        if not isinstance(row, dict) or row.get("looked"):
            continue
        try:
            x, y = (int(p) for p in key.split(",", 1))
        except ValueError:
            continue
        map_id = int(row.get("map_id", 0))
        out.append((map_id, (x, y), key))
    return out
