"""Shop: buy in-sight priced supplies the plan wants; restock potion_reserve (A21)."""

from __future__ import annotations

from ..memory import Memory
from ..navigation import cost_path
from ..pathing import grid_params, nav_search, next_step
from ..shop import (
    GOAL,
    knowledge_items,
    note_shop_purchase,
    pick_supply,
    shop_take_intents,
    wanted_codes,
)
from ..world import WorldModel, chebyshev
from .base import PlayContext, State, StateOutcome
from .explore import plan_sets, reflex_outcome
from .intents import set_position


class ShopState(State):
    """Priority 3, after Loot. Takes priced supplies in sight for plan ``buy``
    ops, Heal ``buy_signals``, and ``potion_reserve`` restock."""

    name = "Shop"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        if ctx.policy.kind != "scripted" or not world.alive or world.pos is None:
            return False
        return shop_outcome(world, ctx, self.name) is not None

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not self.guard(world, ctx)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        reflex = reflex_outcome(
            world, ctx.policy, never_attack=ctx.never_attack, state=self.name, knowledge=ctx.knowledge
        )
        if reflex is not None:
            return reflex
        out = shop_outcome(world, ctx, self.name)
        return out if out is not None else StateOutcome(None, "nothing to buy", state=self.name)


def shop_outcome(w: WorldModel, ctx: PlayContext, state: str) -> StateOutcome | None:
    wants = wanted_codes(w, ctx)
    if not wants:
        return None
    items = knowledge_items(ctx.knowledge)
    supply = pick_supply(w, wants, items)
    if supply is None:
        return None
    here = w.pos
    assert here is not None
    label = supply.code or str(supply.id)
    if chebyshev(supply.pos, here) <= 1:
        intents = shop_take_intents(w, items, supply)
        if intents is None:
            return None
        note_shop_purchase(ctx.memory, supply.code)
        return StateOutcome(intents, f"buy {label}", state=state)
    step = _step_toward(w, ctx.memory, ctx, supply.pos)
    if step is None:
        return None
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
