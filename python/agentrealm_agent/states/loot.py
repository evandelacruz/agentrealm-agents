"""Loot: walk to a worthwhile pickup, Take or WithdrawFromChest, Drop junk when full (A20)."""

from __future__ import annotations

from ..knowledge_base import knowledge_items
from ..loot import worthwhile_pickups
from ..memory import Memory
from ..navigation import cost_path
from ..navigation import stuck as nav_stuck
from ..pathing import grid_params, nav_search, next_step
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


def _defer_far_loot_for_goto(w: WorldModel, m: Memory, policy) -> bool:
    """M7 navigation: do not walk off toward gems while a ``goto`` target is still owed."""
    if policy.goto is None or "goto" not in policy.goals:
        return False
    dest_map = policy.goto_map if policy.goto_map is not None else w.map_id
    if w.map_id != dest_map or w.pos is None:
        return False
    target = tuple(policy.goto)
    if w.pos == target:
        return False
    if nav_stuck.backed_off(m, "goto", dest_map, target, w.tick):
        return False
    return True


def loot_outcome(w: WorldModel, ctx: PlayContext, state: str) -> StateOutcome | None:
    """The pickup in reach, else the first step toward the best reachable one; None for neither."""
    here = w.pos
    if here is None:
        return None
    near = pickup_outcome(w, ctx.knowledge, state=state)
    if near is not None:
        return near
    if _defer_far_loot_for_goto(w, ctx.memory, ctx.policy):
        return None
    _, plan_avoid, plan_costly = plan_sets(w, ctx.memory, ctx.policy, ctx.knowledge)
    far = [p for p in worthwhile_pickups(w, knowledge_items(ctx.knowledge)) if chebyshev(p.pos, here) > 1]
    for p in far[:MAX_WALK_TARGETS]:
        step = _step_toward(w, ctx.memory, ctx.policy, plan_avoid, plan_costly, p.pos)
        if step is not None:
            return StateOutcome([set_position(step)], f"loot {p.code or p.supply_id} → {p.pos}", state=state)
    return None


def _step_toward(w: WorldModel, m: Memory, policy, plan_avoid: set[Pos], plan_costly: set[Pos], goal: Pos) -> Pos | None:
    """Next step of the kept loot path to ``goal``, replanned when it is stale."""
    if m.goal == "loot" and m.path and m.path[-1] == goal:
        step = next_step(w, plan_avoid, m.path)
        if step is not None:
            return step
    found = cost_path(w, goal, grid_params(policy, plan_avoid, plan_costly), nav=nav_search(m, w, "loot", goal))
    step = next_step(w, plan_avoid, found)
    if step is not None:
        m.path, m.goal = found, "loot"
    return step
