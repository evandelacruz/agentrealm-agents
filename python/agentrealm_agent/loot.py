"""Loot scoring, carry space, and junk drops (A20, M8)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .item_table import InventorySupply
from .world import Entity, WorldModel, chebyshev

DEFAULT_CARRY_CAPACITY = 10
NON_TRANSFERABLE = frozenset({"pocket_knife"})
LIFE_CODES = frozenset({"life", "heart", "extra_life"})


def is_counter_supply(code: str | None) -> bool:
    """Gems and lives are consumed on pickup (Manual §11)."""
    if not code:
        return False
    if code in LIFE_CODES or code == "gem":
        return True
    return code.startswith("gem_cache_")


def loot_score(code: str | None, items: dict[str, dict[str, Any]]) -> int:
    """Higher is more worth picking up; hearts win (PLAYABLE_AGENT_PLAN)."""
    if not code:
        return 50
    if code in LIFE_CODES:
        return 10_000
    if code == "gem" or code.startswith("gem_cache_"):
        return 5_000
    row = items.get(code) or {}
    price = row.get("gem_price")
    if isinstance(price, int) and price > 0:
        return 500 + min(price, 500)
    if "potion" in code or "food" in code or code in ("golden_cap", "apple", "berry"):
        return 200
    return 50


def supply_worth_looting(e: Entity, items: dict[str, dict[str, Any]]) -> bool:
    if e.kind != "supply":
        return False
    if is_counter_supply(e.code):
        return True
    if e.gem_price is not None:
        return False
    return loot_score(e.code, items) >= 50


def carry_slots_used(w: WorldModel) -> int:
    n = len(w.held_supplies) + len(w.chest_supplies)
    if w.armed_code:
        n += 1
    n += len(w.worn_codes)
    return n


def inventory_full(w: WorldModel) -> bool:
    return carry_slots_used(w) >= w.carry_capacity


def droppable_supplies(w: WorldModel) -> list[InventorySupply]:
    """Held and stowed supplies that ``Drop`` may shed (Manual §11)."""
    out: list[InventorySupply] = []
    for s in w.held_supplies + w.chest_supplies:
        if s.code in NON_TRANSFERABLE:
            continue
        out.append(s)
    return out


def worst_droppable(w: WorldModel, items: dict[str, dict[str, Any]]) -> InventorySupply | None:
    candidates = droppable_supplies(w)
    if not candidates:
        return None
    return min(candidates, key=lambda s: (loot_score(s.code, items), s.id))


def needs_drop_for_supply(w: WorldModel, supply: Entity, items: dict[str, dict[str, Any]]) -> InventorySupply | None:
    """When full, drop junk only if the pickup beats what we would shed."""
    if is_counter_supply(supply.code):
        return None
    if not inventory_full(w):
        return None
    drop = worst_droppable(w, items)
    if drop is None:
        return None
    if loot_score(supply.code, items) <= loot_score(drop.code, items):
        return None
    return drop


def survival_blocks_loot(w: WorldModel, policy) -> bool:
    """Hostiles and hazards stay in Explore reflexes until Escape/Fight land (A20)."""
    here = w.pos
    if here is None:
        return True
    hazards = {p for p, b in w.view.tiles.items() if b in policy.avoid_blocks}
    if here in hazards:
        return True
    hostiles = [
        e for e in w.entities if e.kind in policy.hostile and chebyshev(e.pos, here) <= policy.hostile_range
    ]
    return bool(hostiles and policy.on_hostile != "ignore")


@dataclass(frozen=True)
class LootTarget:
    kind: str  # supply | chest | death_chest
    pos: tuple[int, int]
    entity_id: int
    score: int


def _chest_score(chest_id: int, w: WorldModel, items: dict[str, dict[str, Any]]) -> int:
    contents = w.chest_contents.get(chest_id)
    if not contents:
        return 0
    return max(loot_score(s.code, items) for s in contents)


def loot_targets(w: WorldModel, items: dict[str, dict[str, Any]]) -> list[LootTarget]:
    here = w.pos
    if here is None:
        return []
    out: list[LootTarget] = []
    for e in w.entities:
        if e.kind == "supply" and supply_worth_looting(e, items):
            out.append(LootTarget("supply", e.pos, e.id, loot_score(e.code, items)))
    if w.death_chest is not None and w.death_chest[0] == w.map_id:
        _, at, cid = w.death_chest
        contents = w.chest_contents.get(cid)
        if contents is None or contents:
            score = _chest_score(cid, w, items) if contents else 500
            out.append(LootTarget("death_chest", at, cid, max(score, 500)))
    for e in w.entities:
        if e.kind != "chest":
            continue
        contents = w.chest_contents.get(e.id)
        if contents is None or not contents:
            continue
        out.append(LootTarget("chest", e.pos, e.id, _chest_score(e.id, w, items)))
    out.sort(key=lambda t: (-t.score, chebyshev(t.pos, here), t.entity_id))
    return out


def best_loot_target(w: WorldModel, items: dict[str, dict[str, Any]]) -> LootTarget | None:
    targets = loot_targets(w, items)
    return targets[0] if targets else None

