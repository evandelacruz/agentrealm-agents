"""Break memory per (block, capability) and grid break costs (A28).

A supply's capabilities come only from sourced facts: the manual's per-class
rules applied to the exact subtype codes docs/GAME_NOTES.md names, plus any
capability a break with that subtype armed has opened, as learned in
``KnowledgeBase.breaks``. No read serves an item's class, so an unlisted code
carries none (PLAN.md Server gaps). Break attempts are stored with keys
``"<map_id>,<x>,<y>,<capability>"``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .executor.pacing import DEFAULT_WEAPON_COOLDOWN_TICKS
from .item_table import InventorySupply
from .knowledge_base import KnowledgeBase
from .plan import CAPABILITIES
from .world import Pos, WorldModel

BREAKABLE = frozenset({"bush", "tree", "rock", "mountain", "wall"})
REGROWTH_TICKS = 600  # cut bush grew back in 60 s (GAME_NOTES Breaking blocks)
BREAK_BASE_COST = DEFAULT_WEAPON_COOLDOWN_TICKS + 1
# Refusals of a break that say "not yet", not "cannot": the pair is not marked failed.
TRANSIENT_BREAK_REJECTIONS = frozenset({"attack_cooldown"})

# Manual per-class rules (§5.3, §11, §16) on the codes GAME_NOTES names:
# every sword cuts and chops, the starting pocket knife cuts, every mallet
# smashes, matches and torches burn. No bomb code is sourced yet.
MANUAL_CAPABILITIES: dict[str, frozenset[str]] = {
    "bronze_sword": frozenset({"cut", "chop"}),
    "pocket_knife": frozenset({"cut"}),
    "bronze_mallet": frozenset({"smash"}),
    "matches": frozenset({"burn"}),
    "torch": frozenset({"burn"}),
}
# Weapons are not used up by a break; tools are (M §11).
WEAPONS = frozenset({"bronze_sword", "pocket_knife", "bronze_mallet"})


def break_key(map_id: int, pos: Pos, capability: str) -> str:
    return f"{map_id},{pos[0]},{pos[1]},{capability}"


def capabilities_for_code(code: str, kb: KnowledgeBase | None = None) -> frozenset[str]:
    """Sourced capabilities of subtype ``code``: manual rules plus breaks it opened."""
    if not code:
        return frozenset()
    caps = set(MANUAL_CAPABILITIES.get(code, ()))
    if kb is not None:
        with kb.lock:
            rows = list(kb.breaks.values())
        for row in rows:
            if isinstance(row, dict) and row.get("result") == "opened" and row.get("code") == code:
                cap = row.get("capability")
                if cap in CAPABILITIES:
                    caps.add(cap)
    return frozenset(caps)


def _break_row(kb: KnowledgeBase | None, key: str) -> dict[str, Any] | None:
    if kb is None:
        return None
    with kb.lock:
        row = kb.breaks.get(key)
    return row if isinstance(row, dict) else None


def attempt_failed(kb: KnowledgeBase | None, map_id: int | None, pos: Pos, capability: str) -> bool:
    if map_id is None:
        return False
    row = _break_row(kb, break_key(map_id, pos, capability))
    if row is None:
        return False
    result = row.get("result")
    return result in ("applied_no_effect", "failed", "no_effect")


def attempt_open(kb: KnowledgeBase | None, map_id: int | None, pos: Pos, capability: str) -> bool:
    if map_id is None:
        return False
    row = _break_row(kb, break_key(map_id, pos, capability))
    return isinstance(row, dict) and row.get("result") == "opened"


def record_attempt(
    kb: KnowledgeBase | None,
    *,
    map_id: int | None,
    pos: Pos,
    capability: str,
    result: str,
    block_after: str | None = None,
    tick: int | None = None,
    code: str | None = None,
) -> None:
    if kb is None or map_id is None or capability not in CAPABILITIES:
        return
    entry: dict[str, Any] = {"result": result, "capability": capability}
    if code:
        entry["code"] = code
    if block_after is not None:
        entry["block_after"] = block_after
    if tick is not None:
        entry["tick"] = tick
    with kb.lock:
        kb.breaks[break_key(map_id, pos, capability)] = entry


def held_supplies(w: WorldModel) -> list[InventorySupply]:
    """Supplies Break can arm: held, plus the armed one. Worn gear is not armable."""
    out = list(w.held_supplies)
    if w.armed_code and not any(s.code == w.armed_code for s in w.held_supplies):
        out.append(InventorySupply(-1, w.armed_code))
    return out


def held_capabilities(w: WorldModel, kb: KnowledgeBase | None = None) -> set[str]:
    caps: set[str] = set()
    for s in held_supplies(w):
        caps |= capabilities_for_code(s.code, kb)
    return caps


@dataclass(frozen=True)
class BreakChoice:
    pos: Pos
    capability: str
    supply: InventorySupply
    cost: int


def _tool_gem_cost(kb: KnowledgeBase | None, code: str) -> int:
    if kb is None or not code:
        return 0
    with kb.lock:
        row = kb.items.get(code)
    if not isinstance(row, dict):
        return 0
    price = row.get("gem_price")
    return int(price) if isinstance(price, int) and not isinstance(price, bool) and price > 0 else 0


def _capability_order(cap: str) -> tuple[int, str]:
    # Weapons (free) before consumable tools; cut before smash before burn before blast.
    order = {"cut": 0, "chop": 1, "smash": 2, "burn": 3, "blast": 4}
    return order.get(cap, 9), cap


def pick_supply_for_capability(
    w: WorldModel, capability: str, kb: KnowledgeBase | None = None
) -> InventorySupply | None:
    """Cheapest held supply that provides ``capability`` (weapons first)."""
    cands: list[tuple[tuple[int, int, str], InventorySupply]] = []
    for s in w.held_supplies:
        caps = capabilities_for_code(s.code, kb)
        if capability not in caps:
            continue
        weapon = 0 if s.code in WEAPONS else 1
        cands.append(((weapon, _capability_order(capability)[0], s.code), s))
    if not cands:
        armed = w.armed_code
        if armed and capability in capabilities_for_code(armed, kb):
            sid = next((s.id for s in w.held_supplies if s.code == armed), -1)
            return InventorySupply(sid, armed)
        return None
    cands.sort(key=lambda t: t[0])
    return cands[0][1]


def untried_capabilities(kb: KnowledgeBase | None, map_id: int | None, pos: Pos, held: Iterable[str]) -> list[str]:
    out = [c for c in held if c in CAPABILITIES and not attempt_failed(kb, map_id, pos, c)]
    out.sort(key=_capability_order)
    return out


def break_step_cost(kb: KnowledgeBase | None, code: str) -> int:
    """Weapon cooldown plus one, plus the gem price of a tool a break uses up."""
    return BREAK_BASE_COST + (0 if code in WEAPONS else _tool_gem_cost(kb, code))


def breakable_block(w: WorldModel, pos: Pos) -> str | None:
    block = w.view.tiles.get(pos)
    if block in BREAKABLE:
        return block
    return None


def _step_toward(p: Pos, goal: Pos) -> Pos:
    nx = p[0] + (1 if goal[0] > p[0] else -1 if goal[0] < p[0] else 0)
    ny = p[1] + (1 if goal[1] > p[1] else -1 if goal[1] < p[1] else 0)
    return nx, ny


def _choice_at(w: WorldModel, kb: KnowledgeBase | None, pos: Pos, held: set[str]) -> BreakChoice | None:
    if w.map_id is None or breakable_block(w, pos) is None:
        return None
    for cap in untried_capabilities(kb, w.map_id, pos, held):
        supply = pick_supply_for_capability(w, cap, kb)
        if supply is None:
            continue
        return BreakChoice(pos, cap, supply, break_step_cost(kb, supply.code))
    return None


def nominate_on_path(
    w: WorldModel,
    kb: KnowledgeBase | None,
    start: Pos,
    goal: Pos,
) -> BreakChoice | None:
    """The first breakable on the straight route from ``start`` to ``goal`` we can still try.

    Only cells on that route are candidates: a breakable beside or behind us
    does not open the way to ``goal``.
    """
    held = held_capabilities(w, kb)
    if not held or w.map_id is None:
        return None
    p = start
    while p != goal:
        p = _step_toward(p, goal)
        choice = _choice_at(w, kb, p, held)
        if choice is not None:
            return choice
    return None


def break_costs_for_planning(
    w: WorldModel,
    kb: KnowledgeBase | None,
    goal: Pos | None,
) -> dict[Pos, int]:
    """Break step costs for breakables on the straight route to ``goal`` with an untried capability."""
    if w.map_id is None or w.pos is None:
        return {}
    held = held_capabilities(w, kb)
    if not held:
        return {}
    cells: set[Pos] = set()
    if goal is not None:
        p = w.pos
        while p != goal:
            p = _step_toward(p, goal)
            if breakable_block(w, p) is not None:
                cells.add(p)
    costs: dict[Pos, int] = {}
    for p in cells:
        if breakable_block(w, p) is None:
            continue
        for cap in untried_capabilities(kb, w.map_id, p, held):
            supply = pick_supply_for_capability(w, cap, kb)
            if supply is None:
                continue
            costs[p] = break_step_cost(kb, supply.code)
            break
    return costs
