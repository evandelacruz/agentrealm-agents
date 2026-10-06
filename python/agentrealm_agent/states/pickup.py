"""Pickup: take a worthwhile supply underfoot or adjacent (reflex 4, A20).

A reflex: it acts on what is in reach now, whatever the plan says, and
never walks. Walking to a supply further away is **Loot**'s, for a
``fetch_item`` op.
"""

from __future__ import annotations

from ..item_table import InventorySupply
from ..knowledge_base import KnowledgeBase, knowledge_items
from ..loot import pickup_room, worthwhile_pickups
from ..world import WorldModel, chebyshev
from .base import PlayContext, State, StateOutcome
from .intents import drop, take, withdraw


def pickup_outcome(w: WorldModel, knowledge: KnowledgeBase | None, *, state: str) -> StateOutcome | None:
    """``Drop`` junk, ``Take``, or ``WithdrawFromChest`` one supply; None when nothing in reach is worth it."""
    here = w.pos
    if here is None:
        return None
    items = knowledge_items(knowledge)
    for p in worthwhile_pickups(w, items):
        if chebyshev(p.pos, here) > 1:
            continue
        room = pickup_room(w, p, items)
        label = p.code or str(p.supply_id)
        if isinstance(room, InventorySupply):
            intent, reason = drop(room.id), f"drop {room.code or room.id} for {label}"
        elif p.chest_id is None:
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
        return pickup_outcome(world, ctx.knowledge, state=self.name) is not None

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not self.guard(world, ctx)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        out = pickup_outcome(world, ctx.knowledge, state=self.name)
        return out if out is not None else StateOutcome(None, "nothing in reach", state=self.name)
