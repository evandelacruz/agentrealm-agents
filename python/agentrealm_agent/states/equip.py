"""Equip: carry out the plan's ``equip`` op, arming and wearing better gear (A19, A55, A99)."""

from __future__ import annotations

from ..equip import ARMED, EquipUpgrade, best_equip_upgrade, hunting_ceiling, named_equip
from ..knowledge_base import knowledge_items
from ..navigation import stuck as nav_stuck
from ..world import WorldModel
from .base import PlayContext, State, StateOutcome, my_op
from .intents import arm, remove_slot, wear


class EquipState(State):
    """Executor for ``equip``: one upgrade per decision, best first, by the one
    best-weapon rule the planner is told (``equip.weapon_rank``). An op with a
    ``code`` arms or wears that held item instead, the planner's call. The op
    is finished once nothing is left to do: no upgrade, or the named item on,
    not held or refused."""

    name = "Equip"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        if ctx.policy.kind != "scripted" or not world.alive or world.pos is None:
            return False
        return my_op(ctx, self.name) is not None

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not self.guard(world, ctx)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        op = my_op(ctx, self.name)
        assert op is not None and ctx.plan is not None
        code = op.get("code")
        up = named_equip(world, ctx.memory, code, _armed_owned(world, ctx)) if code else _upgrade(world, ctx)
        if up is None:
            ctx.plan.finish_current("nothing left to equip", memory=ctx.memory)
            return StateOutcome(None, "nothing to equip", state=self.name)
        if up.learn_slot:
            return StateOutcome([wear(up.supply_id)], f"learn wear {up.code}", state=self.name)
        if up.slot == ARMED:
            return StateOutcome([arm(up.supply_id)], f"arm {up.code}", state=self.name)
        intents = [remove_slot(up.slot)] if up.remove_first else []
        intents.append(wear(up.supply_id))
        return StateOutcome(intents, f"wear {up.code} ({up.slot})", state=self.name)


def _upgrade(world: WorldModel, ctx: PlayContext) -> EquipUpgrade | None:
    return best_equip_upgrade(
        world,
        knowledge_items(ctx.knowledge),
        world.threat,
        ctx.memory,
        _armed_owned(world, ctx),
        hunting_ceiling(world, ctx.knowledge),
    )


def _armed_owned(world: WorldModel, ctx: PlayContext) -> bool:
    """Heal, Solve or Break holds the armed slot: it armed a drink or tool, or is about to."""
    m = ctx.memory
    if m.solve_rearm is not None or m.break_rearm is not None or m.heal_rearm is not None:
        return True
    att = nav_stuck.active(m, world)
    return att is not None and att.level == nav_stuck.BREAK
