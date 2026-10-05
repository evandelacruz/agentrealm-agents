"""Shop wants, priced supplies in sight, and buy-signal consumption (A21)."""

from __future__ import annotations

from .directives import PARAM_DEFAULTS
from .healing import DEFAULT_BUY_POTION, POTION_CODES, potion_count
from .knowledge_base import KnowledgeBase
from .loot import inventory_full, worst_droppable
from .memory import Memory
from .plan import Plan, goal_done
from .states.base import PlayContext
from .world import Entity, WorldModel, chebyshev

GOAL = "shop"


def _price(e: Entity, items: dict) -> int | None:
    if isinstance(e.gem_price, int) and e.gem_price > 0:
        return e.gem_price
    row = items.get(e.code) or {}
    p = row.get("gem_price")
    return p if isinstance(p, int) and p > 0 else None


def can_afford(w: WorldModel, price: int) -> bool:
    return w.gems is not None and w.gems >= price


def supply_matches(want: str, code: str) -> bool:
    if want == code:
        return True
    if want in POTION_CODES and code in POTION_CODES:
        return True
    return False


def holds_code(w: WorldModel, code: str) -> bool:
    for s in w.held_supplies + w.chest_supplies:
        if supply_matches(code, s.code):
            return True
    return False


def plan_buy_op(ctx: PlayContext, w: WorldModel) -> dict | None:
    plan = ctx.plan
    if plan is None:
        return None
    op = plan.current()
    if op is None or op.get("op") != "buy":
        return None
    if goal_done(op, w, plan):
        return None
    return op


def wanted_codes(w: WorldModel, ctx: PlayContext) -> list[str]:
    """Codes Shop should try to buy this window, best effort first."""
    out: list[str] = []
    seen: set[str] = set()

    def add(code: str) -> None:
        if code and code not in seen:
            seen.add(code)
            out.append(code)

    op = plan_buy_op(ctx, w)
    if op is not None:
        add(op["code"])

    for sig in ctx.memory.buy_signals:
        code = sig.get("code")
        if isinstance(code, str) and not holds_code(w, code):
            add(code)

    reserve = int(ctx.params.get("potion_reserve", PARAM_DEFAULTS["potion_reserve"]))
    if potion_count(w) < reserve:
        add(DEFAULT_BUY_POTION)

    return out


def priced_in_sight(w: WorldModel, items: dict) -> list[Entity]:
    out: list[Entity] = []
    for e in w.entities:
        if e.kind != "supply":
            continue
        price = _price(e, items)
        if price is not None and can_afford(w, price):
            out.append(e)
    return out


def pick_supply(w: WorldModel, wants: list[str], items: dict) -> Entity | None:
    """Best priced supply matching a wanted code: nearest, then cheapest."""
    here = w.pos
    if here is None or not wants:
        return None
    candidates: list[tuple[tuple[int, int, int], Entity]] = []
    for e in priced_in_sight(w, items):
        if not any(supply_matches(want, e.code) for want in wants):
            continue
        price = _price(e, items)
        assert price is not None
        candidates.append(((chebyshev(e.pos, here), price, e.id), e))
    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0])
    return candidates[0][1]


def consume_buy_signal(m: Memory, code: str) -> None:
    """Drop Heal's first matching ``buy`` op once Shop acts on it (A10, A21)."""
    for i, sig in enumerate(m.buy_signals):
        c = sig.get("code")
        if c == code or (isinstance(c, str) and code in POTION_CODES and c in POTION_CODES):
            del m.buy_signals[i]
            return


def shop_take_intents(
    w: WorldModel,
    items: dict,
    supply: Entity,
) -> list[dict] | None:
    """``Drop`` junk when full, then ``Take`` the priced supply."""
    from .states.intents import drop, take

    price = _price(supply, items)
    if price is None or not can_afford(w, price):
        return None
    if inventory_full(w):
        shed = worst_droppable(w, items)
        if shed is None:
            return None
        return [drop(shed.id), take(supply.id)]
    return [take(supply.id)]


def note_shop_purchase(m: Memory, code: str) -> None:
    consume_buy_signal(m, code)


def knowledge_items(knowledge: KnowledgeBase | None) -> dict:
    return (knowledge.items if knowledge else {}) or {}
