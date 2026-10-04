"""Travel-related knowledge-base sections (A27)."""

from __future__ import annotations

from typing import Any

from ..knowledge_base import KnowledgeBase
from ..knowledge_maps import HUNTING
from ..world import Pos

SHOPS = "shops"
TOWN = "town"


def _cell_key(p: Pos) -> str:
    return f"{p[0]},{p[1]}"


def sync_town(kb: KnowledgeBase, town: dict | None) -> None:
    if town is None:
        return
    try:
        entry = {
            "map_id": int(town["map_id"]),
            "x": int(town["x"]),
            "y": int(town["y"]),
        }
    except (KeyError, TypeError, ValueError):
        return
    with kb.lock:
        kb.extra[TOWN] = entry


def town_from_kb(kb: KnowledgeBase | None) -> tuple[int, Pos] | None:
    if kb is None:
        return None
    with kb.lock:
        raw = kb.extra.get(TOWN)
    if not isinstance(raw, dict):
        return None
    try:
        return int(raw["map_id"]), (int(raw["x"]), int(raw["y"]))
    except (KeyError, TypeError, ValueError):
        return None


def entrance_key(map_id: int, p: Pos) -> str:
    """``kb.entrances`` key: map and cell, so the same x,y on two maps stay apart."""
    return f"{map_id}:{p[0]},{p[1]}"


def iter_entrances(kb: KnowledgeBase | None) -> list[tuple[int, Pos, dict[str, Any]]]:
    """Every entrance row as ``(map_id, pos, row)``; the row is the live dict."""
    if kb is None:
        return []
    out: list[tuple[int, Pos, dict[str, Any]]] = []
    with kb.lock:
        for key, row in kb.entrances.items():
            if not isinstance(row, dict):
                continue
            try:
                mid, cell = key.split(":", 1)
                x, y = (int(p) for p in cell.split(",", 1))
                out.append((int(mid), (x, y), row))
            except ValueError:
                continue
    return out


def merge_entrance(kb: KnowledgeBase, map_id: int, p: Pos, patch: dict[str, Any]) -> bool:
    """Merge ``patch`` into the entrance row at ``map_id:p``. False when there is none."""
    with kb.lock:
        row = kb.entrances.get(entrance_key(map_id, p))
        if not isinstance(row, dict):
            return False
        row.update(patch)
        return True


def sync_entrances(kb: KnowledgeBase, minimap: dict) -> None:
    """Merge minimap entrance marks into ``kb.entrances`` (Manual minimap)."""
    with kb.lock:
        for m in minimap.get("maps") or []:
            if not isinstance(m, dict):
                continue
            try:
                map_id = int(m["map_id"])
            except (KeyError, TypeError, ValueError):
                continue
            for ent in m.get("entrances") or []:
                if not isinstance(ent, dict):
                    continue
                try:
                    x, y = int(ent["x"]), int(ent["y"])
                except (KeyError, TypeError, ValueError):
                    continue
                row = kb.entrances.setdefault(entrance_key(map_id, (x, y)), {})
                row["map_id"] = map_id
                row.setdefault("x", x)
                row.setdefault("y", y)


def iter_hunting_cells(kb: KnowledgeBase | None) -> list[tuple[int, Pos, dict[str, Any]]]:
    if kb is None:
        return []
    out: list[tuple[int, Pos, dict[str, Any]]] = []
    with kb.lock:
        for mid, entry in kb.maps.items():
            if not isinstance(entry, dict):
                continue
            try:
                map_id = int(mid)
            except ValueError:
                continue
            hunting = entry.get(HUNTING)
            if not isinstance(hunting, dict):
                continue
            for key, fact in hunting.items():
                # A closed cell stays a candidate while its ceiling is known: the
                # strength bracket rules it out, and a loadout change reopens it.
                if not isinstance(fact, dict) or (fact.get("closed") and "strength_ceiling" not in fact):
                    continue
                try:
                    x, y = (int(p) for p in key.split(",", 1))
                except ValueError:
                    continue
                out.append((map_id, (x, y), fact))
    return out


def record_shop_cell(kb: KnowledgeBase | None, map_id: int, pos: Pos) -> None:
    if kb is None:
        return
    with kb.lock:
        shops = kb.maps.setdefault(str(map_id), {}).setdefault(SHOPS, {})
        shops[_cell_key(pos)] = {}


def iter_shop_cells(kb: KnowledgeBase | None) -> list[tuple[int, Pos]]:
    if kb is None:
        return []
    out: list[tuple[int, Pos]] = []
    with kb.lock:
        for mid, entry in kb.maps.items():
            if not isinstance(entry, dict):
                continue
            try:
                map_id = int(mid)
            except ValueError:
                continue
            shops = entry.get(SHOPS)
            if not isinstance(shops, dict):
                continue
            for key in shops:
                try:
                    x, y = (int(p) for p in key.split(",", 1))
                except ValueError:
                    continue
                out.append((map_id, (x, y)))
    return out


def entrance_from_kb(kb: KnowledgeBase | None, map_id: int | None, x: int, y: int) -> tuple[int, Pos] | None:
    if map_id is not None:
        return map_id, (x, y)
    # No map given: take the one mark at x,y, if only one map has a mark there.
    maps = {mid for mid, pos, _ in iter_entrances(kb) if pos == (x, y)}
    if len(maps) == 1:
        return maps.pop(), (x, y)
    return None
