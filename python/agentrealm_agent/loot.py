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

Ground gems use ``supply_subtype_code`` ``gem`` (GAME_NOTES, Obs). Lives on
the ground are still unknown, so hearts first stays off until A47 observes one
(``observe_life_supply_code`` on a Take that raises ``lives``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .item_table import InventorySupply
from .world import Entity, Pos, WorldModel, chebyshev

# The Olympuff starting kit's pocket knife cannot change hands (Manual §5.3, §11).
NON_TRANSFERABLE = frozenset({"pocket_knife"})
# Observed on the wire (GAME_NOTES.md, docs/observations/A20_live_play.md).
GEM_SUPPLY_CODES: frozenset[str] = frozenset({"gem"})
# Shipped empty; grows when live play confirms a life's ground code (A47).
LIFE_SUPPLY_CODES: frozenset[str] = frozenset()
# Authored carry caps (Manual §16 gear table); applied when Take of that upgrade applies (A47).
AUTHORED_CHEST_CAPACITY: dict[str, int] = {"middle_chest": 30, "red_chest": 100}
_observed_life_codes: set[str] = set()
# Food GAME_NOTES names (Manual §16; Obs).
FOOD_CODES = frozenset({"golden_cap", "apple", "berry"})

LIFE_SCORE = 10_000
GEM_SCORE = 5_000
UNKNOWN_SCORE = 50


def _life_supply_codes() -> frozenset[str]:
    return LIFE_SUPPLY_CODES | frozenset(_observed_life_codes)


def observe_life_supply_code(code: str | None) -> None:
    """Record a life's ground ``supply_subtype_code`` once Take raises ``lives`` (A47)."""
    if code:
        _observed_life_codes.add(code)


def is_counter_supply(code: str | None) -> bool:
    """Gems and lives are consumed on pickup into counters (Manual §11)."""
    return bool(code) and (code in _life_supply_codes() or code in GEM_SUPPLY_CODES)


def loot_score(code: str | None, items: dict[str, dict[str, Any]]) -> int:
    """Higher is more worth carrying; lives first once A47 fills LIFE_SUPPLY_CODES, then gems."""
    if not code:
        return UNKNOWN_SCORE
    if code in _life_supply_codes():
        return LIFE_SCORE
    if code in GEM_SUPPLY_CODES:
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
    """Held supplies ``Drop`` may shed; stowed too once live play confirms it (A47)."""
    held = [s for s in w.held_supplies if s.code not in NON_TRANSFERABLE and s.id not in w.undroppable]
    if not w.stowed_drop_supported:
        return held
    stowed = [s for s in w.chest_supplies if s.code not in NON_TRANSFERABLE and s.id not in w.undroppable]
    return held + stowed


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


def supply_code_for_take(intent: dict | None, entities: list[Entity]) -> str | None:
    """Subtype of the supply a ``Take`` names, from the pre-tick entity list."""
    if not intent or intent.get("verb") != "Take":
        return None
    sid = intent.get("supply_id")
    if not isinstance(sid, int):
        return None
    for e in entities:
        if e.kind == "supply" and e.id == sid:
            return e.code or None
    return None


def learn_loot_applied(
    w: WorldModel,
    intent: dict | None,
    outcome: str | None,
    *,
    entities_before: list[Entity],
) -> None:
    """What an applied Take or Drop teaches before the snapshot updates (A47)."""
    if outcome != "applied" or not intent:
        return
    verb = intent.get("verb")
    if verb == "Take":
        code = supply_code_for_take(intent, entities_before)
        cap = AUTHORED_CHEST_CAPACITY.get(code or "")
        if cap is not None:
            w.carry_capacity = max(w.carry_capacity, cap)
    elif verb == "Drop" and intent.get("supply_id") is not None:
        sid = int(intent["supply_id"])
        if any(s.id == sid for s in w.chest_supplies):
            w.stowed_drop_supported = True


def learn_loot_life_take(
    w: WorldModel,
    intent: dict | None,
    *,
    entities_before: list[Entity],
    lives_before: int,
) -> None:
    """After the snapshot applies, record a life's ground code when lives rose on Take (A47)."""
    if not intent or intent.get("verb") != "Take" or w.lives <= lives_before:
        return
    observe_life_supply_code(supply_code_for_take(intent, entities_before))


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
