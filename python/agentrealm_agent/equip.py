"""Equip scoring and upgrade selection (A19, A55, M8, A99).

Only sourced facts decide what goes where:

- **Weapons** are the subtypes the Manual's Supplies reference lists in the
  ``weapon`` class (A54, ``supplies.is_weapon``). Nothing else is armed by Equip.
- **Worn slots** come from the snapshot's ``worn`` by slot: the world model
  files a subtype seen worn in a slot there for the run (``WorldModel.worn_slots``).
  Failing that, the Supplies reference's ``slot`` for a subtype it classes as
  worn (``supplies.worn_slot``). A subtype it classes as anything else (a tool,
  a consumable, a gem) is never worn. Only a subtype whose slot neither gives
  gets one ``Wear`` to find it (A55), reading which slot ``worn`` shows.

**The best weapon** is one rule, :func:`weapon_rank`, that Equip arms by and
the planner's ``upgrades_for_sale`` lists by (A99): a weapon that keeps the
character within the highest hunting-ground strength ceiling known beats one
that does not, and among those alike, more published ``damage`` wins (the
Supplies reference; damage adds to attack power on every swing). Strength is
attack power plus the armed weapon's damage plus worn armor's defense
(:func:`loadout_strength`). With no ceiling known, damage alone decides. An
``equip`` op that names a code arms or wears that item whatever the rule says
(:func:`named_equip`): the planner's call, for a fight the hunting ground does
not decide.

Each worn slot compares like with like, never across units: learned
per-NPC-type ``damage_saved`` on the hostile types both items have measured,
weighted by the threat table; failing that, ``gem_price`` when both have one.
The non-transferable starting kit has no shop price and counts as 0 gems.
Items with nothing comparable are left alone. A swap needs a clear gain
(``MIN_GAIN_RATIO``), so ties and noise never swap.

A refused ``Arm``, ``Wear`` or ``Remove`` marks its (subtype, slot) pair, and
Equip does not try that pair again until the loadout or inventory changes.
A slot-learn ``Wear`` that gets ``not_wearable`` is never retried for that
subtype; any other refusal on a try is cleared when the loadout or inventory
changes. Consumables and compose fragments are never equipped; they stay for
Heal and Solve (PLAYABLE_AGENT_PLAN Gear).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .item_table import InventorySupply
from .knowledge_base import KnowledgeBase
from .loot import NON_TRANSFERABLE
from .supplies import armor_defense, heals, is_weapon, weapon_damage, worn, worn_slot
from .memory import Memory
from .survival import BASE_ATTACK_POWER
from .threat import ThreatTable
from .travel.knowledge import iter_hunting_cells
from .world import WorldModel

ARMED = "armed"
WEAR_SLOTS = ("head", "body", "legs", "feet", "accessory")
NOT_WEARABLE = "not_wearable"

# A swap must beat what is in the slot by more than this factor: equal or near-equal
# scores (one rolled hit apart, or a re-price) keep what is equipped.
MIN_GAIN_RATIO = 1.25


def is_consumable(code: str | None) -> bool:
    """Food or a potion: Heal's to use, never equipped."""
    return heals(code)


def never_worn(code: str | None) -> bool:
    """A weapon, a consumable, or anything else the Supplies reference lists in
    a class that is not worn: Equip never sends ``Wear`` for it."""
    return is_consumable(code) or is_weapon(code) or worn(code) is False


def wear_slot(code: str | None, w: WorldModel) -> str | None:
    """The slot ``code`` is worn in: as seen worn this run, else as the
    Supplies reference gives it; None when never worn or not yet known."""
    if not code or never_worn(code):
        return None
    slot = w.worn_slots.get(code) or worn_slot(code)
    return slot if slot in WEAR_SLOTS else None


def _price(code: str, items: dict[str, dict[str, Any]]) -> int | None:
    price = (items.get(code) or {}).get("gem_price")
    if isinstance(price, int) and not isinstance(price, bool) and price > 0:
        return price
    if code in NON_TRANSFERABLE:
        return 0  # starting kit: never sold, so no shop price
    return None


def _per_npc(code: str, items: dict[str, dict[str, Any]], field: str) -> dict[str, int]:
    raw = (items.get(code) or {}).get(field)
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(v, int) and not isinstance(v, bool) and v > 0}


def _threat_weights(threat: ThreatTable) -> dict[str, int]:
    return {
        key[1]: amount
        for key, amount in threat.by_type.items()
        if key[0] == "npc" and isinstance(amount, int) and amount > 0
    }


def compare(
    held: str,
    current: str,
    items: dict[str, dict[str, Any]],
    threat: ThreatTable,
    field: str,
) -> tuple[int, int] | None:
    """(held score, current score) on one shared scale, or None when none is shared.

    Measured hits on the threat-table NPC types both subtypes have a sample
    for come first; else both ``gem_price``s.
    """
    weights = _threat_weights(threat)
    h, c = _per_npc(held, items, field), _per_npc(current, items, field)
    shared = [t for t in weights if t in h and t in c]
    if shared:
        return sum(h[t] * weights[t] for t in shared), sum(c[t] * weights[t] for t in shared)
    hp, cp = _price(held, items), _price(current, items)
    if hp is not None and cp is not None:
        return hp, cp
    return None


