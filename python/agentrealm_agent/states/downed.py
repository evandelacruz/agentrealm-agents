"""Downed: wait while dead (priority 0).

Runs when ``world.alive`` is false. **Recover** handles the death chest after
respawn (A11).
"""

from __future__ import annotations

from ..world import WorldModel
from .base import PlayContext, State, StateOutcome


class DownedState(State):
    """Priority 0. Holds the round with ``wait=True`` until the world respawns us."""

    name = "Downed"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not world.alive

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        return StateOutcome(None, "downed", state=self.name, wait=True)

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return world.alive
