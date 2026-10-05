"""Equip scoring and upgrade selection (A19, M8).

Armor is scored by ``damage_saved`` against hostile types in the threat table,
weighted by each type's expected hit damage. Weapons use ``weapon_damage`` the
same way. With no learned hits, ``gem_price`` from the item table breaks ties.
Consumables and compose fragments are never equipped; they stay for Heal and
Solve (PLAYABLE_AGENT_PLAN Gear).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .healing import FOOD_CODES, POTION_CODES
from .item_table import InventorySupply
from .loot import NON_TRANSFERABLE
from .threat import ThreatTable
from .world import WorldModel

WEAR_SLOTS = ("head", "body", "legs", "feet", "accessory")

# Subtype code substrings for armed gear (GAME_NOTES Items; capabilities not served).
ARMED_HINTS = (
    "sword",
    "knife",
    "mallet",
    "axe",
    "bow",
    "dagger",
    "hammer",
    "torch",
    "matches",
    "wand",
)


def is_consumable(code: str | None) -> bool:
    return bool(code) and (code in FOOD_CODES or code in POTION_CODES)


def is_armed_gear(code: str | None) -> bool:
    if not code or is_consumable(code):
        return False
    return any(h in code for h in ARMED_HINTS)


def infer_wear_slot(code: str | None) -> str | None:
    """Map a held subtype to a worn slot, or None when it should not be worn."""
    if not code or is_consumable(code) or is_armed_gear(code):
        return None
    if "chest" in code and "bronze" not in code and "mail" not in code:
        # Carried chest upgrades (e.g. middle_chest) are not worn armor.
        if code.endswith("_chest") or code == "middle_chest":
            return None
    if any(k in code for k in ("map", "cloak", "disguise", "goggles")):
        return "accessory"
    if any(k in code for k in ("helm", "hat", "hood", "goggle")):
        return "head"
    if any(k in code for k in ("boot", "feet", "shoe", "sandal")):
        return "feet"
    if any(k in code for k in ("leg", "greave", "pant")):
        return "legs"
    if any(k in code for k in ("mail", "body", "plate", "vest", "armor", "robe")):
        return "body"
    return None


def _gem_price_score(row: dict[str, Any]) -> int:
    price = row.get("gem_price")
    if isinstance(price, int) and not isinstance(price, bool) and price > 0:
        return price
    return 0


def _weighted_per_npc(row: dict[str, Any], field: str, threat: ThreatTable) -> int:
    per_type = row.get(field)
    if not isinstance(per_type, dict):
        return 0
    total = 0
    for key, amount in threat.by_type.items():
        if key[0] != "npc" or not isinstance(amount, int) or amount <= 0:
            continue
        hit = per_type.get(key[1])
        if isinstance(hit, int) and not isinstance(hit, bool) and hit > 0:
            total += hit * amount
    return total


def armor_score(code: str | None, items: dict[str, dict[str, Any]], threat: ThreatTable) -> int:
    if not code:
        return 0
    row = items.get(code) or {}
    saved = _weighted_per_npc(row, "damage_saved", threat)
    if saved > 0:
        return saved
    return _gem_price_score(row)


def weapon_score(code: str | None, items: dict[str, dict[str, Any]], threat: ThreatTable) -> int:
    if not code:
        return 0
    row = items.get(code) or {}
    damage = _weighted_per_npc(row, "weapon_damage", threat)
    if damage > 0:
        return damage
    return _gem_price_score(row)


def _held_candidates(w: WorldModel) -> list[InventorySupply]:
    out: list[InventorySupply] = []
    for s in w.held_supplies:
        if s.fragment is not None:
            continue
        if s.code in NON_TRANSFERABLE and not is_armed_gear(s.code):
            continue
        out.append(s)
    return out


@dataclass(frozen=True)
class EquipUpgrade:
    delta: int
    intents: tuple[dict, ...]
    reason: str


def best_equip_upgrade(
    w: WorldModel,
    items: dict[str, dict[str, Any]],
    threat: ThreatTable,
) -> EquipUpgrade | None:
    """The best held upgrade to armed or one worn slot, or None."""
    candidates: list[EquipUpgrade] = []

    armed = w.armed_code
    armed_sc = weapon_score(armed, items, threat)
    for s in _held_candidates(w):
        if not is_armed_gear(s.code):
            continue
        sc = weapon_score(s.code, items, threat)
        if sc > armed_sc:
            candidates.append(
                EquipUpgrade(
                    sc - armed_sc,
                    ({"verb": "Arm", "supply_id": s.id},),
                    f"arm {s.code}",
                )
            )

    for slot in WEAR_SLOTS:
        worn = w.worn_codes.get(slot)
        worn_sc = armor_score(worn, items, threat)
        for s in _held_candidates(w):
            if infer_wear_slot(s.code) != slot:
                continue
            sc = armor_score(s.code, items, threat)
            if sc <= worn_sc:
                continue
            intents: list[dict] = []
            if worn:
                intents.append({"verb": "Remove", "slot": slot})
            intents.append({"verb": "Wear", "supply_id": s.id})
            candidates.append(
                EquipUpgrade(
                    sc - worn_sc,
                    tuple(intents),
                    f"wear {s.code} ({slot})",
                )
            )

    if not candidates:
        return None
    return max(candidates, key=lambda u: (u.delta, u.reason))


def equip_guard(w: WorldModel, items: dict[str, dict[str, Any]], threat: ThreatTable) -> bool:
    return best_equip_upgrade(w, items, threat) is not None
