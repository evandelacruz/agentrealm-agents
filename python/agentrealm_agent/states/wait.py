"""Wait: hold the round for the plan's ``wait`` op (A34).

The planner's ``wait`` always says why and is short (``MAX_WAIT_SECONDS``);
``Plan.advance`` pops it when its time is up. Reflexes still outrank it.
"""

from __future__ import annotations

from ..world import WorldModel
from .base import PlayContext, State, StateOutcome, my_op


class WaitState(State):
    """Executor for ``wait``: no intent, the round held."""

    name = "Wait"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        return ctx.policy.kind == "scripted" and world.alive and my_op(ctx, self.name) is not None

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not self.guard(world, ctx)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        op = my_op(ctx, self.name)
        assert op is not None
        return StateOutcome(None, f"plan wait: {op['why']}", state=self.name, wait=True)
