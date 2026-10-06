"""Loot: walk to a worthwhile pickup, Take or WithdrawFromChest, Drop junk when full (A20)."""

from __future__ import annotations

from ..knowledge_base import knowledge_items
from ..loot import worthwhile_pickups
from ..memory import Memory
from ..navigation import cost_path
from ..navigation import stuck as nav_stuck
from ..pathing import bounded_step, goto_navigation_pending, grid_params, nav_search
from ..world import Pos, WorldModel, chebyshev
from .base import PlayContext, State, StateOutcome
from .explore import plan_sets, reflex_outcome
from .intents import set_position
from .pickup import pickup_outcome

# Walk targets tried per decision, best first; each is one budgeted search (A13).
MAX_WALK_TARGETS = 3


class LootState(State):
    """Priority 3, after Recover. Claims the round only when it will send an intent."""

    name = "Loot"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        policy = ctx.policy
        if policy.kind != "scripted" or not policy.pickup or not world.alive or world.pos is None:
            return False
        return loot_outcome(world, ctx, self.name) is not None

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not self.guard(world, ctx)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        # Reflexes 3–4 first (fight, pickup in reach); Escape and Flee outrank Loot.
        reflex = reflex_outcome(
            world, ctx.policy, never_attack=ctx.never_attack, state=self.name, knowledge=ctx.knowledge
        )
        if reflex is not None:
            return reflex
        out = loot_outcome(world, ctx, self.name)
        # The guard just found one; None only if the world changed in between.
        return out if out is not None else StateOutcome(None, "nothing to loot", state=self.name)


def loot_outcome(w: WorldModel, ctx: PlayContext, state: str) -> StateOutcome | None:
    """The pickup in reach, else the first step toward the best reachable one; None for neither."""
    here = w.pos
    if here is None:
        return None
    near = pickup_outcome(w, ctx.knowledge, state=state)
    if near is not None:
        nav_stuck.finish_in_reach(ctx.memory, w, "loot")
        return near
    if goto_navigation_pending(w, ctx.memory, ctx.policy):
        return None
    _, plan_avoid, plan_costly = plan_sets(w, ctx.memory, ctx.policy, ctx.knowledge)
    far = [p for p in worthwhile_pickups(w, knowledge_items(ctx.knowledge)) if chebyshev(p.pos, here) > 1]
    for p in far[:MAX_WALK_TARGETS]:
        step = _step_toward(w, ctx.memory, ctx.policy, plan_avoid, plan_costly, p.pos)
        if step is not None:
            return StateOutcome([set_position(step)], f"loot {p.code or p.supply_id} → {p.pos}", state=state)
    return None


def _step_toward(w: WorldModel, m: Memory, policy, plan_avoid: set[Pos], plan_costly: set[Pos], goal: Pos) -> Pos | None:
    """Next step of the kept loot path to ``goal``, replanned when it is stale.

    Bounded like any walk (``bounded_step``, A15): a pickup with no path, or
    no progress in its window, is given up with a backoff."""

    def params():
        return grid_params(policy, plan_avoid, plan_costly)

    def plan() -> list[Pos] | None:
        return cost_path(w, goal, params(), nav=nav_search(m, w, "loot", goal))

    return bounded_step(m, w, "loot", goal, plan_avoid, plan, params=params)
