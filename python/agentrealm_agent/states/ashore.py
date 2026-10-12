"""Ashore: under attack on water with no weapon to swing, make for land (A133).

A fording supply armed in the hand leaves no weapon: on water no other can be
armed (``would_strand``), so no fight there is worth having. While a hostile
is on us or in range, the character walks to the nearest reachable land,
priced by the cost grid's hostile danger, and fights or flees by the normal
rules once ashore. A fording supply worn leaves the hand free, so this does
not run.
"""

from __future__ import annotations

from ..fording import on_water, stranded_unarmed
from ..navigation import nearest_target
from ..pathing import grid_params, plan_sets
from ..survival import flee_from, recently_attacked
from ..world import WATER, WorldModel, chebyshev
from .base import PlayContext, State, StateOutcome
from .intents import set_position

# How far round the character land is looked for.
LAND_RADIUS = 30


class AshoreState(State):
    """Reflex, above Retreat, Heal, Fight and Flee: active while on water with
    only a fording supply in hand and a hostile on us or in range."""

    name = "Ashore"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        w, m = world, ctx.memory
        if ctx.policy.kind != "scripted" or not w.alive or w.pos is None or w.map_id is None:
            return False
        if not on_water(w, m, w.pos) or not stranded_unarmed(w):
            return False
        return recently_attacked(w) or bool(flee_from(w, ctx.policy))

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not self.guard(world, ctx)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        w, m = world, ctx.memory
        here = w.pos
        assert here is not None
        blocked, plan_avoid, plan_costly = plan_sets(w, m, ctx.policy, ctx.knowledge)
        land = {
            p
            for p, block in w.view.tiles.items()
            if block != WATER and w.walkable(p) and p not in blocked and chebyshev(p, here) <= LAND_RADIUS
        } - w.occupied()
        params = grid_params(ctx.policy, plan_avoid, plan_costly, m=m, w=w, knowledge=ctx.knowledge)
        found = nearest_target(w, land, params)
        if found is None or not found[1]:
            return StateOutcome(None, "no land in reach", state=self.name)
        target, path = found
        m.path, m.goal = [], ""
        return StateOutcome([set_position(path[0])], f"under attack on water: for land at {target[0]},{target[1]}", reflex=True, state=self.name)
