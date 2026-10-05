"""Idle and wander: non-scripted policies (lowest priority).

Runs when ``policy.kind`` is ``idle`` (wait forever) or ``wander`` (random step).
Scripted characters reach here only if every other state fell through.
"""

from __future__ import annotations

from ..navigation.rejection import navigation_avoid_costly
from ..world import WorldModel
from .base import PlayContext, State, StateOutcome
from .intents import set_position


class IdleState(State):
    """Last in ``STATES``. ``idle`` waits; ``wander`` picks a random open neighbour."""

    name = "Idle"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        return ctx.policy.kind in ("idle", "wander")

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not self.guard(world, ctx)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        # Sync and Downed rank above Idle, so here we are placed and alive.
        if ctx.policy.kind == "idle":
            return StateOutcome(None, "idle", state=self.name, wait=True)
        avoid, _ = navigation_avoid_costly(ctx.memory.nav, ctx.knowledge, world.map_id, world.tick)
        options = world.open_neighbours(world.pos, avoid)
        if not options:
            return StateOutcome(None, "wander: boxed in", state=self.name, wait=True)
        p = ctx.rng.choice(sorted(options))
        return StateOutcome([set_position(p)], "wander", state=self.name)
