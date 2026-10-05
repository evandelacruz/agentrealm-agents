"""Reflex 4 pickup helper (A20): not a ``STATES`` entry.

``ExploreState`` calls ``pickup_outcome`` from ``scripted_outcome`` when
``policy.pickup`` is on and a supply within one block beats what we carry.
"""

from __future__ import annotations

from ..item_table import InventorySupply
from ..knowledge_base import KnowledgeBase, knowledge_items
from ..loot import pickup_room, worthwhile_pickups
from ..world import WorldModel, chebyshev
from .base import StateOutcome
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
