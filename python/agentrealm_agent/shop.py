"""Shop: the plan's ``buy`` op, priced supplies in sight, and Takes in flight (A21)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .healing import supply_matches
from .memory import Memory
from .plan import Plan, goal_done
from .travel.strength import loadout_key
from .world import Entity, WorldModel, chebyshev

if TYPE_CHECKING:
    from .states.base import PlayContext

GOAL = "shop"
SHOP_PENDING_TICKS = 10  # a Take not settled by then is stale (A21)
SHOP_MAX_REFUSALS = 3  # rejected Takes of one supply before Shop skips it (A21)


def price_of(e: Entity, items: dict) -> int | None:
    if isinstance(e.gem_price, int) and e.gem_price > 0:
        return e.gem_price
    row = items.get(e.code) or {}
    p = row.get("gem_price")
    return p if isinstance(p, int) and p > 0 else None


def can_afford(w: WorldModel, price: int) -> bool:
    return w.gems is not None and w.gems >= price


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
    """The code the plan's ``buy`` op wants, while it is on top and not bought yet."""
    op = plan_buy_op(ctx, w)
    return [op["code"]] if op is not None else []


def pick_supply(
    w: WorldModel, wants: list[str], items: dict, refusals: dict[int, int] | None = None
) -> Entity | None:
    """Best affordable priced supply matching a wanted code: nearest, then cheapest.
    A supply refused ``SHOP_MAX_REFUSALS`` times is skipped."""
    refusals = refusals or {}
    here = w.pos
    if here is None or not wants:
        return None
    candidates: list[tuple[tuple[int, int, int], Entity]] = []
    for e in w.entities:
        if e.kind != "supply" or not any(supply_matches(want, e.code) for want in wants):
            continue
        if refusals.get(e.id, 0) >= SHOP_MAX_REFUSALS:
            continue
        price = price_of(e, items)
        if price is None or not can_afford(w, price):
            continue
        candidates.append(((chebyshev(e.pos, here), price, e.id), e))
    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0])
    return candidates[0][1]


def _take_failed(m: Memory, plan: Plan | None) -> None:
    """The Take in flight will not land: forget it, and the head buy has paid
    nothing (A112). A later gem drop, from a death say, is no purchase."""
    m.shop_pending = None
    if plan is not None:
        plan.paid_from = None


def note_shop_result(m: Memory, intent: dict | None, applied: bool, plan: Plan | None = None) -> None:
    """Settle the Take in flight from its result: applied settles it, a
    rejection (of it or a ``Drop`` queued before it) counts against that
    supply (``SHOP_MAX_REFUSALS``) and unpays the head buy."""
    pending = m.shop_pending
    if pending is None:
        return
    if not applied:
        m.shop_refusals[pending[0]] = m.shop_refusals.get(pending[0], 0) + 1
        _take_failed(m, plan)
        return
    if intent and intent.get("verb") == "Take" and intent.get("supply_id") == pending[0]:
        m.shop_pending = None


def shop_pending_stale(w: WorldModel, pending: tuple) -> bool:
    """The Take in flight can no longer land: we left the shop cell, or it timed out."""
    _, _, _, pos, map_id, sent = pending
    if w.pos is None or w.map_id != map_id or chebyshev(pos, w.pos) > 1:
        return True
    return w.tick - sent > SHOP_PENDING_TICKS


def sync_shop(w: WorldModel, m: Memory, plan: Plan | None = None) -> None:
    """A gem drop since the Take was sent is the purchase landing (A21).

    The gem drop is read before staleness, so a Take that landed is never
    unpaid by the same read that sees it go stale. A stale Take with no gem
    drop is forgotten, and the head buy has paid nothing, so a later unrelated
    gem drop does not count as a purchase (A112). A loadout,
    gem or map change clears the refusal counts.
    """
    key = (loadout_key(w), w.gems, w.map_id)
    if key != m.shop_refusal_key:
        m.shop_refusal_key = key
        m.shop_refusals.clear()
    pending = m.shop_pending
    if pending is None:
        return
    if pending[2] is not None and w.gems is not None and w.gems < pending[2]:
        m.shop_pending = None
        return
    if shop_pending_stale(w, pending):
        _take_failed(m, plan)
