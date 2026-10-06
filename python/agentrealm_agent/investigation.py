"""Investigation memory in the per-world knowledge base (A30).

Remembers reads and speech so a ``read`` or ``say`` op is not repeated. Kept in
top-level knowledge-base keys of its own (PLAYABLE_AGENT_PLAN Knowledge base):

- ``read_cells``: ``{"<map_id>": ["x,y", ...]}``, readable cells whose
  ``Read`` applied;
- ``spoken_npcs``: NPC ids whose ``Say`` applied.

Both grow only with what the world holds, one entry per sign or NPC. Clue
text with place and time lives in ``kb.clues`` (``clues.py``, A32).
"""

from __future__ import annotations

import math

from .knowledge_base import KnowledgeBase
from .world import Pos, WorldModel, chebyshev

READ_CELLS_KEY = "read_cells"
SPOKEN_NPCS_KEY = "spoken_npcs"
# Say reaches an NPC this many blocks away.
SPEECH_RANGE = 25
# A Read or Say refused this many times ends its op (Investigate, A30).
MAX_REJECTIONS = 3


def read_key(map_id: int, pos: Pos) -> str:
    return f"read:{map_id}:{pos[0]},{pos[1]}"


def say_key(npc_id: int) -> str:
    return f"say:{npc_id}"


def read_supply_key(supply_id: int) -> str:
    return f"read_supply:{supply_id}"


def sight_range(w: WorldModel, map_id: int, at: Pos) -> int:
    """Readable sight: perception × zone brightness at the stand, capped at perception.

    GAME_NOTES Light (Guide, The world model). Carried light is not counted yet.
    """
    fact = w.zones.get(map_id, {}).get(at)
    bright = fact.brightness if fact is not None else 1.0
    return max(1, min(w.perception, math.ceil(w.perception * bright)))


def in_sight(w: WorldModel, map_id: int, at: Pos, target: Pos) -> bool:
    return chebyshev(at, target) <= sight_range(w, map_id, at)


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
