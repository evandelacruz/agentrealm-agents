"""Equip: arm and wear better gear from held supplies (A19)."""

from __future__ import annotations

from ..equip import best_equip_upgrade, equip_guard
from ..world import WorldModel
from .base import PlayContext, State, StateOutcome
from .pickup import knowledge_items


class EquipState(State):
    """Priority 3, after Recover and before Loot."""

    name = "Equip"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        if ctx.policy.kind != "scripted" or not world.alive or world.pos is None:
            return False
        items = knowledge_items(ctx.knowledge)
        return equip_guard(world, items, world.threat)

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not self.guard(world, ctx)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        items = knowledge_items(ctx.knowledge)
        upgrade = best_equip_upgrade(world, items, world.threat)
        if upgrade is None:
            return StateOutcome(None, "nothing to equip", state=self.name)
        return StateOutcome(list(upgrade.intents), upgrade.reason, state=self.name)
