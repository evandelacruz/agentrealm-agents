"""Investigation memory in the per-world knowledge base (A30).

Remembers reads, speech, and door looks so the interest list does not repeat
work. Clue text with place and time is A32.
"""

from __future__ import annotations

from typing import Any

from .knowledge_base import KnowledgeBase
from .knowledge_maps import _cell_key, _entry, _parse_cell
from .world import Pos

READ_KEY = "read"
LOOKED_KEY = "looked"
SPOKEN_NPCS_KEY = "spoken_npcs"
READ_SUPPLIES_KEY = "read_supplies"


def _read_list(entry: dict[str, Any]) -> list[str]:
    raw = entry.get(READ_KEY)
    return list(raw) if isinstance(raw, list) else []


def _looked_list(entry: dict[str, Any]) -> list[str]:
    raw = entry.get(LOOKED_KEY)
    return list(raw) if isinstance(raw, list) else []


def cell_was_read(kb: KnowledgeBase | None, map_id: int, pos: Pos) -> bool:
    if kb is None:
        return False
    key = _cell_key(pos)
    with kb.lock:
        entry = kb.maps.get(str(map_id), {})
        return key in _read_list(entry)


def mark_cell_read(kb: KnowledgeBase | None, map_id: int, pos: Pos) -> None:
    if kb is None:
        return
    key = _cell_key(pos)
    with kb.lock:
        entry = _entry(kb, map_id)
        reads = entry.setdefault(READ_KEY, [])
        if key not in reads:
            reads.append(key)


def cell_was_looked(kb: KnowledgeBase | None, map_id: int, pos: Pos) -> bool:
    if kb is None:
        return False
    key = _cell_key(pos)
    with kb.lock:
        entry = kb.maps.get(str(map_id), {})
        return key in _looked_list(entry)


def mark_cell_looked(kb: KnowledgeBase | None, map_id: int, pos: Pos) -> None:
    if kb is None:
        return
    key = _cell_key(pos)
    with kb.lock:
        entry = _entry(kb, map_id)
        looked = entry.setdefault(LOOKED_KEY, [])
        if key not in looked:
            looked.append(key)


def spoken_npc_ids(kb: KnowledgeBase | None) -> set[int]:
    if kb is None:
        return set()
    with kb.lock:
        raw = kb.extra.get(SPOKEN_NPCS_KEY, [])
        if not isinstance(raw, list):
            return set()
        out: set[int] = set()
        for v in raw:
            try:
                out.add(int(v))
            except (TypeError, ValueError):
                continue
        return out


def mark_npc_spoken(kb: KnowledgeBase | None, npc_id: int) -> None:
    if kb is None:
        return
    with kb.lock:
        raw = kb.extra.setdefault(SPOKEN_NPCS_KEY, [])
        if not isinstance(raw, list):
            raw = []
            kb.extra[SPOKEN_NPCS_KEY] = raw
        if npc_id not in raw:
            raw.append(npc_id)


def supply_was_read(kb: KnowledgeBase | None, supply_id: int) -> bool:
    if kb is None:
        return False
    with kb.lock:
        raw = kb.extra.get(READ_SUPPLIES_KEY, [])
        if not isinstance(raw, list):
            return False
        return supply_id in raw


def mark_supply_read(kb: KnowledgeBase | None, supply_id: int) -> None:
    if kb is None:
        return
    with kb.lock:
        raw = kb.extra.setdefault(READ_SUPPLIES_KEY, [])
        if not isinstance(raw, list):
            raw = []
            kb.extra[READ_SUPPLIES_KEY] = raw
        if supply_id not in raw:
            raw.append(supply_id)


def iter_read_cells(kb: KnowledgeBase | None, map_id: int) -> set[Pos]:
    if kb is None:
        return set()
    with kb.lock:
        entry = kb.maps.get(str(map_id), {})
        return {_parse_cell(k) for k in _read_list(entry)}
