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
from ..pathing import grid_params, nav_search, next_step
from ..healing import supply_matches
from ..plan import GoalOp, explore_targets
from ..shop import (
    GOAL,
    can_afford,
    pick_supply,
    price_of,
    wanted_codes,
)
from ..travel.knowledge import iter_shop_cells
from ..travel.ops import TravelOp
from ..travel.resolve import ResolvedDestination, resolve_travel
from ..world import Entity, WorldModel, chebyshev
from .base import PlayContext, State, StateOutcome, my_op, top_op
from .explore import explore_outcome, plan_sets
from .intents import set_position
from .pickup import planned_take
from .travel import walk_to

# The commitment of the shop cell a ``buy`` walks to while its item is out of sight (A71).
SHOP_TARGET = "shop"
# With no shop known yet, how far around the town cell Shop explores for one.
SHOP_SEARCH_RADIUS = 20


class ShopState(State):
    """Executor for ``buy``: takes a priced supply in sight that the op wants,
    walking up beside it first.

    With none in sight, the shop is a prerequisite (A71): Shop walks to the
    nearest known shop cell out of sight, committed until it comes into sight;
    with no shop known yet it walks to town and explores around it. It never
    leaves the buy to the safe default, which wandered off into a pack
    (free-play run 4). The op is dropped when every known shop is in sight
    without the item, or the item is in sight and costs more gems than we have.
    With a full pack, room is the pack rule's (``pack.make_room``, A102): the
    op's ``drop`` names what to give up when nothing held is plain junk.
    """

    name = "Shop"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        if ctx.policy.kind != "scripted" or not world.alive or world.pos is None:
            return False
        return my_op(ctx, self.name) is not None

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not self.guard(world, ctx)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        return shop_outcome(world, ctx, self.name)


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
            out.reason = f"buy {label}" if len(out.intents) == 1 else f"buy {label}: {out.reason}"
        return out
    step = _step_toward(w, ctx.memory, ctx, supply.pos)
    if step is None:
        return StateOutcome(None, f"no step toward {label}", state=state)
    return StateOutcome([set_position(step)], f"shop {label} → {supply.pos}", state=state)


def _step_toward(w: WorldModel, m: Memory, ctx: PlayContext, goal) -> object | None:
    _, plan_avoid, plan_costly = plan_sets(w, m, ctx.policy, ctx.knowledge)
    if m.goal == GOAL and m.path and m.path[-1] == goal and next_step(w, plan_avoid, m.path):
        return next_step(w, plan_avoid, m.path)
    found = cost_path(w, goal, grid_params(ctx.policy, plan_avoid, plan_costly), nav=nav_search(m, w, GOAL, goal))
    step = next_step(w, plan_avoid, found)
    if step is not None:
        m.path, m.goal = found, GOAL
    return step


def _toward_shop(w: WorldModel, ctx: PlayContext, state: str) -> StateOutcome:
    """Nothing the op wants in sight: walk to a shop, else find one (A71)."""
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
    given_up = m.nav_stuck.given_up_travel
    dest = targets_mod.hold(
        m,
        w,
        SHOP_TARGET,
        lambda: _shop_out_of_sight(w, ctx),
        lambda d: d in _shop_cells_out_of_sight(w, ctx),
        op,
    )
    if dest is not None:
        out = walk_to(w, ctx, op, dest, state)
        out.reason = f"buy {code}: {out.reason}"
        return out
    if any(s not in given_up for s in iter_shop_cells(ctx.knowledge)):
        # Every shop we know of is in sight, and none sells it.
        plan.drop_current(f"no {code} for sale at a known shop", memory=m)
        return StateOutcome(None, f"buy {code}: not for sale at a known shop", state=state)
    return _find_shop(w, ctx, op, code, state)


def _sale_price(e: Entity, code: str, items: dict) -> int | None:
    """The gem price of ``e`` when it is a priced supply of ``code``, else None."""
    if e.kind != "supply" or not supply_matches(code, e.code):
        return None
    return price_of(e, items)


def _shop_cells_out_of_sight(w: WorldModel, ctx: PlayContext) -> list[ResolvedDestination]:
    """Known shop cells not given up and out of sight: one in sight shows what it sells."""
    given_up = ctx.memory.nav_stuck.given_up_travel
    here = w.pos
    assert here is not None
    out = []
    for mid, pos in iter_shop_cells(ctx.knowledge):
        if (mid, pos) in given_up or (mid == w.map_id and in_sight(w, mid, here, pos)):
            continue
        out.append(ResolvedDestination(mid, pos, "shop"))
    return out


def _shop_out_of_sight(w: WorldModel, ctx: PlayContext) -> ResolvedDestination | None:
    """The nearest known shop cell out of sight, on this map first."""
    here = w.pos
    assert here is not None
    cells = _shop_cells_out_of_sight(w, ctx)
    if not cells:
        return None
    return min(cells, key=lambda d: (d.map_id != w.map_id, chebyshev(here, d.pos), d.map_id, d.pos))


def _find_shop(w: WorldModel, ctx: PlayContext, op: GoalOp, code: str, state: str) -> StateOutcome:
    """No shop known: walk to town, then explore around it for one. With no
    town known either Shop sends nothing: the safe default explores, and the
    buy's stall clock runs (A34)."""
    m, plan = ctx.memory, ctx.plan
    assert plan is not None and w.pos is not None
    town = resolve_travel(TravelOp(to="town"), w, ctx.knowledge, m.strength)
    if town is None or (town.map_id, town.pos) in m.nav_stuck.given_up_travel:
        return StateOutcome(None, f"buy {code}: no shop or town known", state=state)
    if town.map_id != w.map_id or not in_sight(w, town.map_id, w.pos, town.pos):
        out = walk_to(w, ctx, op, town, state)
        out.reason = f"buy {code}: no shop known, to town: {out.reason}"
        return out
    att = nav_stuck.active(m, w)
    if att is not None and att.level == nav_stuck.BREAK:
        # Stuck step 2: Break, below, opens the way this decision.
        return StateOutcome(None, f"buy {code}: {att.goal} blocked", state=state)
    area: GoalOp = {"op": "explore_area", "x": town.pos[0], "y": town.pos[1], "radius": SHOP_SEARCH_RADIUS}
    if explore_targets(area, w):
        out = explore_outcome(w, m, ctx.policy, ctx.rng, knowledge=ctx.knowledge, op=area, state=state)
        # No step this decision (a taken cell, a wait) is not the search running out: its stall clock runs.
        out.reason = f"buy {code}: no shop known, exploring town: {out.reason}"
        return out
    plan.drop_current(f"no shop found within {SHOP_SEARCH_RADIUS} of town", memory=m)
    return StateOutcome(None, f"buy {code}: no shop found around town", state=state)
