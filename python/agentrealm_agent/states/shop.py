"""Shop: carry out the plan's ``buy`` op on priced supplies in sight (A21)."""

from __future__ import annotations

from ..knowledge_base import knowledge_items
from ..loot import Pickup, loot_score, pickup_room
from ..memory import Memory
from ..navigation import cost_path
from ..pathing import grid_params, nav_search, next_step
from ..shop import (
    GOAL,
    can_afford,
    pick_supply,
    price_of,
    wanted_codes,
)
from ..world import Entity, WorldModel, chebyshev
from .base import PlayContext, State, StateOutcome, my_op
from .explore import plan_sets
from .intents import drop, set_position, take


class ShopState(State):
    """Executor for ``buy``: takes a priced supply in sight that the op wants.
    With none in sight it sends nothing; getting to a shop is a ``travel`` op."""

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


def shop_take_intents(w: WorldModel, items: dict, supply: Entity) -> list[dict] | None:
    """``Take`` the priced supply; with a full pack, ``Drop`` the worst held
    supply first, but only when the purchase outscores it (Loot's rule, A20)."""
    price = price_of(supply, items)
    if price is None or not can_afford(w, price):
        return None
    priced = {**items, supply.code: {**(items.get(supply.code) or {}), "gem_price": price}}
    room = pickup_room(w, Pickup(supply.id, supply.code, supply.pos, None, loot_score(supply.code, priced)), items)
    if room is False:
        return None
    if room is True:
        return [take(supply.id)]
    return [drop(room.id), take(supply.id)]


def shop_outcome(w: WorldModel, ctx: PlayContext, state: str) -> StateOutcome:
    supply = _target(w, ctx)
    if supply is None:
        return StateOutcome(None, "nothing to buy", state=state)
    here = w.pos
    assert here is not None
    label = supply.code or str(supply.id)
    if chebyshev(supply.pos, here) <= 1:
        intents = shop_take_intents(w, knowledge_items(ctx.knowledge), supply)
        if intents is None:
            return StateOutcome(None, f"no room for {label}", state=state)
        # Settled when this Take lands or gems drop (runner, sync_shop).
        ctx.memory.shop_pending = (supply.id, supply.code, w.gems, supply.pos, w.map_id, w.tick)
        return StateOutcome(intents, f"buy {label}", state=state)
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
