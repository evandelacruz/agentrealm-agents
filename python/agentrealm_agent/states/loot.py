"""Loot: carry out the plan's ``fetch_item`` op (A20).

Walks to a free supply of the op's code in sight and ``Take``s it; with none
in sight and the op naming a cell, walks there to look. A supply underfoot
or adjacent that is merely worthwhile is **Pickup**'s, a reflex.
"""

from __future__ import annotations

from ..healing import supply_matches
from ..navigation import cost_path
from ..pathing import bounded_step, grid_params, nav_search
from ..world import Entity, Pos, WorldModel, chebyshev
from .base import PlayContext, State, StateOutcome, my_op
from .explore import plan_sets
from .intents import set_position, take

GOAL = "loot"


class LootState(State):
    """Executor for ``fetch_item``."""

    name = "Loot"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        if ctx.policy.kind != "scripted" or not world.alive or world.pos is None:
            return False
        return my_op(ctx, self.name) is not None

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not self.guard(world, ctx)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        op = my_op(ctx, self.name)
        assert op is not None and world.pos is not None
        code = op["code"]
        supply = _nearest_supply(world, code)
        if supply is not None and chebyshev(supply.pos, world.pos) <= 1:
            return StateOutcome([take(supply.id)], f"fetch {code}", state=self.name)
        target = supply.pos if supply is not None else (op["x"], op["y"]) if "x" in op else None
        if target is None:
            return StateOutcome(None, f"no {code} in sight", state=self.name)
        step = _step_toward(world, ctx, target)
        if step is None:
            return StateOutcome(None, f"no step toward {code}", state=self.name)
        return StateOutcome([set_position(step)], f"fetch {code} → {target}", state=self.name)


def _nearest_supply(w: WorldModel, code: str) -> Entity | None:
    """The nearest free (unpriced) ground supply of ``code`` in sight."""
    here = w.pos
    found = [
        e for e in w.entities if e.kind == "supply" and e.gem_price is None and supply_matches(code, e.code)
    ]
    return min(found, key=lambda e: (chebyshev(e.pos, here), e.id)) if found and here is not None else None


def _step_toward(w: WorldModel, ctx: PlayContext, goal: Pos) -> Pos | None:
    """Next step of the kept loot path to ``goal``, bounded like any walk (``bounded_step``, A15)."""
    m, policy = ctx.memory, ctx.policy
    _, plan_avoid, plan_costly = plan_sets(w, m, policy, ctx.knowledge)

    def plan() -> list[Pos] | None:
        return cost_path(w, goal, grid_params(policy, plan_avoid, plan_costly), nav=nav_search(m, w, GOAL, goal))

    return bounded_step(m, w, GOAL, goal, plan_avoid, plan)
