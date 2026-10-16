"""An op that walks to an entrance is done once the character goes through it (A144).

An entrance is a door: stepping onto it warps the character to the map it
leads to. The op's cell is then on a map he has left, so no walk on the new
map reaches it, and asking the way there is asking the wrong question.
``went_through`` is the one test: the last door gone through
(``WorldModel.passage``) is the op's door, he went through it since the op
reached the top, and he is still on the map it led to. Going through some
other door is no arrival: the walk heads back toward the op's map by
``navigation.walk_toward``, the known route else the way off the map he is
on, and asks once neither is left.
"""

from __future__ import annotations

from typing import Any

from .world import Pos, WorldModel


def entrance_of(op: dict[str, Any]) -> tuple[int | None, Pos | None] | None:
    """The door ``op`` walks to, as (map id, cell), or None when it walks to no door.

    A map id of None is an op that names no map: it means the map the
    character was on when the op reached the top (``went_through``'s
    ``top_map``), as an omitted ``map_id`` means the current map everywhere
    else. A cell of None matches any door (``travel`` to the nearest
    entrance, ``x, y`` of ``0, 0``). A ``travel`` to a point names a door
    when its cell is one: a passage only ever starts on a door.
    """
    name = op.get("op")
    if name == "enter_level":
        return None, (op["x"], op["y"])
    if name != "travel" or op.get("to") not in ("entrance", "point"):
        return None
    map_id = op.get("map_id") if isinstance(op.get("map_id"), int) else None
    if op["to"] == "entrance" and op["x"] == 0 and op["y"] == 0:
        return map_id, None
    return map_id, (op["x"], op["y"])


def went_through(op: dict[str, Any], world: WorldModel, since_tick: int | None, top_map: int | None) -> bool:
    """True when the character went through ``op``'s door at or after
    ``since_tick`` (the tick the op reached the top) and is still on the map
    that door led to. An op that names no map means ``top_map``, the map he
    was on when the op reached the top (``Plan.top_map_id``)."""
    p = world.passage
    door = entrance_of(op)
    if p is None or door is None or since_tick is None or p.tick < since_tick or world.map_id != p.to_map:
        return False
    map_id, cell = door
    if map_id is None:
        map_id = top_map
    return map_id == p.from_map and (cell is None or cell == p.from_pos)
