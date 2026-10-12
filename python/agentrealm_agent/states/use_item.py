"""UseItem: ``Use`` on yourself a held consumable whose effect applies to the
holder (A108): the planner's ``use_item`` op, and a bigger chest once held.

``use_on_self`` is the one Use-self path. Heal drinks through it, and so does
this state, for any item ``supplies.used_on_self`` names: a potion, a chest
(saims docs Gameplay.md: "A chest upgrade is a consumable too"). Held, such an
item does nothing; only its ``Use`` applies it.
"""

from __future__ import annotations

from ..equip import weapon_to_rearm
from ..executor.supply_refusals import can_try, carried_ids, in_hand, must_arm
from ..healing import health_low, in_combat, walk_seconds
from ..item_table import InventorySupply
from ..memory import Memory
from ..plan import Plan
from ..supplies import BLOCK_FOOD, chest_capacity, chest_upgrade, heal_of, is_potion, used_on_self
from ..survival import hostiles_in_range
from ..world import WorldModel
from .base import PlayContext, State, StateOutcome, my_op
from .food import food_route
from .intents import arm_and_use, use_self


class UseItemState(State):
    """Reflex, below Greet, and the executor for ``use_item``.

    With a ``use_item`` op on top it uses one held item of the op's code on
    the character; the op is done once that item is used up, and dropped,
    with why, when none is held or its ``Use`` was refused or did nothing,
    or when it is a potion the one potion rule refuses (``potion_refused``,
    A135).
    With no op it applies a bigger carried chest as soon as one is held and
    no hostile is in range: more carry slots are always better, so that call
    is not the planner's. The weapon the ``Arm`` swapped out goes back on
    Heal's next decision (``rearm_after_use``), as after a potion."""

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
        return use_on_self(world, ctx.memory, item, self.name, ctx.tick_hz)


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
    if plan.used_id is None and is_potion(code) and (why := potion_refused(world, ctx, plan)) is not None:
        plan.drop_current(why, memory=m)
        return StateOutcome(None, why, state=UseItemState.name)
    out = use_on_self(world, m, item, UseItemState.name, ctx.tick_hz)
    if out.intents:
        plan.used_id = item.id
    return out


# The ops a potion drunk just above them is for (A135): a fight, a boss, a
# level, or gathering on ground the planner means to fight on.
FIGHT_OPS = frozenset({"fight", "fight_boss", "enter_level"})


def fight_ahead(plan: Plan) -> bool:
    """The op under the head is a fight (``FIGHT_OPS``, or a
    ``gather_gems`` with ``fight``): a potion drunk now is for it."""
    ops = plan.ops_left()
    nxt = ops[1] if len(ops) > 1 else None
    return nxt is not None and (nxt["op"] in FIGHT_OPS or (nxt["op"] == "gather_gems" and bool(nxt.get("fight"))))


def potion_refused(w: WorldModel, ctx: PlayContext, plan: Plan) -> str | None:
    """Why the planner's potion is not drunk, or None to drink it (A135).
    A potion is for a fight: in combat or with one next on the plan
    (``healing.in_combat``) it is drunk. Out of combat food heals: while Heal
    has food near to go for (``food.food_route``, the one food test, priced
    by its walk) the drink is refused and the reason names that food; above
    the ``low_health`` line with none it is refused too, the potion kept for
    a fight."""
    if in_combat(w, ctx.memory, ctx.policy, fight_ahead=fight_ahead(plan)):
        return None
    if (route := food_route(w, ctx)) is not None:
        cell = f"{w.map_id}:{route.pos[0]},{route.pos[1]}"
        if route.food is not None:
            code = route.food.code or ""
            what = f"{code} at {cell}"
        else:
            block = w.view.tiles.get(route.pos) or ""
            code = BLOCK_FOOD.get(block, "")
            what = f"a {block} at {cell} to break for {code}"
        return (
            f"a potion is for a fight: out of combat with food near ({what}, heal {heal_of(code)}, "
            f"{walk_seconds(w, route.pos)} s walk; State food_near), Heal eats it"
        )
    if not health_low(w, ctx.params):
        return (
            "a potion is for a fight: out of combat above the low_health line it is kept; put the use_item "
            "directly above the fight, boss or level op it is for"
        )
    return None


def self_use_item(w: WorldModel, m: Memory, code: str) -> InventorySupply | None:
    """A held (or armed) ``code`` a ``Use`` on yourself applies and may be
    sent now (``supply_refusals.can_try``: not refused, or no longer for the reason
    it was), lowest id first."""
    if not used_on_self(code):
        return None
    return next((h for h in sorted(in_hand(w), key=lambda h: h.id) if h.code == code and can_try(m, w, "use", h.id)), None)


def chest_to_apply(w: WorldModel, m: Memory) -> InventorySupply | None:
    """The held chest that gives the pack the most slots, when that is more
    than it has now (``supplies.chest_upgrade``) and its ``Use`` may be sent."""
    chests = [h for h in in_hand(w) if chest_upgrade(h.code, w.carry_capacity) and self_use_item(w, m, h.code or "")]
    return max(chests, key=lambda h: (chest_capacity(h.code) or 0, -h.id), default=None)


def use_on_self(w: WorldModel, m: Memory, item: InventorySupply, state: str, tick_hz: int) -> StateOutcome:
    """``Use`` ``item`` on yourself (API Use), with its ``Arm`` first in one
    paced queue unless it is already in the slot, so both are sent.

    The self-``Use`` is remembered in ``self_use`` so the runner files a
    refusal, or one that applied and did nothing, against this supply
    (A76, A80); and the weapon to put back in ``self_use_rearm``, re-armed on
    Heal's next decision (``rearm_after_use``). It is always a weapon
    (``equip.weapon_to_rearm``): the one armed now, else the last one armed
    this run, never the potion or tool in the slot. The ``Arm`` never goes
    out without its ``Use``: a cooldown that leaves no room for both in one
    queue sends nothing yet."""
    # Another supply of the same code in the slot is not this one: arm it (A76).
    in_slot = w.armed_code == item.code and w.armed_id in (None, item.id)
    if in_slot and not must_arm(m, item.id):
        out = StateOutcome([use_self()], f"use {item.code}", state=state)
    elif queue := arm_and_use(w, m, item.id, use_self(), tick_hz):
        out = StateOutcome(queue, f"arm and use {item.code}", state=state)
        out.paced = True
    else:
        out = StateOutcome(None, f"wait out the cooldown to arm and use {item.code}", state=state)
        out.wait = True
        return out
    m.self_use = item.id
    if m.self_use_rearm is None:
        m.self_use_rearm = weapon_to_rearm(w, m)
    return out


def rearm_after_use(w: WorldModel, m: Memory) -> list[dict] | None:
    """One ``Arm`` for the weapon a ``Use`` on yourself swapped out, sent once (A24).

    Clears ``self_use_rearm`` either way, so a rejected ``Arm`` or a weapon no
    longer in hand cannot keep Heal (or Equip, which waits on it) stuck.
    """
    code, m.self_use_rearm = m.self_use_rearm, None
    if code is None or w.armed_code == code:
        return None
    for h in w.held_supplies:
        if h.code == code:
            return [{"verb": "Arm", "supply_id": h.id}]
    return None
