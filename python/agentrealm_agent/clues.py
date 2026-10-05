"""Clue capture and no-LLM clue rules (A32).

Every sign read and helper line heard is stored in the knowledge base with
where and when it was found, and a ``clue`` signal is queued for the
strategist (A35). Without an LLM, clue text biases ``Explore`` toward named
directions and raises named capabilities on odd blocks nearby.
"""

from __future__ import annotations

import re
from typing import Any

from .knowledge_base import KnowledgeBase
from .memory import Memory
from .navigation.planner import nearest_target
from .plan import CAPABILITIES
from .world import Pos, WorldModel, chebyshev

SIGNALS_KEPT = 16  # nothing drains them until the strategist (A35)

# Map clue phrases to a preferred step direction (API: up is decreasing y).
_DIRECTIONS: tuple[tuple[str, tuple[int, int]], ...] = (
    ("northwest", (-1, -1)),
    ("north-east", (1, -1)),
    ("northeast", (1, -1)),
    ("southwest", (-1, 1)),
    ("south-east", (1, 1)),
    ("southeast", (1, 1)),
    ("north", (0, -1)),
    ("south", (0, 1)),
    ("east", (1, 0)),
    ("west", (-1, 0)),
    ("up", (0, -1)),
    ("down", (0, 1)),
    ("left", (-1, 0)),
    ("right", (1, 0)),
)

_WORD = re.compile(r"[a-z]+")


def _clue_rows(kb: KnowledgeBase | None) -> list[dict[str, Any]]:
    if kb is None:
        return []
    with kb.lock:
        return list(kb.clues)


def _already_recorded(kb: KnowledgeBase, row: dict[str, Any]) -> bool:
    """Same kind and place: do not store or signal twice."""
    kind = row.get("kind")
    try:
        map_id = int(row["map_id"])
        x, y = int(row["x"]), int(row["y"])
    except (KeyError, TypeError, ValueError):
        return False
    for existing in kb.clues:
        if existing.get("kind") != kind:
            continue
        try:
            if int(existing.get("map_id")) == map_id and int(existing.get("x")) == x and int(existing.get("y")) == y:
                return True
        except (TypeError, ValueError):
            continue
    return False


def record_clue(
    kb: KnowledgeBase | None,
    m: Memory | None,
    *,
    kind: str,
    text: str,
    map_id: int,
    x: int,
    y: int,
    tick: int,
) -> bool:
    """Append one clue and queue a strategist trigger. Returns True when new."""
    cleaned = (text or "").strip()
    if kb is None or not cleaned:
        return False
    row = {"kind": kind, "text": cleaned, "map_id": map_id, "x": x, "y": y, "tick": tick}
    with kb.lock:
        if _already_recorded(kb, row):
            return False
        kb.clues.append(row)
    if m is not None:
        _queue_clue_signal(m, row)
    return True


def _queue_clue_signal(m: Memory, row: dict[str, Any]) -> None:
    m.clue_signals.append({"trigger": "clue", **row})
    del m.clue_signals[:-SIGNALS_KEPT]


def note_read_clue(
    kb: KnowledgeBase | None,
    m: Memory | None,
    result: dict,
    map_id: int,
    pos: Pos,
    tick: int,
    *,
    kind: str = "sign",
) -> None:
    """Store text from an applied ``Read`` intent result (API rules § Read)."""
    if result.get("outcome") != "applied":
        return
    text = result.get("text")
    if not isinstance(text, str):
        return
    record_clue(kb, m, kind=kind, text=text, map_id=map_id, x=pos[0], y=pos[1], tick=tick)


