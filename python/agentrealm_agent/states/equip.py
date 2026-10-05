"""Equip: arm and wear better gear from held supplies (A19)."""

from __future__ import annotations

from ..equip import ARMED, EquipUpgrade, best_equip_upgrade
from ..knowledge_base import knowledge_items
from ..navigation import stuck as nav_stuck
from ..world import WorldModel
from .base import PlayContext, State, StateOutcome
from .intents import arm, remove_slot, wear
from .break_state import break_op
from .solve import solve_op


class EquipState(State):
    """Priority 3, after Recover and before Loot."""

    name = "Equip"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        if ctx.policy.kind != "scripted" or not world.alive or world.pos is None:
            return False
        return _upgrade(world, ctx) is not None

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not self.guard(world, ctx)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        up = _upgrade(world, ctx)
        if up is None:
            return StateOutcome(None, "nothing to equip", state=self.name)
        if up.slot == ARMED:
            return StateOutcome([arm(up.supply_id)], f"arm {up.code}", state=self.name)
        intents = [remove_slot(up.slot)] if up.remove_first else []
        intents.append(wear(up.supply_id))
        return StateOutcome(intents, f"wear {up.code} ({up.slot})", state=self.name)


def _upgrade(world: WorldModel, ctx: PlayContext) -> EquipUpgrade | None:
    return best_equip_upgrade(world, knowledge_items(ctx.knowledge), world.threat, ctx.memory, _armed_owned(world, ctx))


def _armed_owned(world: WorldModel, ctx: PlayContext) -> bool:
    """Solve or Break holds the armed slot: it armed a tool, or is about to."""
    m = ctx.memory
    if m.solve_rearm is not None or m.break_rearm is not None:
        return True
    if solve_op(ctx.plan) is not None or break_op(ctx.plan) is not None:
        return True
    att = nav_stuck.active(m, world)
    return att is not None and att.level == nav_stuck.BREAK
