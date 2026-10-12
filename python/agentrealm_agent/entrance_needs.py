"""What a level entrance needs before it is entered (A147).

A clue may tie a level to an item or a capability ("Sandals get you
through"). The planner reads the clue (every clue is in its prompt) and
records the need with its reply's ``entrance_needs`` key; the need is kept
on the entrance's ``kb.entrances`` row as ``needs`` (``clue`` keeps the
planner's reason), the same field a look files on a locked door.

One rule then holds for every way in: an entrance whose need is not met is
not entered (``unmet_need``). A need is met when a carried, armed or worn
supply is the item, or has the capability (a break capability, or
``fords``). The one exception is the item lying in sight: Level takes it on
the way (its prerequisite stop, A71). Otherwise a reply's ``enter_level``,
or ``travel`` to that entrance, is left out with why (``refuse_unmet``), and
Level drops an ``enter_level`` that reaches the top: the item comes first.
"""

from __future__ import annotations

import json
from typing import Any

from .healing import supply_matches
from .knowledge_base import KnowledgeBase
from .supplies import break_capabilities, fords
from .travel.knowledge import entrance_from_kb, entrance_key
from .world import Pos, WorldModel

NEEDS = "needs"
CLUE = "clue"  # the planner's reason for a need it recorded


def meets(code: str | None, need: str) -> bool:
    """``code`` is the needed item, or has the needed capability."""
    if not code:
        return False
    return supply_matches(need, code) or need in break_capabilities(code) or (need == "fords" and fords(code))


def need_of(kb: KnowledgeBase | None, map_id: int | None, pos: Pos) -> str | None:
    """The need recorded on the entrance at ``pos`` on ``map_id``, if any."""
    if kb is None or map_id is None:
        return None
    with kb.lock:
        row = kb.entrances.get(entrance_key(map_id, pos))
        need = row.get(NEEDS) if isinstance(row, dict) else None
    return need if isinstance(need, str) and need else None


def carried(w: WorldModel) -> list[str]:
    """Every code held, stowed, armed or worn."""
    codes = [s.code for s in w.held_supplies + w.chest_supplies] + [w.armed_code or ""] + list(w.worn_codes.values())
    return [c for c in codes if c]


def unmet_need(w: WorldModel, kb: KnowledgeBase | None, map_id: int | None, pos: Pos) -> str | None:
    """The entrance's recorded need when nothing carried meets it, else None."""
    need = need_of(kb, map_id, pos)
    if need is None or any(meets(code, need) for code in carried(w)):
        return None
    return need


def in_sight(w: WorldModel, need: str) -> bool:
    """A supply that meets ``need`` lies in sight, not for sale."""
    return any(e.kind == "supply" and e.gem_price is None and meets(e.code, need) for e in w.entities)


def why_refused(need: str, map_id: int, pos: Pos) -> str:
    return (
        f"the level entrance at {map_id}:{pos[0]},{pos[1]} needs {need}, and none is held: get it first "
        "(buy it from a seller in State sellers, follow a clue to where it is made or sold, or explore for one), "
        "then enter"
    )


def entrance_of(op: dict[str, Any], w: WorldModel, kb: KnowledgeBase | None) -> tuple[int, Pos] | None:
    """The entrance an op enters: ``enter_level`` on this map, or ``travel`` to a named entrance."""
    if op.get("op") == "enter_level" and w.map_id is not None:
        return w.map_id, (op["x"], op["y"])
    if op.get("op") == "travel" and op.get("to") == "entrance" and (op.get("x"), op.get("y")) != (0, 0):
        return entrance_from_kb(kb, op.get("map_id"), op["x"], op["y"])
    return None


def refuse_unmet(
    goals: list[dict[str, Any]], w: WorldModel, kb: KnowledgeBase | None
) -> tuple[list[dict[str, Any]], list[str]]:
    """A reply's ops that would enter an entrance whose need is unmet are
    left out, each with why, unless the item lies in sight for Level to take."""
    kept, refused = [], []
    for g in goals:
        where = entrance_of(g, w, kb)
        need = unmet_need(w, kb, *where) if where is not None else None
        if need is not None and not in_sight(w, need):
            refused.append(f"{json.dumps(g, sort_keys=True)} left out: {why_refused(need, *where)}")
            continue
        kept.append(g)
    return kept, refused


def record_needs(raw: Any, kb: KnowledgeBase | None) -> list[str]:
    """Record a reply's ``entrance_needs``: each ``{x, y, map_id, item, why}``
    sets ``needs`` on that known entrance's row (``item`` null clears it).
    Returns why each entry not recorded was refused."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        return ["`entrance_needs` must be a list"]
    refused = []
    for entry in raw:
        coords = isinstance(entry, dict) and all(
            isinstance(entry.get(k), int) and not isinstance(entry.get(k), bool) for k in ("x", "y", "map_id")
        )
        item = entry.get("item") if isinstance(entry, dict) else None
        if not coords or not (item is None or (isinstance(item, str) and item)):
            refused.append(f"entrance_needs entry {entry!r} dropped: it takes x, y, map_id and item (a code, a capability, or null)")
            continue
        key = entrance_key(entry["map_id"], (entry["x"], entry["y"]))
        if kb is None:
            continue
        with kb.lock:
            row = kb.entrances.get(key)
            if not isinstance(row, dict):
                refused.append(f"entrance_needs entry {entry!r} dropped: no known entrance there (name one from level_entrances)")
                continue
            if item is None:
                row.pop(NEEDS, None)
                row.pop(CLUE, None)
                continue
            row[NEEDS] = item
            why = entry.get("why")
            if isinstance(why, str) and why:
                row[CLUE] = why
    return refused
