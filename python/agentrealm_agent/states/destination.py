"""Where the op on top is headed: the destination its walks serve (A120).

When the planner's current op has a destination, any walk the executor
makes on its behalf serves it. The safe default runs for the op when its
executor has no step (Travel on unexplored ground, say): it then explores
only a frontier that brings the destination nearer, and with none to walk
to it asks the planner rather than wander (``explore.safe_default``).
"""

from __future__ import annotations

from ..navigation import route_first_leg
from ..pathing import Toward, grid_params
from ..plan import GoalOp
from ..stalled import map_local
from ..world import WorldModel
from .base import PlayContext, top_op
from .explore import plan_sets
from .travel import resolve_destination


def op_destination(w: WorldModel, ctx: PlayContext) -> Toward | None:
    """The cell on this map the top op's walk heads for, or None when it has none here.

    A ``travel``'s is its destination as Travel resolves it, or, on another
    map, the door its known route leaves this map by. Any other op that names
    a cell on this map (``stalled.map_local``: a ``break_block``, a
    ``use_block``, a gather region) heads for that cell. An ``explore_area``
    has none: its own walk is the exploring.
    """
    op = top_op(ctx)
    if op is None or w.pos is None or w.map_id is None or op["op"] == "explore_area":
        return None
    if op["op"] == "travel":
        return _travel_destination(w, ctx, op)
    if map_local(op):
        return Toward((op["x"], op["y"]), f"{op['op']} {op['x']},{op['y']}", op)
    return None


def _travel_destination(w: WorldModel, ctx: PlayContext, op: GoalOp) -> Toward | None:
    dest = resolve_destination(w, ctx, op)
    if dest is None:
        return None
    label = f"travel:{dest.label} {dest.pos[0]},{dest.pos[1]}"
    if dest.map_id == w.map_id:
        return Toward(dest.pos, label, op)
    m = ctx.memory
    _, avoid, costly = plan_sets(w, m, ctx.policy, ctx.knowledge)
    params = grid_params(ctx.policy, avoid, costly, allow_goal_door=True, m=m, w=w, knowledge=ctx.knowledge)
    leg = route_first_leg(w, ctx.knowledge, dest.map_id, dest.pos, params)
    return Toward(leg[-1], label, op) if leg else None
