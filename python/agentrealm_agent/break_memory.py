"""Break memory per (block, capability) and grid break costs (A28).

Capabilities come from the manual's per-class rules (docs/GAME_NOTES.md
Movement and blocks) until a read serves them; break attempts are stored on
``KnowledgeBase.breaks`` with keys ``"<map_id>,<x>,<y>,<capability>"``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .executor.pacing import DEFAULT_WEAPON_COOLDOWN_TICKS
from .item_table import InventorySupply
from .knowledge_base import KnowledgeBase
from .plan import CAPABILITIES
from .world import Pos, WorldModel, chebyshev

BREAKABLE = frozenset({"bush", "tree", "rock", "mountain", "wall"})
REGROWTH_TICKS = 600  # cut bush grew back in 60 s (GAME_NOTES Breaking blocks)
BREAK_BASE_COST = DEFAULT_WEAPON_COOLDOWN_TICKS + 1

# Manual §11, §16 (GAME_NOTES Movement and blocks).
_WEAPON_CUT = frozenset({"cut", "chop"})


def break_key(map_id: int, pos: Pos, capability: str) -> str:
    return f"{map_id},{pos[0]},{pos[1]},{capability}"


def capabilities_for_code(code: str) -> frozenset[str]:
    """Capabilities a supply subtype carries by manual class rules."""
    if not code:
        return frozenset()
    lower = code.lower()
    if "knife" in lower or "sword" in lower:
        return _WEAPON_CUT
    if "mallet" in lower:
        return frozenset({"smash"})
    if "match" in lower or "torch" in lower:
        return frozenset({"burn"})
    if "bomb" in lower:
        return frozenset({"blast"})
    return frozenset()


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
) -> None:
    if kb is None or map_id is None or capability not in CAPABILITIES:
        return
    entry: dict[str, Any] = {"result": result}
    if block_after is not None:
        entry["block_after"] = block_after
    if tick is not None:
        entry["tick"] = tick
    with kb.lock:
        kb.breaks[break_key(map_id, pos, capability)] = entry


def held_supplies(w: WorldModel) -> list[InventorySupply]:
    out = list(w.held_supplies)
    if w.armed_code:
        armed = next((s for s in w.held_supplies if s.code == w.armed_code), None)
        if armed is None and w.armed_code:
            out.append(InventorySupply(-1, w.armed_code))
    for code in w.worn_codes.values():
        if code:
            out.append(InventorySupply(-2, code))
    return out


def held_capabilities(w: WorldModel) -> set[str]:
    caps: set[str] = set()
    for s in held_supplies(w):
        caps |= capabilities_for_code(s.code)
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


def pick_supply_for_capability(w: WorldModel, capability: str) -> InventorySupply | None:
    """Cheapest held supply that provides ``capability`` (weapons first)."""
    cands: list[tuple[tuple[int, int, str], InventorySupply]] = []
    for s in w.held_supplies:
        caps = capabilities_for_code(s.code)
        if capability not in caps:
            continue
        weapon = 0 if capability in _WEAPON_CUT and ("sword" in s.code or "knife" in s.code) else 1
        cands.append(((weapon, _capability_order(capability)[0], s.code), s))
    if not cands:
        armed = w.armed_code
        if armed and capability in capabilities_for_code(armed):
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
    return BREAK_BASE_COST + _tool_gem_cost(kb, code)


def breakable_block(w: WorldModel, pos: Pos) -> str | None:
    block = w.view.tiles.get(pos)
    if block in BREAKABLE:
        return block
    return None


def _step_toward(p: Pos, goal: Pos) -> Pos:
    nx = p[0] + (1 if goal[0] > p[0] else -1 if goal[0] < p[0] else 0)
    ny = p[1] + (1 if goal[1] > p[1] else -1 if goal[1] < p[1] else 0)
    return nx, ny


def _choice_at(
    w: WorldModel, kb: KnowledgeBase | None, pos: Pos, held: set[str], start: Pos
) -> BreakChoice | None:
    if w.map_id is None or breakable_block(w, pos) is None:
        return None
    for cap in untried_capabilities(kb, w.map_id, pos, held):
        supply = pick_supply_for_capability(w, cap)
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
    """A breakable cell on the best route toward ``goal`` we can still try."""
    held = held_capabilities(w)
    if not held or w.map_id is None:
        return None
    best: BreakChoice | None = None
    best_rank: tuple[int, int] | None = None

    def consider(p: Pos) -> None:
        nonlocal best, best_rank
        choice = _choice_at(w, kb, p, held, start)
        if choice is None:
            return
        rank = (chebyshev(start, p), choice.cost)
        if best_rank is None or rank < best_rank:
            best, best_rank = choice, rank

    for p, block in w.view.tiles.items():
        if block in BREAKABLE:
            consider(p)
    p = start
    while p != goal:
        p = _step_toward(p, goal)
        consider(p)
    return best


def enclosing_break_choice(w: WorldModel, kb: KnowledgeBase | None) -> BreakChoice | None:
    """Breakable neighbour when every walkable neighbour is blocked or occupied."""
    here = w.pos
    if here is None or w.map_id is None:
        return None
    open_n = w.open_neighbours(here, set())
    if open_n:
        return None
    held = held_capabilities(w)
    if not held:
        return None
    best: BreakChoice | None = None
    for p in w.neighbours(here):
        if breakable_block(w, p) is None:
            continue
        for cap in untried_capabilities(kb, w.map_id, p, held):
            supply = pick_supply_for_capability(w, cap)
            if supply is None:
                continue
            choice = BreakChoice(p, cap, supply, break_step_cost(kb, supply.code))
            if best is None or choice.cost < best.cost:
                best = choice
    return best


def break_costs_for_planning(
    w: WorldModel,
    kb: KnowledgeBase | None,
    goal: Pos | None,
    *,
    nominated: set[Pos] | None = None,
) -> dict[Pos, int]:
    """Per-cell break step costs for nominated breakables with an untried capability."""
    if w.map_id is None or w.pos is None:
        return {}
    held = held_capabilities(w)
    if not held:
        return {}
    cells: set[Pos] = set(nominated or ())
    if goal is not None and w.pos is not None:
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
            supply = pick_supply_for_capability(w, cap)
            if supply is None:
                continue
            costs[p] = break_step_cost(kb, supply.code)
            break
    return costs
