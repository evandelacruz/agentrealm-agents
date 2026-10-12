"""Levels are taken in order (A145): level N+1 is not tried until N is beaten.

The minimap marks entrances with no level number, and a map inside a level
names its ``level``, so an entrance's number is learned only by going in:
the warp that lands on a level map records it on the entrance's
``kb.entrances`` row (``record_entrance_level``).

The target is the lowest level not in ``levels_cleared`` (``target_level``).
Entrances are tried in one order (``entrance_rank``): one known to be the
target first, then the unknown ones (nearest first is the caller's
distance), then ones known to be cleared levels. One known to be a higher
uncleared level is barred while a lower uncleared level exists
(``barred``). Inside a level above the target the character leaves at
once, the way it came: ``sync_leave`` puts a ``leave_level`` op on top of
the stack, which Level carries out, and the reflexes act above it as
always. A planner op that targets a barred entrance is left out with why
(``refuse_barred``).
"""

from __future__ import annotations

import json
from typing import Any

from .knowledge_base import KnowledgeBase
from .knowledge_maps import is_level_interior
from .travel.knowledge import entrance_from_kb, entrance_key, iter_entrances
from .world import Pos, WorldModel

# The key of an entrance row's level number.
LEVEL = "level"
# Entrance order: the known target, then unknown, then cleared levels.
TARGET, UNKNOWN, CLEARED = 0, 1, 2
LEAVE_OP = {"op": "leave_level"}


def record_entrance_level(kb: KnowledgeBase | None, from_map: int, from_pos: Pos, to_level: int | None) -> None:
    """A step through ``from_map:from_pos`` landed on a map inside level
    ``to_level``: that cell is the level's entrance. Only a step in from
    outside every level counts; a door between a level's rooms is no entrance."""
    if kb is None or not isinstance(to_level, int) or to_level <= 0 or is_level_interior(kb, from_map):
        return
    with kb.lock:
        row = kb.entrances.setdefault(entrance_key(from_map, from_pos), {})
        row["map_id"] = from_map
        row.setdefault("x", from_pos[0])
        row.setdefault("y", from_pos[1])
        row[LEVEL] = to_level


def entrance_level(row: dict[str, Any]) -> int | None:
    level = row.get(LEVEL)
    return level if isinstance(level, int) and not isinstance(level, bool) and level > 0 else None


def target_level(w: WorldModel) -> int | None:
    """The lowest level number not cleared; None once every authored level is."""
    cleared = set(w.levels_cleared or ())
    n = 1
    while n in cleared:
        n += 1
    return None if w.level_count is not None and n > w.level_count else n


def is_barred(level: int | None, w: WorldModel) -> bool:
    """A known level above the target and not cleared: never tried while a lower one is open."""
    target = target_level(w)
    return level is not None and target is not None and level > target and level not in (w.levels_cleared or ())


def entrance_rank(row: dict[str, Any], w: WorldModel) -> int | None:
    """Where an entrance comes in the order, or None when it is barred."""
    level = entrance_level(row)
    if level is None:
        return UNKNOWN
    if is_barred(level, w):
        return None
    return TARGET if level == target_level(w) else CLEARED


def barred_cells(kb: KnowledgeBase | None, w: WorldModel, map_id: int | None) -> set[Pos]:
    """The entrance cells on ``map_id`` known to be a barred level."""
    return {pos for mid, pos, row in iter_entrances(kb) if mid == map_id and entrance_rank(row, w) is None}


def target_cells(kb: KnowledgeBase | None, w: WorldModel, map_id: int | None) -> set[Pos]:
    """The entrance cells on ``map_id`` known to be the target level."""
    return {pos for mid, pos, row in iter_entrances(kb) if mid == map_id and entrance_rank(row, w) == TARGET}


def _row(kb: KnowledgeBase | None, map_id: int, pos: Pos) -> dict[str, Any] | None:
    return next((row for mid, p, row in iter_entrances(kb) if (mid, p) == (map_id, pos)), None)


def op_barred_level(op: dict[str, Any], w: WorldModel, kb: KnowledgeBase | None) -> int | None:
    """The barred level an ``enter_level`` or a ``travel`` to an entrance names, if any."""
    name = op.get("op")
    if name == "enter_level" and w.map_id is not None:
        place: tuple[int, Pos] | None = (w.map_id, (op["x"], op["y"]))
    elif name == "travel" and op.get("to") == "entrance" and (op.get("x"), op.get("y")) != (0, 0):
        place = entrance_from_kb(kb, op.get("map_id"), op["x"], op["y"])
    else:
        return None
    row = _row(kb, *place) if place is not None else None
    level = entrance_level(row) if row is not None else None
    return level if is_barred(level, w) else None


def refuse_barred(
    goals: list[dict[str, Any]], w: WorldModel, kb: KnowledgeBase | None
) -> tuple[list[dict[str, Any]], list[str]]:
    """A reply's ops that target a known level above the target are left out, each with why."""
    kept, refused = [], []
    target = target_level(w)
    for g in goals:
        level = op_barred_level(g, w, kb)
        if level is not None:
            refused.append(
                f"{json.dumps(g, sort_keys=True)} left out: that entrance is level {level}, and level {target} "
                "is not cleared yet; levels are taken in order"
            )
            continue
        kept.append(g)
    return kept, refused


def too_high(w: WorldModel) -> bool:
    """Inside a level above the target: time to leave."""
    level = w.map_level
    return w.alive and w.map_id is not None and level is not None and level > 0 and is_barred(level, w)


def sync_leave(plan, w: WorldModel) -> None:
    """Inside a level above the target, ``leave_level`` is the op on top."""
    if too_high(w) and (plan.current() or {}).get("op") != LEAVE_OP["op"]:
        plan.push(dict(LEAVE_OP), f"inside level {w.map_level}; level {target_level(w)} is not cleared yet")


def order_line(w: WorldModel) -> str:
    """State: the level to take next."""
    target = target_level(w)
    return f"level_target={target if target is not None else 'none (every level cleared)'}"
