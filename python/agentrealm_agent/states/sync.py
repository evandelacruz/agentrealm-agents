"""Sync: wait while self or position are unknown (reads are choose_call)."""

from __future__ import annotations

from ..world import WorldModel
from .base import PlayContext, State, StateOutcome


class SyncState(State):
    name = "Sync"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        # Self/position reads are choose_call; tick waits only with no tracked pos.
        return world.pos is None

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        return StateOutcome(None, "sync", state=self.name)

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not self.guard(world, ctx)
