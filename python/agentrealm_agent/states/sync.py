"""Sync: wait while self or position are unknown (reads are choose_call)."""

from __future__ import annotations

from ..executor import wait
from ..world import WorldModel
from .base import PlayContext, State, StateOutcome


class SyncState(State):
    name = "Sync"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        # Self/position reads are choose_call; tick waits only with no tracked pos.
        return world.pos is None

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        if world.placed is False or world.asleep:
            return StateOutcome(wait(), "wake", state=self.name)
        return StateOutcome(None, "sync", state=self.name, wait=True)

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not self.guard(world, ctx)