def note_spoken_clue(
    kb: KnowledgeBase | None,
    m: Memory | None,
    w: WorldModel,
    event: dict,
    tick: int,
) -> None:
    """Store a helper reply from ``SpokenTo`` (GAME_NOTES Speech)."""
    if event.get("kind") != "SpokenTo":
        return
    if event.get("speaker_kind", "character") != "npc":
        return
    text = event.get("text")
    if not isinstance(text, str) or not text.strip():
        return
    try:
        speaker_id = int(event["speaker_id"])
    except (KeyError, TypeError, ValueError):
        return
    pos = w.pos
    map_id = w.map_id
    for ent in w.entities:
        if ent.id == speaker_id and ent.kind == "npc":
            pos, map_id = ent.pos, w.map_id
            break
    if map_id is None or pos is None:
        return
    record_clue(kb, m, kind="npc", text=text, map_id=map_id, x=pos[0], y=pos[1], tick=tick)


def clue_texts(kb: KnowledgeBase | None) -> list[str]:
    return [(c.get("text") or "").lower() for c in _clue_rows(kb)]


def direction_preference(kb: KnowledgeBase | None) -> tuple[float, float] | None:
    """Unit-ish vector of summed direction hints from all clues, or None."""
    dx = dy = 0.0
    for text in clue_texts(kb):
        for word in _WORD.findall(text):
            for name, (vx, vy) in _DIRECTIONS:
                if word == name or word.replace("-", "") == name.replace("-", ""):
                    dx += vx
                    dy += vy
                    break
    if dx == dy == 0:
        return None
    return dx, dy


def order_explore_targets(w: WorldModel, targets: set[Pos], kb: KnowledgeBase | None) -> list[Pos]:
    """Frontier cells sorted so clue directions come before plain distance."""
    if w.pos is None or not targets:
        return sorted(targets)
    pref = direction_preference(kb)
    if pref is None:
        return sorted(targets, key=lambda p: chebyshev(w.pos, p))
    pdx, pdy = pref

    def key(p: Pos) -> tuple[float, int]:
        ox, oy = p[0] - w.pos[0], p[1] - w.pos[1]
        align = ox * pdx + oy * pdy
        return (-align, chebyshev(w.pos, p))

    return sorted(targets, key=key)


def nearest_explore_target(
    w: WorldModel, targets: set[Pos], params, kb: KnowledgeBase | None
):
    """Like ``nearest_target``, but tries clue-biased frontiers first (A32)."""
    order = order_explore_targets(w, targets, kb)
    return nearest_target(w, targets, params, try_order=order)


# Words helpers and signs use for a capability (GAME_NOTES Supplies; manual §11).
_CAPABILITY_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("burn", ("burn", "matches", "torch")),
    ("blast", ("blast", "bomb")),
    ("smash", ("smash", "mallet")),
    ("cut", ("cut", "knife", "sword")),
    ("chop", ("chop",)),
)


def _capabilities_in_text(text: str) -> set[str]:
    words = set(_WORD.findall(text))
    out: set[str] = set()
    for cap, names in _CAPABILITY_WORDS:
        if cap in words or any(n in words for n in names):
            out.add(cap)
    return out


def mentioned_capabilities(kb: KnowledgeBase | None, map_id: int | None = None) -> set[str]:
    """Capabilities named in clue text; optional ``map_id`` limits to that map."""
    out: set[str] = set()
    for clue in _clue_rows(kb):
        if map_id is not None:
            try:
                if int(clue.get("map_id")) != map_id:
                    continue
            except (TypeError, ValueError):
                continue
        out |= _capabilities_in_text((clue.get("text") or "").lower())
    return out


def capability_priority(cap: str, kb: KnowledgeBase | None, map_id: int | None) -> int:
    """Lower sorts earlier when picking a break capability (A32)."""
    return 0 if cap in mentioned_capabilities(kb, map_id) else 1


def clue_allows_tools(kb: KnowledgeBase | None, map_id: int, clue_boost: float) -> bool:
    """Consumable tools on odd blocks when a clue names the block type or a capability."""
    if clue_boost > 0:
        return True
    return bool(mentioned_capabilities(kb, map_id))
