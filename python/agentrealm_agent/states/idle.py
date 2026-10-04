"""Idle and wander: send nothing or a random step."""

from __future__ import annotations

from ..world import WorldModel
from .base import PlayContext, State, StateOutcome
from .intents import set_position


class IdleState(State):
    name = "Idle"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        return ctx.policy.kind in ("idle", "wander")

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not self.guard(world, ctx)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        # Sync and Downed rank above Idle, so here we are placed and alive.
        if ctx.policy.kind == "idle":
            return StateOutcome(None, "idle", state=self.name)
        options = world.open_neighbours(world.pos, set(ctx.memory.blocked))
        if not options:
            return StateOutcome(None, "wander: boxed in", state=self.name)
        p = ctx.rng.choice(sorted(options))
        return StateOutcome([set_position(p)], "wander", state=self.name)
