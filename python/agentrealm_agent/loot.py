"""Loot scoring, carry space, and junk drops (A20, M8).

Sourced rules (docs/GAME_NOTES.md, Items, slots and gear):

- Everything held, worn, armed, or stowed in the carried chest counts against
  carry capacity, 10 for a new character (Manual §11).
- ``Take`` reaches the supply's block or a neighbour; ``WithdrawFromChest``
  with ``supply_ids`` is all or nothing, ``carry_capacity_full`` when one is
  too many (API).
- ``Drop`` puts a carried supply on the ground under you, and refuses a
  non-transferable one with ``not_transferable`` (Manual §6, §11).
- Gems and lives are consumed on pickup into counters, so they take no slot
  (Manual §11).

Unknown, so disabled: which ``supply_subtype_code`` a life or a gem has. Until
it is observed, no supply is treated as a counter, so "hearts first" does not
fire (PLAN.md A20 partial).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .item_table import InventorySupply
from .world import Pos, WorldModel, chebyshev

# The Olympuff starting kit's pocket knife cannot change hands (Manual §5.3, §11).
NON_TRANSFERABLE = frozenset({"pocket_knife"})
# Unknown: no doc or observation names these codes yet (GAME_NOTES open questions).
UNKNOWN_LIFE_SUPPLY_CODES: frozenset[str] = frozenset()
UNKNOWN_GEM_SUPPLY_CODES: frozenset[str] = frozenset()
# Food GAME_NOTES names (Manual §16; Obs).
FOOD_CODES = frozenset({"golden_cap", "apple", "berry"})

LIFE_SCORE = 10_000
GEM_SCORE = 5_000
UNKNOWN_SCORE = 50


def is_counter_supply(code: str | None) -> bool:
    """Gems and lives are consumed on pickup into counters (Manual §11)."""
    return bool(code) and (code in UNKNOWN_LIFE_SUPPLY_CODES or code in UNKNOWN_GEM_SUPPLY_CODES)


def loot_score(code: str | None, items: dict[str, dict[str, Any]]) -> int:
    """Higher is more worth carrying; lives first, then gems (PLAYABLE_AGENT_PLAN)."""
    if not code:
        return UNKNOWN_SCORE
    if code in UNKNOWN_LIFE_SUPPLY_CODES:
        return LIFE_SCORE
    if code in UNKNOWN_GEM_SUPPLY_CODES:
        return GEM_SCORE
    price = (items.get(code) or {}).get("gem_price")
    if isinstance(price, int) and price > 0:
        return 500 + min(price, 500)
    if "potion" in code or code in FOOD_CODES:
        return 200
    return UNKNOWN_SCORE


def carry_slots_used(w: WorldModel) -> int:
    n = len(w.held_supplies) + len(w.chest_supplies) + len(w.worn_codes)
    return n + (1 if w.armed_code else 0)


def inventory_full(w: WorldModel) -> bool:
    return carry_slots_used(w) >= w.carry_capacity


def droppable_supplies(w: WorldModel) -> list[InventorySupply]:
    """Held supplies ``Drop`` may shed.

    Stowed supplies (``inventory.chest``) are left out: whether ``Drop`` takes
    one is not documented (GAME_NOTES open questions).
    """
    return [s for s in w.held_supplies if s.code not in NON_TRANSFERABLE and s.id not in w.undroppable]


def worst_droppable(w: WorldModel, items: dict[str, dict[str, Any]]) -> InventorySupply | None:
    candidates = droppable_supplies(w)
    if not candidates:
        return None
    return min(candidates, key=lambda s: (loot_score(s.code, items), s.id))


@dataclass(frozen=True)
class Pickup:
    """One supply worth carrying: on the ground (``chest_id`` None) or in a chest."""

    supply_id: int
    code: str
    pos: Pos
    chest_id: int | None
    score: int


def pickup_room(w: WorldModel, p: Pickup, items: dict[str, dict[str, Any]]) -> InventorySupply | None | bool:
    """True when ``p`` fits now; the supply to drop for it; False to skip it.

    A full pack skips a pickup that does not beat the worst droppable supply.
    """
    if p.chest_id is None and is_counter_supply(p.code):
        return True
    if not inventory_full(w):
        return True
    shed = worst_droppable(w, items)
    if shed is None or p.score <= loot_score(shed.code, items):
        return False
    return shed


def pickups(w: WorldModel, items: dict[str, dict[str, Any]]) -> list[Pickup]:
    """Free supplies in sight and supplies in ground chests with known contents.

    Priced supplies are the shop's (Manual §11). The death chest is Recover's
    (A11), which only goes there once the spot is known safe.
    """
    out: list[Pickup] = []
    for e in w.entities:
        if e.kind == "supply" and e.gem_price is None:
            out.append(Pickup(e.id, e.code, e.pos, None, loot_score(e.code, items)))
    death_chest = w.death_chest[2] if w.death_chest is not None else None
    for e in w.entities:
        if e.kind != "chest" or e.id == death_chest:
            continue
        for s in w.chest_contents.get(e.id) or []:
            out.append(Pickup(s.id, s.code, e.pos, e.id, loot_score(s.code, items)))
    return out


def worthwhile_pickups(w: WorldModel, items: dict[str, dict[str, Any]]) -> list[Pickup]:
    """Pickups that fit or are worth a drop, best first, then nearest."""
    here = w.pos
    if here is None:
        return []
    keep = [p for p in pickups(w, items) if pickup_room(w, p, items) is not False]
    keep.sort(key=lambda p: (-p.score, chebyshev(p.pos, here), p.supply_id))
    return keep


def learn_loot_rejection(w: WorldModel, intent: dict | None, code: str | None) -> None:
    """What a refused pickup or drop says about carry space (A20).

    ``carry_capacity_full`` on ``Take`` or ``WithdrawFromChest`` means the pack
    is full now, so capacity is at most what we carry. ``not_transferable`` on
    ``Drop`` marks that supply as one never to drop again.
    """
    if not intent:
        return
    verb = intent.get("verb")
    if code == "carry_capacity_full" and verb in ("Take", "WithdrawFromChest"):
        used = carry_slots_used(w)
        if used:  # an empty model has not read the inventory yet: nothing to learn
            w.carry_capacity = min(w.carry_capacity, used)
    elif code == "not_transferable" and verb == "Drop" and intent.get("supply_id") is not None:
        w.undroppable.add(int(intent["supply_id"]))
