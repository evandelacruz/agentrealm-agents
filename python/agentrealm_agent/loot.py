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

Ground gems use ``supply_subtype_code`` ``gem`` (GAME_NOTES, Obs). Which
code a life has on the ground is still unknown. A47 learns it in play: when
``lives`` rises in a response whose only applied ``Take`` was one supply,
that supply's code is filed as ``life_on_pickup`` in the item table, and
from then on scores above gems (``learn_life_code``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from typing import TYPE_CHECKING

from .item_table import InventorySupply
from .supplies import heals
from .world import Entity, Pos, WorldModel, chebyshev

if TYPE_CHECKING:
    from .knowledge_base import KnowledgeBase

# The Olympuff starting kit's pocket knife cannot change hands (Manual §5.3,
# §11). The Supplies reference does not say which supplies are transferable,
# so this stays a seed.
NON_TRANSFERABLE = frozenset({"pocket_knife"})
# Observed on the wire (GAME_NOTES.md, docs/observations/A20_live_play.md).
GEM_SUPPLY_CODES: frozenset[str] = frozenset({"gem"})
# Steps of walk a gem pile is worth: a sure gem, where a cut may drop none.
# Detour's allowance for one, and its edge over grass in Gather's pick (A73, A94).
GEM_PILE_STEPS = 8
# A life's ground code once live play confirms it (A47). Until then the item
# table's ``life_on_pickup`` rows, learned by ``learn_life_code``, stand in.
LIFE_SUPPLY_CODES: frozenset[str] = frozenset()
# Carry caps the Manual §16 gear table gives for the shop's chest upgrades.
# Assumed, not measured (GAME_NOTES "Assumed until measured"): an applied
# ``Take`` of one raises capacity to this, and ``carry_capacity_full`` still
# lowers it to what was really carried (A47).
MANUAL_CHEST_CAPACITY: dict[str, int] = {"middle_chest": 30, "red_chest": 100}

LIFE_SCORE = 10_000
GEM_SCORE = 5_000
UNKNOWN_SCORE = 50


def is_life_supply(code: str | None, items: dict[str, dict[str, Any]]) -> bool:
    """A life on the ground: a confirmed code, or one the item table learned (A47)."""
    if not code:
        return False
    return code in LIFE_SUPPLY_CODES or (items.get(code) or {}).get("life_on_pickup") is True


def is_counter_supply(code: str | None, items: dict[str, dict[str, Any]]) -> bool:
    """Gems and lives are consumed on pickup into counters (Manual §11)."""
    return bool(code) and (code in GEM_SUPPLY_CODES or is_life_supply(code, items))


def loot_score(code: str | None, items: dict[str, dict[str, Any]]) -> int:
    """Higher is more worth carrying: lives, then gems, then priced gear, potions and food."""
    if not code:
        return UNKNOWN_SCORE
    if is_life_supply(code, items):
        return LIFE_SCORE
    if code in GEM_SUPPLY_CODES:
        return GEM_SCORE
    price = (items.get(code) or {}).get("gem_price")
    if isinstance(price, int) and price > 0:
        return 500 + min(price, 500)
    if heals(code):
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
    one is not documented and not yet observed (GAME_NOTES open questions, A57).
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
    if p.chest_id is None and is_counter_supply(p.code, items):
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


def learn_chest_upgrade(w: WorldModel, code: str | None) -> None:
    """An applied ``Take`` of a chest upgrade raises carry capacity to the
    Manual §16 cap (assumed, not measured; A47)."""
    cap = MANUAL_CHEST_CAPACITY.get(code or "")
    if cap is not None:
        w.carry_capacity = max(w.carry_capacity, cap)


def learn_life_code(
    knowledge: KnowledgeBase | None,
    take_codes: list[str | None],
    lives_before: int,
    lives_after: int,
) -> str | None:
    """File a life's ground code after a response where ``lives`` rose (A47).

    ``take_codes`` are the codes of this response's applied ``Take``s of our
    own queue. Only when exactly one could explain the rise is its code filed
    as ``life_on_pickup``; with two or more, or none, nothing is learned, since
    a wrong guess would score that supply above gems for good. Returns the
    code filed, so the runner can log it for promotion to
    ``LIFE_SUPPLY_CODES``.
    """
    if knowledge is None or lives_after <= lives_before or len(take_codes) != 1:
        return None
    code = take_codes[0]
    if not code or code in GEM_SUPPLY_CODES:
        return None
    with knowledge.lock:
        knowledge.items.setdefault(code, {})["life_on_pickup"] = True
    return code


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
