"""Pickup: take a worthwhile supply underfoot or adjacent (reflex 4, A20).

A reflex: it acts on what is in reach now, whatever the plan says, and
never walks. Walking to a supply further away is **Loot**'s, for a
``fetch_item`` op.

With a full pack it never drops: what to give up is the planner's call
(``pack.make_room``, A102), so a pickup that does not fit is skipped.
"""

from __future__ import annotations

from ..knowledge_base import knowledge_items
from ..loot import Pickup, pickups
from ..navigation import stuck as nav_stuck
from ..pack import TAKE
from ..reach import NEAR
from ..world import WorldModel, chebyshev
from .base import PlayContext, State, StateOutcome
from .intents import take, withdraw
from .room import room_for


def pickup_outcome(w: WorldModel, ctx: PlayContext, *, state: str) -> StateOutcome | None:
    """``Take`` or ``WithdrawFromChest`` one supply that fits; None when nothing in reach does.

    A reflex never drops: with a full pack what to give up is the planner's
    (the pack rule), so a pickup that does not fit is skipped."""
    here = w.pos
    if here is None:
        return None
    items = knowledge_items(ctx.knowledge)
    found = [p for p in pickups(w, items) if chebyshev(p.pos, here) <= NEAR]
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
