"""Door and entrance looks for Investigate (A30, A27 entrance marks).

Walk next to a minimap entrance mark or an unlooked door on any map the
knowledge base knows (through known door warps when needed),
read what the cell shows (``block_type``, ``locked`` from terrain reads,
Manual §9.2), and store it on ``kb.entrances`` and the map's door list
(PLAYABLE_AGENT_PLAN Knowledge base).
"""

from __future__ import annotations

from typing import Any

from .knowledge_base import KnowledgeBase
from .knowledge_maps import is_level_interior, iter_doors, record_locked_door, sync_tiles
from .travel.knowledge import iter_entrances, merge_entrance
from .world import DOORS, UNKNOWN, VOID, Pos, WorldModel, chebyshev


def look_key(map_id: int, pos: Pos) -> str:
    return f"look:{map_id}:{pos[0]},{pos[1]}"


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


def door_locked(w: WorldModel, kb: KnowledgeBase | None, map_id: int, pos: Pos) -> bool:
    if w.map_id == map_id and w.view.locked.get(pos):
        return True
    if kb is None:
        return False
    for d in iter_doors(kb, map_id):
        if int(d["x"]) == pos[0] and int(d["y"]) == pos[1]:
            return bool(d.get("locked"))
    return False


def infer_needs(block: str | None, *, locked: bool) -> str | None:
    """What the cell says it needs. Only ``key`` is sourced: a locked door shows
    ``locked: true`` and takes a key (Manual §9.2, §11). A block never says
    whether it breaks (Manual §11), and "across water" is about the route, not
    the cell, so other block types record ``block_type`` and no ``needs``."""
    if block is not None and locked:
        return "key"
    return None


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
    merge_entrance(kb, map_id, pos, patch)
    if w.map_id == map_id:
        sync_tiles(kb, map_id, {pos: block})
    if block in DOORS:
        if locked:
            record_locked_door(kb, map_id, pos, block)
        _mark_door_looked(kb, map_id, pos, patch)
    return True


def _mark_door_looked(kb: KnowledgeBase, map_id: int, pos: Pos, patch: dict[str, Any]) -> None:
    with kb.lock:
        for d in kb.maps.get(str(map_id), {}).get("doors") or []:
            if int(d["x"]) == pos[0] and int(d["y"]) == pos[1]:
                d.update(patch)
                return


def ready_to_look(w: WorldModel, map_id: int, pos: Pos) -> bool:
    """True when standing on or next to ``pos`` with its block in the view."""
    if w.pos is None or w.map_id != map_id:
        return False
    if w.view.tiles.get(pos) in (None, UNKNOWN, VOID, ""):
        return False
    return chebyshev(w.pos, pos) <= 1


def approach_pos(w: WorldModel, pos: Pos) -> Pos | None:
    """The cell next to ``pos`` Investigate walks to; None when every neighbour is
    known blocked. Any block type is looked at from beside it: a door is a warp,
    and water, wall, tree or rock cannot be stood on (Manual §9.2)."""
    if w.pos is None:
        return None
    if chebyshev(w.pos, pos) == 1:
        return w.pos
    # Seen-walkable first, then unseen: paths may run through fog (README goals).
    walk = [p for p in w.neighbours(pos) if w.view.walkable(p)]
    fog = [p for p in w.neighbours(pos) if w.view.tiles.get(p) in (None, UNKNOWN)]
    cands = walk or fog
    if not cands:
        return None
    here = w.pos
    return min(cands, key=lambda p: (chebyshev(here, p), p))


def iter_unlooked(kb: KnowledgeBase | None, map_id: int) -> list[Pos]:
    """Entrance marks and doors on ``map_id`` not looked at yet. A door whose warp
    is recorded has been visited, so it is skipped."""
    if kb is None:
        return []
    out: set[Pos] = set()
    for mid, pos, row in iter_entrances(kb):
        if mid == map_id and not row.get("looked"):
            out.add(pos)
    for d in iter_doors(kb, map_id):
        if d.get("looked") or "to_map_id" in d:
            continue
        try:
            out.add((int(d["x"]), int(d["y"])))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(out)


def iter_unlooked_targets(kb: KnowledgeBase | None) -> list[tuple[int, Pos]]:
    """Every unlooked entrance mark or door on any map in the knowledge base,
    except level interiors: Level owns their doors (A37)."""
    if kb is None:
        return []
    map_ids: set[int] = set()
    for mid, _pos, _row in iter_entrances(kb):
        map_ids.add(mid)
    with kb.lock:
        for mid_str in kb.maps:
            try:
                map_ids.add(int(mid_str))
            except (TypeError, ValueError):
                continue
    out: list[tuple[int, Pos]] = []
    for mid in sorted(map_ids):
        if is_level_interior(kb, mid):
            continue
        for pos in iter_unlooked(kb, mid):
            out.append((mid, pos))
    return out
