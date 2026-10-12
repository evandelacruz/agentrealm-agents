"""Shop: carry out the plan's ``buy`` op (A21), walking to the shop first
when it is out of sight (A71)."""

from __future__ import annotations

from .. import targets as targets_mod
from ..investigation import in_sight
from ..knowledge_base import knowledge_items
from ..loot import Pickup, loot_score
from ..memory import Memory
from ..navigation import cost_path
from ..navigation import stuck as nav_stuck
from ..pathing import enters_reach, grid_params, nav_search, next_step
from ..healing import supply_matches
from ..plan import GoalOp, explore_targets
from ..shop import (
    GOAL,
    can_afford,
    pick_supply,
    price_of,
    wanted_codes,
)
from ..sellers import SELLER_SEARCH_RADIUS, Seller, sellers_of
from ..travel.resolve import ResolvedDestination, given_up_travel
from ..world import Entity, Pos, WorldModel, chebyshev
from .base import PlayContext, State, StateOutcome, my_op, op_walk, top_op
from .explore import explore_outcome
from .intents import set_position
from .room import planned_take
from .travel import walk_to
from .way import clear_the_way

# The commitment of the seller a ``buy`` walks to while its item is out of sight (A71, A131).
SHOP_TARGET = "shop"


class ShopState(State):
    """Executor for ``buy``: takes a priced supply in sight that the op wants,
    walking up beside it first.

    With none in sight, a seller is a prerequisite (A71): Shop walks only
    toward a seller known to sell the item (``sellers``, A131), the cheapest
    walk first, committed until it comes into sight: a shop cell seen selling
    it, or the place a clue or the op names, searched round once there. It
    never walks to a shop that does not sell it, and never leaves the buy to
    the safe default: with no way it opens the block on the way a held item
    opens, else asks the planner and holds (``way``, A120). The op is dropped
    when no seller of the item is known, when every known seller has been
    reached without it for sale, or when it is in sight and costs more gems
    than we have.
    With a full pack, room is the pack rule's (``pack.make_room``, A102): the
    op's ``drop`` is the only way to make room; without one the op goes back
    to the planner.
    """

    name = "Shop"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        if ctx.policy.kind != "scripted" or not world.alive or world.pos is None:
            return False
        return my_op(ctx, self.name) is not None

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not self.guard(world, ctx)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        op = my_op(ctx, self.name)
        out = shop_outcome(world, ctx, self.name)
        if op is None or out.intents or out.wait or ctx.plan is None or ctx.plan.current() is not op:
            return out
        # Nothing to take and no walk to make: ask the planner and hold (A120).
        return clear_the_way(world, ctx, None, out.reason, self.name, op)


def _target(w: WorldModel, ctx: PlayContext) -> Entity | None:
    """The priced supply Shop would buy now. Pure: guard calls it."""
    wants = wanted_codes(w, ctx)
    if not wants:
        return None
    return pick_supply(w, wants, knowledge_items(ctx.knowledge), ctx.memory.shop_refusals)


def shop_outcome(w: WorldModel, ctx: PlayContext, state: str) -> StateOutcome:
    supply = _target(w, ctx)
    if supply is None:
        return _toward_shop(w, ctx, state)
    targets_mod.release(ctx.memory, SHOP_TARGET)
    here = w.pos
    assert here is not None
    label = supply.code or str(supply.id)
    if chebyshev(supply.pos, here) <= 1:
        items = knowledge_items(ctx.knowledge)
        price = price_of(supply, items)
        if price is None or not can_afford(w, price):
            return StateOutcome(None, f"cannot buy {label}", state=state)
        # Room by the pack rule (A102): the supply scores at the price it is sold for.
        priced = {**items, supply.code: {**(items.get(supply.code) or {}), "gem_price": price}}
        p = Pickup(supply.id, supply.code, supply.pos, None, loot_score(supply.code, priced))
        out = planned_take(w, ctx, p, state)
        if out.intents and out.intents[-1].get("verb") == "Take":
            # Settled when this Take lands or gems drop (runner, sync_shop).
            ctx.memory.shop_pending = (supply.id, supply.code, w.gems, supply.pos, w.map_id, w.tick)
            if ctx.plan is not None:
                # The gems held as the head buy pays (A106): it is done once they
                # drop. Every Take re-bases it, so gems gained after one that was
                # refused or went stale never hide the purchase that lands.
                ctx.plan.paid_from = w.gems
            out.reason = f"buy {label}" if len(out.intents) == 1 else f"buy {label}: {out.reason}"
        return out
    walk = op_walk(w, ctx)
    step = _step_toward(w, ctx.memory, ctx, supply.pos, walk)
    if step is None:
        why = f"no step toward {label} at {supply.pos[0]},{supply.pos[1]}"
        return clear_the_way(w, ctx, supply.pos, why, state, top_op(ctx), reach=walk[2])
    return StateOutcome([set_position(step)], f"shop {label} → {supply.pos}", state=state)


