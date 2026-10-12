"""What a level entrance needs before it is entered (A147).

An entrance row in ``kb.entrances`` holds two needs, each with one owner:

- ``needs``: what a look filed (Investigate, A30: ``key`` on a locked
  door). Only a look writes or clears it (M9's ``LOOK_FIELDS``).
- ``clue_needs``: what a clue ties to the level ("Sandals get you
  through"), with the planner's reason in ``clue_needs_why``. Only the
  planner writes or clears it, with its reply's ``entrance_needs`` key
  (``record_needs``); every clue is in its prompt, and no code reads the
  clue text. A look's clear or refile leaves it as it is.

One rule then holds for every way in: an entrance is not entered while
either need is unmet (``unmet_need``). A need is met when a carried, armed
or worn supply is the item, or has the capability (a break capability, or
``fords``). A reply's ``enter_level`` or ``travel`` to such an entrance is
left out with why (``refuse_unmet``); the one exception is an
``enter_level`` whose item lies in sight, which Level takes on the way (its
prerequisite stop, A71). Level drops an ``enter_level`` and Travel a
``travel`` that would step onto such an entrance.
"""

from __future__ import annotations

import json
from typing import Any

from .healing import supply_matches
from .knowledge_base import KnowledgeBase
from .supplies import break_capabilities, fords
from .travel.knowledge import entrance_from_kb, entrance_key
from .world import Entity, Pos, WorldModel

NEEDS = "needs"  # a look's (A30)
CLUE_NEEDS = "clue_needs"  # the planner's, from a clue
CLUE_WHY = "clue_needs_why"  # the planner's reason for it


def meets(code: str | None, need: str) -> bool:
    """``code`` is the needed item, or has the needed capability."""
    if not code:
        return False
    return supply_matches(need, code) or need in break_capabilities(code) or (need == "fords" and fords(code))


def needs_of(kb: KnowledgeBase | None, map_id: int | None, pos: Pos) -> list[str]:
    """Every need recorded on the entrance at ``pos`` on ``map_id``: a look's, then a clue's."""
    if kb is None or map_id is None:
        return []
    with kb.lock:
        row = kb.entrances.get(entrance_key(map_id, pos))
        found = [row.get(NEEDS), row.get(CLUE_NEEDS)] if isinstance(row, dict) else []
    return [n for n in found if isinstance(n, str) and n]


def carried(w: WorldModel) -> list[str]:
    """Every code held, stowed, armed or worn."""
    codes = [s.code for s in w.held_supplies + w.chest_supplies] + [w.armed_code or ""] + list(w.worn_codes.values())
    return [c for c in codes if c]


def unmet_need(w: WorldModel, kb: KnowledgeBase | None, map_id: int | None, pos: Pos) -> str | None:
    """The first need recorded on the entrance that nothing carried meets, else None."""
    codes = carried(w)
    return next((n for n in needs_of(kb, map_id, pos) if not any(meets(c, n) for c in codes)), None)


def lying_in_sight(w: WorldModel, need: str) -> Entity | None:
    """A supply in sight, not for sale, that meets ``need``, else None."""
    return next((e for e in w.entities if e.kind == "supply" and e.gem_price is None and meets(e.code, need)), None)


def in_sight(w: WorldModel, need: str) -> bool:
    """A supply that meets ``need`` lies in sight, not for sale."""
    return lying_in_sight(w, need) is not None


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
    """A reply's ops that would enter an entrance whose need is unmet are left
    out, each with why. Only an ``enter_level`` whose item lies in sight is
    kept, for Level's prerequisite stop; Travel has none."""
    kept, refused = [], []
    for g in goals:
        where = entrance_of(g, w, kb)
        need = unmet_need(w, kb, *where) if where is not None else None
        if need is not None and not (g["op"] == "enter_level" and in_sight(w, need)):
            refused.append(f"{json.dumps(g, sort_keys=True)} left out: {why_refused(need, *where)}")
            continue
        kept.append(g)
    return kept, refused


def record_needs(raw: Any, kb: KnowledgeBase | None) -> list[str]:
    """Record a reply's ``entrance_needs``: each ``{x, y, map_id, item, why}``
    sets ``clue_needs`` (and ``clue_needs_why``, dropped when no why is given)
    on that known entrance's row; ``item`` null clears both. A look's
    ``needs`` is never touched. Returns why each entry not recorded was
    refused. A value that is not a list was already refused by ``plan_fields``."""
    if not isinstance(raw, list):
        return []
    refused = []
    for entry in raw:
        coords = isinstance(entry, dict) and all(
            isinstance(entry.get(k), int) and not isinstance(entry.get(k), bool) for k in ("x", "y", "map_id")
        )
        item = entry.get("item") if isinstance(entry, dict) else None
        if not coords or not (item is None or (isinstance(item, str) and item)):
            refused.append(f"entrance_needs entry {entry!r} dropped: it takes x, y, map_id and item (a code, a capability, or null)")
            continue
        if kb is None:
            continue
        with kb.lock:
            row = kb.entrances.get(entrance_key(entry["map_id"], (entry["x"], entry["y"])))
            if not isinstance(row, dict):
                refused.append(f"entrance_needs entry {entry!r} dropped: no known entrance there (name one from level_entrances)")
                continue
            row.pop(CLUE_NEEDS, None)
            row.pop(CLUE_WHY, None)
            if item is None:
                continue
            row[CLUE_NEEDS] = item
            why = entry.get("why")
            if isinstance(why, str) and why:
                row[CLUE_WHY] = why
    return refused