def clear_gain(held: int, current: int) -> bool:
    return held > current * MIN_GAIN_RATIO


@dataclass(frozen=True)
class EquipUpgrade:
    """Put held supply ``supply_id`` (``code``) in ``slot``, taking off what is worn there first."""

    slot: str
    supply_id: int
    code: str
    remove_first: bool = False
    learn_slot: bool = False  # A55: Wear with no slot arg to discover where the subtype goes


def _candidates(w: WorldModel) -> list[InventorySupply]:
    return [s for s in w.held_supplies if s.fragment is None and s.code and not is_consumable(s.code)]


def _best_for_slot(
    slot: str,
    current: str | None,
    held: list[InventorySupply],
    items: dict[str, dict[str, Any]],
    threat: ThreatTable,
    field: str,
) -> InventorySupply | None:
    """The held supply that clearly beats ``current``; an empty slot takes any candidate."""
    if not held:
        return None
    if current is None:
        priced = [(_price(s.code, items) or 0, s) for s in held]
        return max(priced, key=lambda p: (p[0], -p[1].id))[1]
    ranked: list[tuple[float, int, InventorySupply]] = []
    for s in held:
        if s.code == current:
            continue
        pair = compare(s.code, current, items, threat, field)
        if pair is None or not clear_gain(*pair):
            continue
        # Each candidate is scored against current on its own shared scale, so
        # candidates are ranked by gain ratio, never by raw score across scales.
        ranked.append((pair[0] / max(pair[1], 1), -s.id, s))
    return max(ranked, key=lambda r: r[:2])[2] if ranked else None


def hunting_ceiling(w: WorldModel, kb: KnowledgeBase | None) -> int | None:
    """The highest strength ceiling of any hunting ground known, this run's
    zone reads and the knowledge base's together; None while none is known."""
    ceilings = [fact.get("strength_ceiling") for _, _, fact in iter_hunting_cells(kb)]
    ceilings += [fact.strength_ceiling for facts in w.zones.values() for fact in facts.values()]
    known = [c for c in ceilings if isinstance(c, int) and not isinstance(c, bool)]
    return max(known, default=None)


def loadout_strength(w: WorldModel, weapon: str | None) -> int:
    """Hunting-ground strength with ``weapon`` armed and what is worn now:
    attack power, the weapon's damage and worn armor's defense (Supplies
    reference; GAME_NOTES Hunting grounds)."""
    armor = sum(armor_defense(code) or 0 for code in w.worn_codes.values())
    return BASE_ATTACK_POWER + (weapon_damage(weapon) or 0) + armor


def weapon_rank(code: str | None, w: WorldModel, ceiling: int | None) -> tuple[bool, int]:
    """The one rule for which weapon is better (A99): keeping strength within
    ``ceiling`` first (always true with none known), then published damage.
    Compare ranks with ``>``: a higher one is a better weapon."""
    fits = ceiling is None or loadout_strength(w, code) <= ceiling
    return fits, weapon_damage(code) or 0


def _best_weapon(
    armed: str | None, held: list[InventorySupply], w: WorldModel, ceiling: int | None
) -> InventorySupply | None:
    """The held weapon whose rank beats the armed one's (any, for an empty slot)."""
    floor = weapon_rank(armed, w, ceiling) if armed is not None else None
    better = [s for s in held if s.code != armed and (floor is None or weapon_rank(s.code, w, ceiling) > floor)]
    return max(better, key=lambda s: (weapon_rank(s.code, w, ceiling), -s.id), default=None)


def named_equip(w: WorldModel, m: Memory, code: str, armed_owned: bool = False) -> EquipUpgrade | None:
    """Arm or wear held ``code`` as the planner named it, whatever the rule
    ranks it; None once it is on, or when it is not held or was refused, or
    is a weapon while another state holds the armed slot (``armed_owned``)."""
    if code == w.armed_code or code in w.worn_codes.values():
        return None
    if armed_owned and is_weapon(code):
        return None
    s = next((h for h in sorted(_candidates(w), key=lambda h: h.id) if h.code == code), None)
    if s is None:
        return None
    if is_weapon(code):
        return None if (code, ARMED) in m.equip_refused else EquipUpgrade(ARMED, s.id, code)
    slot = wear_slot(code, w)
    if slot is None:
        learn = _best_learn_wear(w, m)
        return learn if learn is not None and learn.code == code else None
    if (code, slot) in m.equip_refused or (w.worn_codes.get(slot) is not None and (None, slot) in m.equip_refused):
        return None
    return EquipUpgrade(slot, s.id, code, remove_first=w.worn_codes.get(slot) is not None)