def _step_toward(
    w: WorldModel, m: Memory, ctx: PlayContext, goal, walk: tuple[set[Pos], set[Pos], set[Pos]]
) -> object | None:
    """A step toward a supply for sale in sight, on the op's walk sets: kept
    out of the reach of the hostiles seen that the engagement would not fight
    (``op_walk``, A128); with no way clear of it, no step, and Shop asks."""
    plan_avoid, plan_costly, reach = walk
    kept = m.goal == GOAL and m.path and m.path[-1] == goal and next_step(w, plan_avoid, m.path)
    if kept and not enters_reach(w, m.path, reach):
        return next_step(w, plan_avoid, m.path)
    found = cost_path(w, goal, grid_params(ctx.policy, plan_avoid, plan_costly), nav=nav_search(m, w, GOAL, goal))
    if enters_reach(w, found, reach):
        m.path, m.goal = [], ""
        return None
    step = next_step(w, plan_avoid, found)
    if step is not None:
        m.path, m.goal = found, GOAL
    return step


def _toward_shop(w: WorldModel, ctx: PlayContext, state: str) -> StateOutcome:
    """Nothing the op wants in sight: walk to a seller of it (A71, A131)."""
    m, plan = ctx.memory, ctx.plan
    op = top_op(ctx)
    wants = wanted_codes(w, ctx)
    if op is None or plan is None or not wants:
        return StateOutcome(None, "nothing to buy", state=state)
    code = wants[0]
    items = knowledge_items(ctx.knowledge)
    prices = [p for e in w.entities if (p := _sale_price(e, code, items)) is not None]
    if prices and not any(can_afford(w, p) for p in prices):
        plan.drop_current(f"cannot afford {code}", memory=m)
        return StateOutcome(None, f"buy {code}: cannot afford it", state=state)
    if prices:
        # For sale here, but each one refused or no room for it: its stall clock runs.
        return StateOutcome(None, f"buy {code}: none here to take", state=state)
    while True:
        seller = targets_mod.hold(
            m,
            w,
            SHOP_TARGET,
            lambda: next(iter(_sellers_to_walk(w, ctx, code, op)), None),
            lambda s: s in _sellers_to_walk(w, ctx, code, op),
            op,
        )
        if seller is None:
            break
        out = _to_seller(w, ctx, op, code, seller, state)
        if out is not None:
            return out
        # Searched round it and not found: on to the next seller.
        m.sellers_searched.add((code, seller.map_id, seller.pos))
    known = sellers_of(code, w, ctx.knowledge, op)
    given_up = given_up_travel(m.nav_stuck, w.tick)
    if any((s.map_id, s.pos) in given_up and (code, s.map_id, s.pos) not in m.sellers_searched for s in known):
        # The walk to a seller was given up and is backed off: ``ShopState`` asks and holds (A120).
        return StateOutcome(None, f"buy {code}: the walk to its seller was given up", state=state)
    if known:
        plan.drop_current(f"no {code} for sale at any known seller", memory=m)
        return StateOutcome(None, f"buy {code}: not for sale at any known seller", state=state)
    # No seller known (A110): never a walk to shops that do not sell it.
    plan.drop_current(f"no seller of {code} known", memory=m)
    return StateOutcome(None, f"buy {code}: no seller known", state=state)


def _sale_price(e: Entity, code: str, items: dict) -> int | None:
    """The gem price of ``e`` when it is a priced supply of ``code``, else None."""
    if e.kind != "supply" or not supply_matches(code, e.code):
        return None
    return price_of(e, items)


def _sellers_to_walk(w: WorldModel, ctx: PlayContext, code: str, op: GoalOp) -> list[Seller]:
    """The sellers of ``code`` still worth the walk, cheapest walk first: not
    given up, a shop cell out of sight (one in sight shows what it sells), a
    place to search not searched yet."""
    m = ctx.memory
    given_up = given_up_travel(m.nav_stuck, w.tick)
    here = w.pos
    assert here is not None
    out = []
    for s in sellers_of(code, w, ctx.knowledge, op):
        if (s.map_id, s.pos) in given_up or (code, s.map_id, s.pos) in m.sellers_searched:
            continue
        if s.exact and s.map_id == w.map_id and in_sight(w, s.map_id, here, s.pos):
            continue
        out.append(s)
    return out


def _to_seller(w: WorldModel, ctx: PlayContext, op: GoalOp, code: str, seller: Seller, state: str) -> StateOutcome | None:
    """One step toward ``seller``; at a place to search, one step of the search
    round it. None once the search round it has run out."""
    m = ctx.memory
    assert w.pos is not None
    dest = ResolvedDestination(seller.map_id, seller.pos, "shop" if seller.exact else "seller")
    if seller.exact or seller.map_id != w.map_id or not in_sight(w, seller.map_id, w.pos, seller.pos):
        out = walk_to(w, ctx, op, dest, state)
        out.reason = f"buy {code}: {seller.by} seller: {out.reason}"
        return out
    att = nav_stuck.active(m, w)
    if att is not None and att.level == nav_stuck.BREAK:
        # Stuck step 2: Break, below, opens the way this decision.
        return StateOutcome(None, f"buy {code}: {att.goal} blocked", state=state)
    area: GoalOp = {"op": "explore_area", "x": seller.pos[0], "y": seller.pos[1], "radius": SELLER_SEARCH_RADIUS}
    if not explore_targets(area, w):
        return None
    out = explore_outcome(w, m, ctx.policy, ctx.rng, knowledge=ctx.knowledge, op=area, state=state)
    # No step this decision (a taken cell, a wait) is not the search running out: its stall clock runs.
    out.reason = f"buy {code}: searching round its {seller.by} seller: {out.reason}"
    return out
