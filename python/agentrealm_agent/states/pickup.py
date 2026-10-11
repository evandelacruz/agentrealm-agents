"""Pickup: take a worthwhile supply underfoot or adjacent (reflex 4, A20).

A reflex: it acts on what is in reach now, whatever the plan says, and
never walks. Walking to a supply further away is **Loot**'s, for a
``fetch_item`` op.

Room in a full pack is the one rule in ``pack.make_room`` (A102), for this
reflex and for every state that picks up: :func:`room_for` asks it with the
plan's ops, and :func:`planned_take` carries it out for an op's pickup.
"""

from __future__ import annotations

from ..knowledge_base import knowledge_items
from ..loot import Pickup, pickups
from ..navigation import stuck as nav_stuck
from ..pack import DROP, MOVE, TAKE, Room, drop_spot, make_room
from ..world import WorldModel, chebyshev
from .base import PlayContext, State, StateOutcome, top_op
from .intents import drop, set_position, take, withdraw


def room_for(w: WorldModel, ctx: PlayContext, p: Pickup, *, named: str | None = None) -> Room:
    """The pack rule (``pack.make_room``) for ``p``, with what the plan's ops reserve."""
    ops = ctx.plan.ops_left() if ctx.plan is not None else []
    return make_room(w, p, plan_ops=ops, knowledge=ctx.knowledge, named=named)


def planned_take(w: WorldModel, ctx: PlayContext, p: Pickup, state: str) -> StateOutcome:
    """``Take`` the supply the top op (``buy``, ``fetch_item``) wants, in reach,
    making room by the pack rule: ``Drop`` what the op's ``drop`` names, then
    ``Take``; a step off a shop cell first; or, when the op names nothing it
    may drop, the op goes back to the planner with the choices."""
    op = top_op(ctx)
    named = op.get("drop") if op is not None else None
    room = room_for(w, ctx, p, named=named)
    label = p.code or str(p.supply_id)
    if room.kind == TAKE:
        return StateOutcome([take(p.supply_id)], f"take {label}", state=state)
    if room.kind == DROP:
        assert room.drop is not None
        return StateOutcome([drop(room.drop.id), take(p.supply_id)], f"{room.why}, take it", state=state)
    if room.kind == MOVE:
        spot = drop_spot(w, ctx.knowledge, p.pos)
        if spot is None:
            return StateOutcome(None, f"{room.why}: no cell off it in reach of {label}", state=state)
        return StateOutcome([set_position(spot)], f"{room.why}: step off to {spot}", state=state)
    if ctx.plan is not None and op is not None:
        ctx.plan.drop_current(room.why, memory=ctx.memory)
    return StateOutcome(None, room.why, state=state)


def pickup_outcome(w: WorldModel, ctx: PlayContext, *, state: str) -> StateOutcome | None:
    """``Take`` or ``WithdrawFromChest`` one supply that fits; None when nothing in reach does.

    A reflex never drops: with a full pack what to give up is the planner's
    (the pack rule), so a pickup that does not fit is skipped."""
    here = w.pos
    if here is None:
        return None
    items = knowledge_items(ctx.knowledge)
    found = [p for p in pickups(w, items) if chebyshev(p.pos, here) <= 1]
    found.sort(key=lambda p: (-p.score, chebyshev(p.pos, here), p.supply_id))
    for p in found:
        room = room_for(w, ctx, p)
        if room.kind != TAKE:
            continue
        label = p.code or str(p.supply_id)
        if p.chest_id is None:
            intent, reason = take(p.supply_id), f"take {label}"
        else:
            intent, reason = withdraw(p.chest_id, [p.supply_id]), f"withdraw {label} from chest {p.chest_id}"
        return StateOutcome([intent], reason, reflex=True, state=state)
    return None


class PickupState(State):
    """Reflex, after Recover. Runs while ``policy.pickup`` is on and a
    worthwhile supply is within one block."""

    name = "Pickup"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        if ctx.policy.kind != "scripted" or not ctx.policy.pickup or not world.alive:
            return False
        return pickup_outcome(world, ctx, state=self.name) is not None

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not self.guard(world, ctx)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        out = pickup_outcome(world, ctx, state=self.name)
        if out is not None:
            nav_stuck.finish_in_reach(ctx.memory, world, "loot")  # a fetch walk to it is over
        return out if out is not None else StateOutcome(None, "nothing in reach", state=self.name)