def best_equip_upgrade(
    w: WorldModel,
    items: dict[str, dict[str, Any]],
    threat: ThreatTable,
    m: Memory,
    armed_owned: bool = False,
    ceiling: int | None = None,
) -> EquipUpgrade | None:
    """The first slot with a clear upgrade, weapon first (by :func:`weapon_rank`
    under ``ceiling``, :func:`hunting_ceiling`), then worn slots in order.

    ``armed_owned`` is True while another state (Heal, Solve, Break) holds the armed
    slot. Those states arm potions and tools on purpose, so the armed slot is
    left to them until they are done. Otherwise a non-weapon in the armed slot
    (a potion left there by an earlier run, free-play run 4) counts as an empty
    slot: any held weapon replaces it.
    """
    refused = m.equip_refused
    held = _candidates(w)

    if not armed_owned:
        armed = w.armed_code if is_weapon(w.armed_code) else None
        weapons = [s for s in held if is_weapon(s.code) and (s.code, ARMED) not in refused]
        s = _best_weapon(armed, weapons, w, ceiling)
        if s is not None:
            return EquipUpgrade(ARMED, s.id, s.code)

    for slot in WEAR_SLOTS:
        worn = w.worn_codes.get(slot)
        if worn is not None and (None, slot) in refused:
            continue  # Remove refused on this slot
        fits = [s for s in held if wear_slot(s.code, w) == slot and (s.code, slot) not in refused]
        s = _best_for_slot(slot, worn, fits, items, threat, "damage_saved")
        if s is None:
            continue
        return EquipUpgrade(slot, s.id, s.code, remove_first=worn is not None)
    return _best_learn_wear(w, m)


def _best_learn_wear(w: WorldModel, m: Memory) -> EquipUpgrade | None:
    """The lowest-id held subtype that may be worn but whose slot neither a
    snapshot nor the Supplies reference gives, to try ``Wear`` on once (A55)."""
    for s in sorted(_candidates(w), key=lambda h: h.id):
        code = s.code
        if never_worn(code) or wear_slot(code, w) is not None:
            continue
        if code in m.equip_not_wearable or code in m.equip_try_refused:
            continue
        return EquipUpgrade("", s.id, code, learn_slot=True)
    return None


def note_last_weapon(m: Memory, w: WorldModel) -> None:
    """Once per decision: the weapon armed now is the one to re-arm after a drink (A24)."""
    if is_weapon(w.armed_code):
        m.last_weapon = w.armed_code


def weapon_to_rearm(w: WorldModel, m: Memory) -> str | None:
    """The weapon to put back after a drink: the one armed now, else the last
    one armed this run while still held, else any held weapon. Never a potion
    or a tool, whatever is in the armed slot (free-play run 4)."""
    if is_weapon(w.armed_code):
        return w.armed_code
    held = sorted((s.id, s.code) for s in w.held_supplies if is_weapon(s.code))
    if any(code == m.last_weapon for _, code in held):
        return m.last_weapon
    return held[0][1] if held else None


def loadout_signature(w: WorldModel) -> tuple:
    return (
        w.armed_code,
        tuple(sorted(w.worn_codes.items())),
        tuple(sorted(s.id for s in w.held_supplies)),
    )


def sync_refusals(m: Memory, w: WorldModel) -> None:
    """Forget refusals once the loadout or inventory has changed since they were noted."""
    sig = loadout_signature(w)
    if m.equip_refused_sig is None:
        m.equip_refused_sig = sig
    elif m.equip_refused_sig != sig:
        m.equip_refused.clear()
        m.equip_try_refused.clear()
        m.equip_refused_sig = sig


def note_equip_result(
    m: Memory,
    w: WorldModel,
    intent: dict | None,
    rejected: bool,
    rejection_code: str | None = None,
) -> None:
    """Mark the (subtype, slot) an Equip ``Arm``, ``Wear`` or ``Remove`` was refused for.

    Only a rejection is marked. An applied ``Remove`` clears an earlier refused
    Remove on that slot. Called before the response's observation is applied, so
    held supplies are still the ones the intent named; the runner's
    ``sync_refusals`` after that observation takes the signature.
    """
    if not intent:
        return
    verb = intent.get("verb")
    if not rejected:
        if verb == "Remove":
            m.equip_refused.discard((None, intent.get("slot")))
        return
    if verb == "Remove":
        slot = intent.get("slot")
        if isinstance(slot, str):
            m.equip_refused.add((None, slot))
    elif verb in ("Arm", "Wear"):
        sid = intent.get("supply_id")
        code = next((s.code for s in w.held_supplies if s.id == sid), None)
        if code is None:
            return
        if verb == "Arm":
            m.equip_refused.add((code, ARMED))
        else:
            slot = wear_slot(code, w)
            if slot is not None:
                m.equip_refused.add((code, slot))
            elif rejection_code == NOT_WEARABLE:
                m.equip_not_wearable.add(code)
            else:
                m.equip_try_refused.add(code)
    else:
        return
    m.equip_refused_sig = None
