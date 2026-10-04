"""Downed: dead until the world respawns us (Recover is A11)."""

from __future__ import annotations

from ..world import WorldModel
from .base import State, StateOutcome
from ..brain import PlayContext


class DownedState(State):
    name = "Downed"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not world.alive

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        return StateOutcome(None, "downed", state=self.name)

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return world.alive
