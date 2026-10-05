"""Equip scoring and upgrade selection (A19, A55, M8).

Only sourced facts decide what goes where:

- **Weapons** are the exact codes GAME_NOTES names as weapons
  (``break_memory.WEAPONS``). Nothing else is armed by Equip.
- **Worn slots** come from the snapshot's ``worn`` by slot: the world model
  files a subtype seen worn in a slot there for the run (``WorldModel.worn_slots``).
  A held subtype never seen worn has no slot for scoring until Equip tries
  ``Wear`` once (A55) and reads which slot ``worn`` shows. Slot-learn tries
  skip weapons, manual break tools, capability-tagged subtypes, and anything
  with no shop price in the item table (keys and junk stay for Solve and Loot).

Each slot compares like with like, never across units: learned per-NPC-type
hits (``weapon_damage`` for weapons, ``damage_saved`` for armor) on the hostile
types both items have measured, weighted by the threat table; failing that,
``gem_price`` when both have one. The non-transferable starting kit has no shop
price and counts as 0 gems. Items with nothing comparable are left alone. A
swap needs a clear gain (``MIN_GAIN_RATIO``), so ties and noise never swap.

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

from .break_memory import MANUAL_CAPABILITIES, WEAPONS
from .healing import FOOD_CODES, POTION_CODES
from .item_table import InventorySupply
from .loot import NON_TRANSFERABLE
from .memory import Memory
from .threat import ThreatTable
from .world import WorldModel

ARMED = "armed"
WEAR_SLOTS = ("head", "body", "legs", "feet", "accessory")
NOT_WEARABLE = "not_wearable"

# A swap must beat what is in the slot by more than this factor: equal or near-equal
# scores (one rolled hit apart, or a re-price) keep what is equipped.
MIN_GAIN_RATIO = 1.25


def is_consumable(code: str | None) -> bool:
    return bool(code) and (code in FOOD_CODES or code in POTION_CODES)


def is_weapon(code: str | None) -> bool:
    return bool(code) and code in WEAPONS


def wear_slot(code: str | None, w: WorldModel) -> str | None:
    if not code or is_consumable(code) or is_weapon(code):
        return None
    slot = w.worn_slots.get(code) if code else None
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


def best_equip_upgrade(
    w: WorldModel,
    items: dict[str, dict[str, Any]],
    threat: ThreatTable,
    m: Memory,
    armed_owned: bool = False,
) -> EquipUpgrade | None:
    """The first slot with a clear upgrade, weapon first, then worn slots in order.

    ``armed_owned`` is True while another state (Heal, Solve, Break) holds the armed
    slot. Those states arm potions and tools on purpose, so the armed
    slot is only touched while it holds a weapon or nothing.
    """
    refused = m.equip_refused
    held = _candidates(w)

    armed = w.armed_code
    if not armed_owned and (armed is None or is_weapon(armed)):
        weapons = [s for s in held if is_weapon(s.code) and (s.code, ARMED) not in refused]
        s = _best_for_slot(ARMED, armed, weapons, items, threat, "weapon_damage")
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
    return _best_learn_wear(w, m, items)


def _learn_wear_subtype(code: str, items: dict[str, dict[str, Any]]) -> bool:
    """Held shop-priced armor-like gear that is not a sourced break tool (A55)."""
    if not code or is_weapon(code) or code in MANUAL_CAPABILITIES:
        return False
    row = items.get(code) or {}
    if row.get("capabilities"):
        return False
    return _price(code, items) is not None


def _best_learn_wear(w: WorldModel, m: Memory, items: dict[str, dict[str, Any]]) -> EquipUpgrade | None:
    """One held armor subtype with no known slot to try ``Wear`` on (A55)."""
    for s in sorted(_candidates(w), key=lambda h: h.id):
        code = s.code
        if wear_slot(code, w) is not None:
            continue
        if not _learn_wear_subtype(code, items):
            continue
        if code in m.equip_not_wearable or code in m.equip_try_refused:
            continue
        return EquipUpgrade("", s.id, code, learn_slot=True)
    return None


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
