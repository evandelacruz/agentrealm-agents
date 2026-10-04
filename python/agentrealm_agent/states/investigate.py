"""Investigate: reads and speech from where the agent stands (A30)."""

from __future__ import annotations

from ..interest_list import pick_interest_tick
from ..world import WorldModel
from .base import PlayContext, State, StateOutcome
from .intents import read_block, say_to


class InvestigateState(State):
    name = "Investigate"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        if ctx.policy.kind != "scripted" or not world.alive or world.pos is None:
            return False
        return pick_interest_tick(world, ctx.knowledge, ctx.policy, ctx.memory, ctx.directives) is not None

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not self.guard(world, ctx)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        item = pick_interest_tick(world, ctx.knowledge, ctx.policy, ctx.memory, ctx.directives)
        if item is None:
            return StateOutcome(None, "nothing to investigate", state=self.name)
        if item.kind == "read_block" and item.map_id is not None and item.pos is not None:
            return StateOutcome([read_block(item.map_id, item.pos)], item.reason, state=self.name)
        if item.kind == "say" and item.npc is not None:
            return StateOutcome([say_to(item.npc)], item.reason, state=self.name)
        return StateOutcome(None, item.reason, state=self.name)
