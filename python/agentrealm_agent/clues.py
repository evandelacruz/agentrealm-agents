"""Clue capture and no-LLM clue rules (A32).

Every sign or scroll read and helper line heard is stored in the knowledge base with
where and when it was found, and a ``clue`` signal is queued for the
strategist (A35). Without an LLM, one simple rule uses the text: a recent
clue on this map that names a direction steers ``Explore`` toward frontiers
on that side of where the clue was found.

A row in ``kb.clues`` is ``{kind, text, map_id, x, y, tick}``, plus
``speaker_id`` for a helper line or ``supply_id`` for a scroll. ``kind`` is
``"sign"`` (cell is the sign), ``"scroll"`` (cell is the scroll on the ground,
else where we stood; A56) or ``"npc"`` (cell is the helper when in sight, else
where we stood: ``Say`` reaches 25 blocks, further than sight). A sign is
stored once per cell, a scroll once per supply, and a helper line once per
speaker and text.
"""

from __future__ import annotations

import re
from typing import Any

from .knowledge_base import KnowledgeBase
from .memory import Memory
from .navigation.planner import CostGridParams, nearest_target
from .world import Pos, WorldModel

SIGNALS_KEPT = 16  # newest kept until the strategist drains them (A35)

# A direction clue steers Explore for this many game ticks after it was found
# (5 minutes at 10 Hz), and only on the map it was found on.
DIRECTION_TICKS = 3000

# Clue words for a step direction (API: up is decreasing y). Text is split
# into words, so "north-west" counts as north plus west.
_DIRECTIONS: dict[str, tuple[int, int]] = {
    "north": (0, -1),
    "south": (0, 1),
    "east": (1, 0),
    "west": (-1, 0),
    "northeast": (1, -1),
    "northwest": (-1, -1),
    "southeast": (1, 1),
    "southwest": (-1, 1),
    "up": (0, -1),
    "down": (0, 1),
    "left": (-1, 0),
    "right": (1, 0),
}

_WORD = re.compile(r"[a-z]+")


# --- Capture -----------------------------------------------------------------


def _dedup_key(row: dict[str, Any]) -> tuple:
    """A helper line is the same clue when the same speaker says the same text; a scroll, the same
    supply; a sign, at the same cell."""
    if row.get("speaker_id") is not None:
        return ("npc", row.get("speaker_id"), row.get("text"))
    if row.get("supply_id") is not None:
        return ("scroll", row.get("supply_id"))
    return (row.get("kind"), row.get("map_id"), row.get("x"), row.get("y"))


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
    speaker_id: int | None = None,
    supply_id: int | None = None,
) -> bool:
    """Append one clue and queue a strategist trigger. Returns True when new."""
    cleaned = (text or "").strip()
    if kb is None or not cleaned:
        return False
    row: dict[str, Any] = {"kind": kind, "text": cleaned, "map_id": map_id, "x": x, "y": y, "tick": tick}
    if speaker_id is not None:
        row["speaker_id"] = speaker_id
    if supply_id is not None:
        row["supply_id"] = supply_id
    key = _dedup_key(row)
    with kb.lock:
        if any(_dedup_key(existing) == key for existing in kb.clues):
            return False
        kb.clues.append(row)
    if m is not None:
        m.clue_signals.append({"trigger": "clue", **row})
        del m.clue_signals[:-SIGNALS_KEPT]
    return True


def note_read_clue(
    kb: KnowledgeBase | None, m: Memory | None, result: dict, map_id: int, pos: Pos, tick: int
) -> None:
    """Store the sign text from an applied ``Read {kind: block}`` result (API rules § Read)."""
    if result.get("outcome") != "applied":
        return
    text = result.get("text")
    if isinstance(text, str):
        record_clue(kb, m, kind="sign", text=text, map_id=map_id, x=pos[0], y=pos[1], tick=tick)


def note_scroll_clue(
    kb: KnowledgeBase | None, m: Memory | None, w: WorldModel, result: dict, supply_id: int
) -> None:
    """Store the scroll text from an applied ``Read {kind: supply}`` result (A56).

    The cell is the scroll's when it lies on the ground in sight, else where we
    stood (a carried scroll).
    """
    if result.get("outcome") != "applied" or w.map_id is None:
        return
    text = result.get("text")
    if not isinstance(text, str):
        return
    pos = next((e.pos for e in w.entities if e.id == supply_id and e.kind == "supply"), w.pos)
    if pos is None:
        return
    tick = int(result.get("tick", w.tick))
    record_clue(
        kb, m, kind="scroll", text=text, map_id=w.map_id, x=pos[0], y=pos[1], tick=tick, supply_id=supply_id
    )


