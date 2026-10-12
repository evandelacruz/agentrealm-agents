"""Levels are taken in order (A145): level N+1 is not tried until N is beaten.

The minimap marks entrances with no level number, and only the minimap's
entry for a map inside a level names its ``level`` (the position read
carries none), so an entrance's number is learned only by going in: a door
step that lands on another map is held (``note_door_step``) until a minimap
read names the landing map's level (``minimap_due``, at most
``MINIMAP_TRIES`` reads per map), and then a step in from outside every
level records it on the entrance's ``kb.entrances`` row (``settle_entry``).

The target is the lowest level not in ``levels_cleared`` (``target_level``).
Entrances are tried in one order (``entrance_rank``): one known to be the
target first, then the unknown ones (nearest first is the caller's
distance), then ones known to be cleared levels. One known to be a higher
uncleared level is barred while a lower uncleared level exists
(``barred``). Inside a level above the target the character leaves at
once: ``sync_leave`` puts a ``leave_level`` op on top of the stack, which
Level carries out by the doors it knows lead out (``way_out``), and the
reflexes act above it as always. It is the agent's op, not the planner's,
and stalls like any other op (A104). A planner op that targets a barred entrance is left out with why
(``refuse_barred``).
"""

from __future__ import annotations

import json
from typing import Any

from .knowledge_base import KnowledgeBase
from .knowledge_maps import door_warp_known, is_level_interior, iter_doors
from .memory import Memory
from .stalled import find as find_stalled
from .travel.knowledge import entrance_from_kb, entrance_key, iter_entrances
from .world import DOORS, Pos, WorldModel, chebyshev

# The key of an entrance row's level number.
LEVEL = "level"
# Entrance order: the known target, then unknown, then cleared levels.
TARGET, UNKNOWN, CLEARED = 0, 1, 2
LEAVE_OP = {"op": "leave_level"}
# Minimap reads spent on one map to learn its level before giving up on it:
# a map is on the minimap once one of its tiles is revealed.
MINIMAP_TRIES = 2


def note_door_step(m: Memory, from_map: int, from_pos: Pos, to_map: int, to_pos: Pos) -> None:
    """A door step landed on another map: held until that map's level is read."""
    if to_map != from_map:
        m.level_entry = (from_map, from_pos, to_map, to_pos)


def minimap_due(w: WorldModel, m: Memory) -> bool:
    """A door step landed on a map whose level is unread: only the minimap
    carries it, and it says whether that door was a level entrance."""
    entry = m.level_entry
    return (
        entry is not None
        and w.map_id == entry[2]
        and w.map_id not in w.map_levels
        and m.minimap_reads.get(w.map_id, 0) < MINIMAP_TRIES
    )


def level_pending(w: WorldModel, m: Memory) -> bool:
    """The current map's level is still to be read: what it means waits for it."""
    return minimap_due(w, m) and w.map_level is None


def settle_entry(kb: KnowledgeBase | None, w: WorldModel, m: Memory) -> None:
    """Once the landing map's level is read, a step in from outside every
    level records the entrance's number. Left behind, or its reads spent,
    the step is let go."""
    entry = m.level_entry
    if entry is None or level_pending(w, m):
        return
    m.level_entry = None
    from_map, from_pos, landing_map, _ = entry
    if w.map_id == landing_map and w.map_level:
        record_entrance_level(kb, from_map, from_pos, w.map_level)


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


def sync_leave(plan, w: WorldModel, m: Memory) -> None:
    """Inside a level above the target, ``leave_level`` goes on top of the
    stack, once: not while one is already on it (demoted below another op,
    say), nor while it is remembered as stalled with no reason to retry
    (A104): the stall rule moves or drops it like any other op."""
    if not too_high(w) or any(op.get("op") == LEAVE_OP["op"] for op in plan.ops_left()):
        return
    if find_stalled(m.stalled_ops, LEAVE_OP, w.map_id) is not None:
        return
    plan.push(dict(LEAVE_OP), f"inside level {w.map_level}; level {target_level(w)} is not cleared yet")


def _leads_out(kb: KnowledgeBase | None, w: WorldModel, door: dict[str, Any]) -> bool | None:
    """Whether a door's known warp lands outside every level; None when its warp is unknown."""
    if not door_warp_known(door):
        return None
    to_map = int(door["to_map_id"])
    if to_map in w.map_levels:
        return w.map_levels[to_map] == 0
    return kb is not None and not is_level_interior(kb, to_map)


def landing_cell(kb: KnowledgeBase | None, map_id: int | None) -> Pos | None:
    """Where a known entrance's warp lands on ``map_id``: the way the character came in."""
    if kb is None or map_id is None:
        return None
    for mid, pos, _ in iter_entrances(kb):
        for d in iter_doors(kb, mid):
            if (int(d["x"]), int(d["y"])) == pos and door_warp_known(d) and int(d["to_map_id"]) == map_id:
                return int(d["to_x"]), int(d["to_y"])
    return None


def way_out(kb: KnowledgeBase | None, w: WorldModel) -> Pos | None:
    """Where ``leave_level`` walks, by what is known, never a guess on proximity alone:

    a door seen here whose known warp lands outside every level; else, on
    the map a known entrance landed on, the door nearest that landing whose
    warp is not known to lead deeper into a level (the way back the
    character came in), or the landing cell while no such door is seen;
    else None: no known way out, and the planner is asked.
    """
    if w.map_id is None:
        return None
    here = w.pos
    known = {(int(d["x"]), int(d["y"])): _leads_out(kb, w, d) for d in iter_doors(kb, w.map_id)} if kb is not None else {}
    doors = {p for p, b in w.view.tiles.items() if b in DOORS}
    out = [p for p in doors if known.get(p)]
    if out and here is not None:
        return min(out, key=lambda p: (chebyshev(p, here), p))
    landing = landing_cell(kb, w.map_id)
    if landing is None:
        return None
    back = [p for p in doors if known.get(p) is not False]
    if back:
        return min(back, key=lambda p: (chebyshev(p, landing), p))
    return landing if landing != here else None


def order_line(w: WorldModel) -> str:
    """State: the level to take next."""
    target = target_level(w)
    return f"level_target={target if target is not None else 'none (every level cleared)'}"
