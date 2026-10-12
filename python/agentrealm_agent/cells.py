"""Cells the planner names by x, y (A148).

The planner takes every cell it targets from a State list that gives the
cell's x and y outright (doors, NPCs, signs, items on the ground, level
entrances, sellers), never by counting columns on the map drawing. What
follows is the one check that a named cell can be a target at all, run on
every reply before its ops reach a state: a cell off the current map's
edge, or a wall where the op would stand, is refused with why (for the
ops that run on this map: those before the first that may leave it), so the
planner hears of it at once instead of from a walk that cannot end.
"""

from __future__ import annotations

import json
from typing import Any, Sequence

from .level_order import LEAVE_OP, too_high
from .plan import TRAVEL_SYMBOLIC
from .stalled import StalledOp, find as find_stalled
from .world import DOORS, Pos, WorldModel

# Ops whose x, y is a cell the character walks onto, so it cannot be a wall.
# Any other op's cell may be a block it acts on (a break, a sign, a door).
STAND_ON_OPS = frozenset({"travel", "fetch_item"})
# Travel destinations whose x, y is a cell (an entrance names its own map).
TRAVEL_TO_CELL = frozenset({"point", "shop", "entrance"})
# Ops that end on another map: into a level, out of one, or out of one once its boss is beaten.
MAP_CHANGING_OPS = frozenset({"enter_level", "leave_level", "fight_boss"})


def in_bounds(w: WorldModel, p: Pos) -> bool:
    """``p`` lies on the current map: inside the size the terrain reads
    gave (``WorldModel.map_size``), or anywhere while no read has given one."""
    if w.map_size is None:
        return True
    width, height = w.map_size
    return 0 <= p[0] < width and 0 <= p[1] < height


def is_wall(w: WorldModel, p: Pos) -> bool:
    """``p`` on the current map is a wall: not a sign set in one, nor a door."""
    return w.view.tiles.get(p) == "wall" and not w.view.readable.get(p)


def target_here(op: dict[str, Any], w: WorldModel) -> Pos | None:
    """The cell ``op`` targets on the current map, or None when it names no
    cell, or one on another map (its own ``map_id``, or an entrance that
    names none). A symbolic travel's ``0, 0`` (the nearest shop or entrance,
    which the agent finds) names no cell."""
    x, y = op.get("x"), op.get("y")
    if not isinstance(x, int) or not isinstance(y, int) or isinstance(x, bool) or isinstance(y, bool):
        return None
    if op.get("op") == "travel" and op.get("to") not in TRAVEL_TO_CELL:
        return None
    if op.get("op") == "travel" and op.get("to") in TRAVEL_SYMBOLIC and (x, y) == (0, 0):
        return None
    map_id = op.get("map_id")
    if map_id is None and op.get("op") == "travel" and op.get("to") == "entrance":
        return None
    if map_id is not None and map_id != w.map_id:
        return None
    return (x, y)


def bad_cell(op: dict[str, Any], w: WorldModel) -> str | None:
    """Why ``op``'s cell cannot be its target, or None when it can."""
    if w.map_id is None:
        return None
    p = target_here(op, w)
    if p is None:
        return None
    if not in_bounds(w, p):
        width, height = w.map_size or (0, 0)
        return f"{p[0]},{p[1]} is off map {w.map_id}, which is x 0..{width - 1}, y 0..{height - 1}"
    if op.get("op") in STAND_ON_OPS and is_wall(w, p):
        return f"{p[0]},{p[1]} on map {w.map_id} is a wall"
    return None


def leaves_map(op: dict[str, Any], w: WorldModel) -> bool:
    """``op`` may end with the character on another map: an op that enters
    or clears a level, or a travel that names no cell here (another map's,
    a town, a hunting ground, an entrance on a map it does not name) or
    names a door here, which warps."""
    if op.get("op") in MAP_CHANGING_OPS:
        return True
    if op.get("op") != "travel":
        return False
    p = target_here(op, w)
    return p is None or w.view.tiles.get(p) in DOORS


def leaving_level(w: WorldModel, stalled_ops: Sequence[StalledOp] = ()) -> bool:
    """The agent puts ``leave_level`` on top by itself before any reply op
    runs (``level_order.sync_leave``): inside a level above the target, unless
    it is remembered as stalled."""
    return too_high(w) and find_stalled(list(stalled_ops), LEAVE_OP, w.map_id) is None


def refuse_bad_cells(
    goals: list[dict[str, Any]],
    w: WorldModel,
    ahead: Sequence[dict[str, Any]] = (),
    stalled_ops: Sequence[StalledOp] = (),
) -> tuple[list[dict[str, Any]], list[str]]:
    """A reply's ops whose cell on the current map is off it, or a wall to
    stand on, are left out, each with why, before any reaches a state. Only
    the ops that run on this map are judged: those before the first that may
    leave it, counting the ops ``ahead`` of the reply's on the stack (the
    pinned directives ops) and the ``leave_level`` the agent puts on top by
    itself inside a level above the target (:func:`leaving_level`). Every
    op after that runs on a map not known yet, so it passes, and its walk
    finds out there."""
    kept, refused = [], []
    here = not leaving_level(w, stalled_ops) and not any(leaves_map(g, w) for g in ahead)
    for g in goals:
        why = bad_cell(g, w) if here else None
        if why is None:
            kept.append(g)
        else:
            refused.append(
                f"{json.dumps(g, sort_keys=True)} left out: {why}; take x, y from a State list "
                "(doors_seen, on_the_ground, nearby_npcs, signs_seen, level_entrances, sellers), never by counting map columns"
            )
        here = here and not leaves_map(g, w)
    return kept, refused


def doors_here(w: WorldModel) -> list[Pos]:
    """Every door seen on the current map, the one under the character included."""
    return [p for p, block in w.view.tiles.items() if block in DOORS]


def step_from(w: WorldModel, door: Pos) -> list[Pos]:
    """The ground next to ``door`` (four ways) a walk steps onto it from."""
    x, y = door
    return [p for p in ((x, y + 1), (x, y - 1), (x - 1, y), (x + 1, y)) if w.walkable(p)]