def note_spoken_clue(
    kb: KnowledgeBase | None, m: Memory | None, w: WorldModel, event: dict, tick: int
) -> None:
    """Store a helper reply from ``SpokenTo`` (GAME_NOTES NPCs, signs and scrolls)."""
    if event.get("kind") != "SpokenTo" or event.get("speaker_kind", "character") != "npc":
        return
    text = event.get("text")
    if not isinstance(text, str) or not text.strip():
        return
    try:
        speaker_id = int(event["speaker_id"])
    except (KeyError, TypeError, ValueError):
        return
    if w.map_id is None:
        return
    # The helper's cell when in sight; else where we stood when we heard it.
    pos = next((e.pos for e in w.entities if e.id == speaker_id and e.kind == "npc"), w.pos)
    if pos is None:
        return
    record_clue(
        kb, m, kind="npc", text=text, map_id=w.map_id, x=pos[0], y=pos[1], tick=tick, speaker_id=speaker_id
    )


# --- Reading clues back ------------------------------------------------------


def clues_on_map(kb: KnowledgeBase | None, map_id: int) -> list[dict[str, Any]]:
    """Clue rows found on ``map_id``. Clues on other maps never count."""
    if kb is None:
        return []
    with kb.lock:
        rows = list(kb.clues)
    out = []
    for row in rows:
        try:
            if int(row.get("map_id")) == map_id:
                out.append(row)
        except (TypeError, ValueError):
            continue
    return out


def _clue_cell(row: dict[str, Any]) -> Pos | None:
    try:
        return int(row["x"]), int(row["y"])
    except (KeyError, TypeError, ValueError):
        return None


# --- Rule 1: a direction clue steers Explore ---------------------------------


def direction_of(text: str) -> tuple[int, int] | None:
    """Summed direction words in one clue text, or None when it names none or they cancel."""
    dx = dy = 0
    for word in _WORD.findall(text.lower()):
        vx, vy = _DIRECTIONS.get(word, (0, 0))
        dx, dy = dx + vx, dy + vy
    return None if dx == dy == 0 else (dx, dy)


def direction_hint(w: WorldModel, kb: KnowledgeBase | None) -> tuple[Pos, tuple[int, int]] | None:
    """The newest direction clue on this map from the last ``DIRECTION_TICKS``: (where found, direction)."""
    if w.map_id is None:
        return None
    best: tuple[int, Pos, tuple[int, int]] | None = None
    for row in clues_on_map(kb, w.map_id):
        tick, cell = row.get("tick"), _clue_cell(row)
        if not isinstance(tick, int) or cell is None or not 0 <= w.tick - tick <= DIRECTION_TICKS:
            continue
        direction = direction_of(row.get("text") or "")
        if direction is not None and (best is None or tick > best[0]):
            best = (tick, cell, direction)
    return None if best is None else (best[1], best[2])


def on_hint_side(hint: tuple[Pos, tuple[int, int]], p: Pos) -> bool:
    """``p`` lies on the side of the clue cell its direction names."""
    (cx, cy), (dx, dy) = hint
    return (p[0] - cx) * dx + (p[1] - cy) * dy > 0


def nearest_explore_target(
    w: WorldModel, targets: set[Pos], params: CostGridParams | None, kb: KnowledgeBase | None
) -> tuple[Pos, list[Pos]] | None:
    """Explore frontier to walk to: the nearest one on the side a recent clue names, else the nearest.

    Two ``nearest_target`` searches at most, over disjoint target sets, so the
    clue costs no more A* than plain nearest-frontier choice.
    """
    hint = direction_hint(w, kb)
    if hint is None:
        return nearest_target(w, targets, params)
    ahead = {p for p in targets if on_hint_side(hint, p)}
    if ahead:
        found = nearest_target(w, ahead, params)
        if found is not None:
            return found
    rest = targets - ahead
    return nearest_target(w, rest, params) if rest else None
