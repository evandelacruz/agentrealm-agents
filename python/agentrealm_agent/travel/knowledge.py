"""Travel-related knowledge-base sections (A27)."""

from __future__ import annotations

from typing import Any

from ..knowledge_base import KnowledgeBase
from ..world import Pos

SHOPS = "shops"
HUNTING = "hunting"
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
                key = _cell_key((x, y))
                row = kb.entrances.setdefault(key, {})
                row["map_id"] = map_id
                row.setdefault("x", x)
                row.setdefault("y", y)


def record_hunting_zone(kb: KnowledgeBase | None, map_id: int, pos: Pos, ceiling: int | None, *, closed: bool = False) -> None:
    if kb is None:
        return
    entry: dict[str, Any] = {}
    if ceiling is not None:
        entry["strength_ceiling"] = ceiling
    if closed:
        entry["closed"] = True
    with kb.lock:
        hunting = kb.maps.setdefault(str(map_id), {}).setdefault(HUNTING, {})
        prev = hunting.get(_cell_key(pos))
        if isinstance(prev, dict):
            entry = {**prev, **entry}
        hunting[_cell_key(pos)] = entry


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
                if not isinstance(fact, dict) or fact.get("closed"):
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
    if kb is None:
        if map_id is None:
            return None
        return map_id, (x, y)
    key = _cell_key((x, y))
    with kb.lock:
        row = kb.entrances.get(key)
    if isinstance(row, dict) and "map_id" in row:
        try:
            return int(row["map_id"]), (int(row.get("x", x)), int(row.get("y", y)))
        except (TypeError, ValueError):
            pass
    if map_id is not None:
        return map_id, (x, y)
    return None
