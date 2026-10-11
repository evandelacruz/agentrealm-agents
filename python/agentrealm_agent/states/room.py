"""Room in a full pack, carried out for the states (A102).

The rule is ``pack.make_room``; this is the one place a state asks it with
the plan's ops (:func:`room_for`) and turns its answer into intents for a
planned pickup (:func:`planned_take`). Shop, Loot, Pickup, Level and Detour
all come here.
"""

from __future__ import annotations

from ..loot import Pickup
from ..pack import DROP, MOVE, TAKE, Room, drop_spot, make_room
from ..world import WorldModel
from .base import PlayContext, StateOutcome, top_op
from .intents import drop, set_position, take


def room_for(w: WorldModel, ctx: PlayContext, p: Pickup, *, named: str | None = None) -> Room:
    """The pack rule (``pack.make_room``) for ``p``, with what the plan's ops reserve."""
    ops = ctx.plan.ops_left() if ctx.plan is not None else []
    return make_room(w, p, plan_ops=ops, knowledge=ctx.knowledge, named=named)


def planned_take(w: WorldModel, ctx: PlayContext, p: Pickup, state: str) -> StateOutcome:
    """``Take`` the supply the top op (``buy``, ``fetch_item``) wants, in reach,
    making room by the pack rule: ``Drop`` what the op's ``drop`` names, then
    ``Take``; a step off a shop cell first. When the op names nothing it may
    drop, or no cell in reach lets the drop stay ours, the op goes back to the
    planner with the reason."""
    op = top_op(ctx)
    named = op.get("drop") if op is not None else None
    room = room_for(w, ctx, p, named=named)
    label = p.code or str(p.supply_id)
    if room.kind == TAKE:
        return StateOutcome([take(p.supply_id)], f"take {label}", state=state)
    if room.kind == DROP and room.drop is not None:
        return StateOutcome([drop(room.drop.id), take(p.supply_id)], f"{room.why}, take it", state=state)
    why = room.why
    if room.kind == MOVE:
        spot = drop_spot(w, ctx.knowledge, p.pos)
        if spot is not None:
            return StateOutcome([set_position(spot)], f"{why}: step off to {spot}", state=state)
        why = f"{why}, and no cell in reach of {label} is off the shop"
    if ctx.plan is not None and op is not None:
        ctx.plan.drop_current(why, memory=ctx.memory)
    return StateOutcome(None, why, state=state)
