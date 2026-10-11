"""UseItem: ``Use`` on yourself a held consumable whose effect applies to the
holder (A108): the planner's ``use_item`` op, and a bigger chest once held.

``use_on_self`` is the one Use-self path. Heal drinks through it, and so does
this state, for any item ``supplies.used_on_self`` names: a potion, a chest
(saims docs Gameplay.md: "A chest upgrade is a consumable too"). Held, such an
item does nothing; only its ``Use`` applies it.
"""

from __future__ import annotations

from ..equip import weapon_to_rearm
from ..healing import can_try, carried_ids, in_hand, must_arm
from ..item_table import InventorySupply
from ..memory import Memory
from ..supplies import chest_capacity, chest_upgrade, used_on_self
from ..survival import hostiles_in_range
from ..world import WorldModel
from .base import PlayContext, State, StateOutcome, my_op
from .intents import arm_and_use, use_self


class UseItemState(State):
    """Reflex, below Greet, and the executor for ``use_item``.

    With a ``use_item`` op on top it uses one held item of the op's code on
    the character; the op is done once that item is used up, and dropped,
    with why, when none is held or its ``Use`` was refused or did nothing.
    With no op it applies a bigger carried chest as soon as one is held and
    no hostile is in range: more carry slots are always better, so that call
    is not the planner's. The weapon the ``Arm`` swapped out goes back on
    Heal's next decision (``heal_rearm``), as after a drink."""

    name = "UseItem"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        if ctx.policy.kind != "scripted" or not world.alive or world.pos is None:
            return False
        if my_op(ctx, self.name) is not None:
            return True
        return not hostiles_in_range(world, ctx.policy) and chest_to_apply(world, ctx.memory) is not None

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not self.guard(world, ctx)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        if my_op(ctx, self.name) is not None:
            return _use_op(world, ctx)
        item = chest_to_apply(world, ctx.memory)
        if item is None:
            return StateOutcome(None, "no bigger chest to apply", state=self.name)
        return use_on_self(world, ctx.memory, item, self.name)


def _use_op(world: WorldModel, ctx: PlayContext) -> StateOutcome:
    plan, m = ctx.plan, ctx.memory
    op = my_op(ctx, UseItemState.name)
    assert op is not None and plan is not None
    code = op["code"]
    if plan.used_id is not None and plan.used_id not in carried_ids(world):
        plan.finish_current(f"used {code}", memory=m)
        return StateOutcome(None, f"used {code}", state=UseItemState.name)
    item = self_use_item(world, m, code)
    if item is None:
        held = any(h.code == code for h in in_hand(world))
        why = f"its Use on yourself was refused or did nothing: {code}" if held else f"{code} not held"
        plan.drop_current(why, memory=m)
        return StateOutcome(None, why, state=UseItemState.name)
    out = use_on_self(world, m, item, UseItemState.name)
    if out.intents:
        plan.used_id = item.id
    return out


def self_use_item(w: WorldModel, m: Memory, code: str) -> InventorySupply | None:
    """A held (or armed) ``code`` a ``Use`` on yourself applies and may be
    sent now (``healing.can_try``: not refused, or no longer for the reason
    it was), lowest id first."""
    if not used_on_self(code):
        return None
    return next((h for h in sorted(in_hand(w), key=lambda h: h.id) if h.code == code and can_try(m, w, "use", h.id)), None)


def chest_to_apply(w: WorldModel, m: Memory) -> InventorySupply | None:
    """The held chest that gives the pack the most slots, when that is more
    than it has now (``supplies.chest_upgrade``) and its ``Use`` may be sent."""
    chests = [h for h in in_hand(w) if chest_upgrade(h.code, w.carry_capacity) and self_use_item(w, m, h.code or "")]
    return max(chests, key=lambda h: (chest_capacity(h.code) or 0, -h.id), default=None)


def use_on_self(w: WorldModel, m: Memory, item: InventorySupply, state: str) -> StateOutcome:
    """``Use`` ``item`` on yourself (API Use), with its ``Arm`` first in one
    paced queue unless it is already in the slot, so both are sent.

    The self-``Use`` is remembered in ``heal_drink`` so the runner files a
    refusal, or one that applied and did nothing, against this supply
    (A76, A80); and the weapon to put back in ``heal_rearm``, re-armed on
    Heal's next decision. The ``Arm`` never goes out without its ``Use``: a
    cooldown that leaves no room for both in one queue sends nothing yet."""
    # Another supply of the same code in the slot is not this one: arm it (A76).
    in_slot = w.armed_code == item.code and w.armed_id in (None, item.id)
    if in_slot and not must_arm(m, item.id):
        out = StateOutcome([use_self()], f"use {item.code}", state=state)
    elif queue := arm_and_use(w, m, item.id, use_self()):
        out = StateOutcome(queue, f"arm and use {item.code}", state=state)
        out.paced = True
    else:
        out = StateOutcome(None, f"wait out the cooldown to arm and use {item.code}", state=state)
        out.wait = True
        return out
    m.heal_drink = item.id
    if m.heal_rearm is None:
        m.heal_rearm = weapon_to_rearm(w, m)
    return out
