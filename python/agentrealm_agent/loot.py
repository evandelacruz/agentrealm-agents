"""Loot scoring, carry space, and junk drops (A20, M8).

Sourced rules (docs/GAME_NOTES.md, Items, slots and gear):

- Everything held, worn, armed, or stowed in the carried chest counts against
  carry capacity, 10 for a new character (Manual §11). The server serves no
  capacity, so it is derived at every inventory sync from the chest applied
  (``WorldModel.sync_carry_capacity``, A115), and the applied chest, a fact
  only this character's own ``Use`` shows, is kept per character in the world
  knowledge base (``load_carried_chest``, ``save_carried_chest``).
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

from .item_table import DEFAULT_CARRY_CAPACITY, InventorySupply
from .supplies import chest_capacity, heals
from .world import Entity, Pos, WorldModel

if TYPE_CHECKING:
    from .knowledge_base import KnowledgeBase

# The Olympuff starting kit's pocket knife cannot change hands (Manual §5.3,
# §11). The Supplies reference does not say which supplies are transferable,
# so this stays a seed.
NON_TRANSFERABLE = frozenset({"pocket_knife"})
# Observed on the wire (GAME_NOTES.md, docs/observations/A20_live_play.md).
GEM_SUPPLY_CODES: frozenset[str] = frozenset({"gem"})
# Steps of walk a gem pile is worth: a sure gem, where a cut may drop none.
# Detour's allowance for one (A73).
GEM_PILE_STEPS = 8
# A life's ground code once live play confirms it (A47). Until then the item
# table's ``life_on_pickup`` rows, learned by ``learn_life_code``, stand in.
LIFE_SUPPLY_CODES: frozenset[str] = frozenset()

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
    return w.slots_carried()


def inventory_full(w: WorldModel) -> bool:
    return carry_slots_used(w) >= w.carry_capacity


def droppable_supplies(w: WorldModel) -> list[InventorySupply]:
    """Held supplies ``Drop`` may shed.

    Stowed supplies (``inventory.chest``) are left out: whether ``Drop`` takes
    one is not documented and not yet observed (GAME_NOTES open questions, A57).
    """
    return [s for s in w.held_supplies if s.code not in NON_TRANSFERABLE and s.id not in w.undroppable]


@dataclass(frozen=True)
class Pickup:
    """One supply worth carrying: on the ground (``chest_id`` None) or in a chest."""

    supply_id: int
    code: str
    pos: Pos
    chest_id: int | None
    score: int


def pickups(w: WorldModel, items: dict[str, dict[str, Any]]) -> list[Pickup]:
    """Free supplies in sight and supplies in ground chests with known contents.

    Priced supplies are the shop's (Manual §11). Our death chests are
    Recover's (A11, A122), which takes back all that fits from one in reach.
    """
    out: list[Pickup] = []
    for e in w.entities:
        if e.kind == "supply" and e.gem_price is None:
            out.append(Pickup(e.id, e.code, e.pos, None, loot_score(e.code, items)))
    for e in w.entities:
        if e.kind != "chest" or e.id in w.death_chests:
            continue
        for s in w.chest_contents.get(e.id) or []:
            out.append(Pickup(s.id, s.code, e.pos, e.id, loot_score(s.code, items)))
    return out


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
    """A ``Use`` on yourself of a chest that went through (it was used up)
    makes it the carried chest when its Supplies reference ``chest_capacity``
    is larger; one no larger (the blue chest, the size a character starts
    with) changes nothing. Taking or holding a chest raises nothing (A108).
    The server serves neither capacity nor the carried chest, so capacity is
    derived from this chest (``WorldModel.sync_carry_capacity``, A115), and a
    ``carry_capacity_full`` refusal still bounds it (A47)."""
    cap = chest_capacity(code)
    if cap is not None and cap > (chest_capacity(w.carried_chest) or DEFAULT_CARRY_CAPACITY):
        w.carried_chest, w.full_at = code, None
        w.sync_carry_capacity()


CARRIED_CHESTS_KEY = "carried_chests"  # kb.extra: character id -> chest code applied (A115)


def load_carried_chest(knowledge: KnowledgeBase | None, w: WorldModel) -> None:
    """The chest this character applied, from the world knowledge base, so a
    new run starts at its size instead of the blue chest's (A115). Only what
    the server cannot tell us is kept: the applied chest, never the capacity."""
    rows = knowledge.extra.get(CARRIED_CHESTS_KEY) if knowledge is not None else None
    code = rows.get(str(w.character_id)) if isinstance(rows, dict) else None
    if isinstance(code, str) and chest_capacity(code) is not None:
        w.carried_chest = code
        w.sync_carry_capacity()


def save_carried_chest(knowledge: KnowledgeBase | None, w: WorldModel) -> None:
    """Write the applied chest back; the blue chest (None) removes the row."""
    if knowledge is None:
        return
    with knowledge.lock:
        rows = knowledge.extra.get(CARRIED_CHESTS_KEY)
        rows = dict(rows) if isinstance(rows, dict) else {}
        if w.carried_chest is None:
            rows.pop(str(w.character_id), None)
        else:
            rows[str(w.character_id)] = w.carried_chest
        if rows:
            knowledge.extra[CARRIED_CHESTS_KEY] = rows
        else:
            knowledge.extra.pop(CARRIED_CHESTS_KEY, None)


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


def full_bound(intent: dict, used: int) -> int:
    """The most the pack can hold, from a ``carry_capacity_full`` refusal with
    ``used`` slots carried. Taking is all or nothing: a ``Take``, or a
    ``WithdrawFromChest`` with no ``supply_ids`` (refused only when not even
    one fits), shows no room for one more; one naming ``n`` supplies shows
    only that ``n`` more did not fit (API WithdrawFromChest)."""
    ids = intent.get("supply_ids") if intent.get("verb") == "WithdrawFromChest" else None
    extra = len(ids) if isinstance(ids, list) and ids else 1
    return used + extra - 1


def learn_loot_rejection(w: WorldModel, intent: dict | None, code: str | None) -> None:
    """What a refused pickup or drop says about carry space (A20).

    ``carry_capacity_full`` on ``Take`` or ``WithdrawFromChest`` bounds
    capacity by what did not fit (``full_bound``). ``not_transferable`` on
    ``Drop`` marks that supply as one never to drop again.
    """
    if not intent:
        return
    verb = intent.get("verb")
    if code == "carry_capacity_full" and verb in ("Take", "WithdrawFromChest"):
        used = carry_slots_used(w)
        if used:  # an empty model has not read the inventory yet: nothing to learn
            w.full_at = full_bound(intent, used)
            if (chest_capacity(w.carried_chest) or DEFAULT_CARRY_CAPACITY) > w.full_at:
                w.carried_chest = None  # refused below its size: not the chest we thought we carry
            w.sync_carry_capacity()
    elif code == "not_transferable" and verb == "Drop" and intent.get("supply_id") is not None:
        w.undroppable.add(int(intent["supply_id"]))
