"""Investigation memory in the per-world knowledge base (A30).

Remembers reads and speech so the interest list does not repeat them. Kept in
top-level knowledge-base keys of its own (PLAYABLE_AGENT_PLAN Knowledge base):

- ``read_cells``: ``{"<map_id>": ["x,y", ...]}``, readable cells whose
  ``Read`` applied;
- ``spoken_npcs``: NPC ids whose ``Say`` applied.

Both grow only with what the world holds, one entry per sign or NPC. Clue
text with place and time lives in ``kb.clues`` (``clues.py``, A32).
"""

from __future__ import annotations

from .knowledge_base import KnowledgeBase
from .world import Pos

READ_CELLS_KEY = "read_cells"
SPOKEN_NPCS_KEY = "spoken_npcs"


def _cell(pos: Pos) -> str:
    return f"{pos[0]},{pos[1]}"


def cell_was_read(kb: KnowledgeBase | None, map_id: int, pos: Pos) -> bool:
    if kb is None:
        return False
    with kb.lock:
        raw = kb.extra.get(READ_CELLS_KEY)
        cells = raw.get(str(map_id)) if isinstance(raw, dict) else None
        return isinstance(cells, list) and _cell(pos) in cells


def mark_cell_read(kb: KnowledgeBase | None, map_id: int, pos: Pos) -> None:
    if kb is None:
        return
    with kb.lock:
        raw = kb.extra.get(READ_CELLS_KEY)
        if not isinstance(raw, dict):
            raw = kb.extra[READ_CELLS_KEY] = {}
        cells = raw.get(str(map_id))
        if not isinstance(cells, list):
            cells = raw[str(map_id)] = []
        if _cell(pos) not in cells:
            cells.append(_cell(pos))


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
        raw = kb.extra.get(SPOKEN_NPCS_KEY)
        if not isinstance(raw, list):
            raw = kb.extra[SPOKEN_NPCS_KEY] = []
        if npc_id not in raw:
            raw.append(npc_id)
