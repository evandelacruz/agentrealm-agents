"""Sync: hold the round until placement is known (priority 0).

Runs when ``world.pos`` is missing; ``choose_call`` normally fills self and
position first. Sends ``Wait`` to wake from sleep, else ``wait=True``.
"""

from __future__ import annotations

from ..executor import wait
from ..world import WorldModel
from .base import PlayContext, State, StateOutcome


class SyncState(State):
    """Priority 0. Active while the runner has no position; yields once placed."""

    name = "Sync"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        # Self/position reads are choose_call; tick waits only with no tracked pos.
        return world.pos is None

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        if world.asleep:
            # Any intent wakes a sleeping character (GAME_NOTES Sleep). Re-read
            # self next window, so a round trip that does not say it woke can
            # never leave the agent sending Waits forever.
            ctx.memory.need_self = True
            return StateOutcome([wait()], "wake", state=self.name)
        return StateOutcome(None, "sync", state=self.name, wait=True)

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not self.guard(world, ctx)
