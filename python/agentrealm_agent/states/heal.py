"""Heal: how a hurt character heals (A10), and the weapon put back after a
``Use`` on yourself (A24).

Heal decides only how to heal, and health rises only from a heal: a potion,
or food, which is eaten on pickup and never carried (A130, A155). There is
no natural regeneration, so Heal never walks to safe ground to wait for
health; safe ground is a refuge from attack (Retreat, Flee), not a healing
spot. When a carried potion is
spent is the planner's ``low_health`` param (``healing.health_low``,
``healing.spend_potion``). What is sent after a refusal is the executor's
(``executor.supply_refusals``); Heal asks only whether a supply may be tried
now (``can_try``).
"""

from __future__ import annotations

from agentrealm_adk.break_memory import choice_at, given_up_blocks
from agentrealm_adk.healing import carried_heal, drink_first, food_on_hold, hurt, in_combat, potion_count
from agentrealm_adk.memory import Memory, queue_signal
from agentrealm_adk.navigation import cost_path
from agentrealm_adk.navigation import stuck as nav_stuck
from agentrealm_adk import targets as targets_mod
from agentrealm_adk.pathing import bounded_step, nav_search
from agentrealm_adk.survival import hostiles_in_range, hostiles_reaching
from agentrealm_adk.world import Pos, WorldModel, chebyshev

from .base import PlayContext, State, StateOutcome
from .break_state import break_toward, in_break_reach
from .food import block_target, food_target, take_reach, walk_grids
from .intents import set_position, take
from .use_item import rearm_after_use, use_on_self


class HealState(State):
    """Reflex, above Retreat. Under attack at or below the low line with a
    usable potion held (``healing.drink_first``, A165), it drinks before any
    other state acts, Retreat and Flee included, with a hostile in range or
    not, and the drink is a reflex, so it goes out over a walk queue Retreat
    left running. Else, hurt and no hostile in range, it heals by the first
    of these that can be had:

    1. In combat or about to be (``healing.in_combat``), a carried potion
       that is due goes first (A135).
    2. Not under attack (``healing.food_on_hold``, A165), food lying within ``healing.FOOD_REACH`` that it is hurt by at least
       the heal of (``healing.worth_eating``, A155), the cheapest by its
       walk on Heal's ground (``food.food_target``). Food is eaten on
       pickup, so the ``Take`` is the meal.
    3. Out of combat (so not under attack either), a block within that reach that drops such food (a
       bush, for berries) and a held blade opens, the cheapest the same way
       (``food.block_target``, A135).
    4. A carried potion when ``healing.spend_potion`` says it is worth
       drinking now (A96): out of combat only at the low line, and only
       once no food near could be had this decision (A135); the reserve is
       kept otherwise.

    With neither, Heal asks the planner once for food and potions (a
    ``heal_supplies`` trigger, counting the potions held back) and sends
    nothing: the op on top goes on. It never walks to safe ground to wait.

    The walk to food is priced the one way (``_walk_toward``) and bounded by
    stuck detection (``bounded_step``): one that goes nowhere gives its
    target up in the one give-up memory (A90).

    A ``Use`` on yourself is ``Arm`` and ``Use`` in one paced queue. Heal
    runs on the next decision after it, even at full health or with a
    hostile in range, to re-arm the weapon it swapped out (A24): one ``Arm``,
    sent once, before anything else, so ``self_use_rearm`` never outlives
    the ``Use``."""

    name = "Heal"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        if ctx.policy.kind != "scripted" or not world.alive or world.pos is None:
            return False
        return _wants_heal(world, ctx) or _drink_due(world, ctx) or ctx.memory.self_use_rearm is not None

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not self.guard(world, ctx)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        # Under attack at the low line, the potion goes before anything else (A165).
        if drink_first(world, ctx.policy, ctx.params) and (out := use_carried_heal(world, ctx)):
            out.reflex = True
            return out
        # A Use on yourself went out: the weapon goes back before anything else.
        if ctx.memory.self_use_rearm is not None and (out := _rearm_weapon(world, ctx.memory)).intents:
            return out
        if not _wants_heal(world, ctx):
            return _out(None, "not hurt, or a hostile in range")
        return _choose(world, ctx)


def _wants_heal(w: WorldModel, ctx: PlayContext) -> bool:
    """Hurt and out of combat."""
    return hurt(w) and not hostiles_in_range(w, ctx.policy)


def _drink_due(w: WorldModel, ctx: PlayContext) -> bool:
    """Under attack at the low line with a potion ``spend_potion`` lets go (A165)."""
    return drink_first(w, ctx.policy, ctx.params) and carried_heal(w, ctx.memory, ctx.policy, ctx.params) is not None


def _choose(w: WorldModel, ctx: PlayContext) -> StateOutcome:
    # A potion is for a fight (A135): in one, a heal that is due goes before any walk for food.
    fighting = in_combat(w, ctx.memory, ctx.policy)
    if fighting and (out := use_carried_heal(w, ctx)):
        return out
    # Under attack, no walk goes for food (A165): only the potion below heals.
    if not food_on_hold(w, ctx.policy) and (out := _act_food(w, ctx)):
        return out
    if not fighting and (out := _act_block(w, ctx)):
        return out
    # No food near could be had this decision: the potion rule weighs no food.
    if out := use_carried_heal(w, ctx):
        return out
    _ask_for_supplies(w, ctx.memory)
    return _out(None, "nothing to heal with")


def _ask_for_supplies(w: WorldModel, m: Memory) -> None:
    """Hurt, with nothing to eat and no potion due (``healing.spend_potion``):
    ask the planner for food and potions (a ``fetch_item`` or ``buy``), once
    until health is full again (``note_heal_window`` re-arms it). The ask carries the potions
    held: a reserve held back above the low line, which the planner weighs
    before buying more (A96)."""
    if m.heal_supplies_asked:
        return
    m.heal_supplies_asked = True
    queue_signal(
        m,
        {
            "trigger": "heal_supplies",
            "health": w.health,
            "max_health": w.max_health,
            "potions": potion_count(w),
            "tick": w.tick,
        },
    )


def _out(intents: list[dict] | None, reason: str) -> StateOutcome:
    return StateOutcome(intents, reason, state=HealState.name)


def _walk_toward(w: WorldModel, ctx: PlayContext, at: Pos, *, goal: str) -> StateOutcome | None:
    """One move toward ``at``, or None when there is none now.

    One price for every Heal walk: its walk on Heal's ground (``food.ground``),
    a block on the straight way a held item opens costed at its break. When
    the cheapest way crosses such a block, Heal walks that way on its own
    ground and opens the block once it is in the opener's reach
    (``break_toward``); else it steps on the plain grid. Either walk is
    bounded like any other (``bounded_step``, A15): no path, or no progress
    in its window, gives ``at`` up with a backoff, so Heal tries something
    else instead of pacing. A kept path that now crosses the reach of a known
    hostile it did not cross when planned is planned again on the grid that
    prices it.
    """
    m, policy = ctx.memory, ctx.policy
    plain, opening = walk_grids(w, ctx, at)
    way = cost_path(w, at, opening) if opening.break_costs else None
    grid = plain
    if (cut := next((p for p in way or [] if p in opening.break_costs), None)) is not None:
        # The first block the priced way crosses is the one opened, so the
        # walk and its price never part.
        choice = choice_at(w, ctx.knowledge, cut, given_up_blocks(m.nav_stuck, w.map_id, w.tick))
        if choice is not None:
            if in_break_reach(w, ctx, choice):
                out = break_toward(w, ctx, choice, HealState.name)
                out.reason = f"{goal}: {out.reason}"
                return out
            grid = opening  # out of reach: walk the priced way on Heal's ground
    if m.goal == goal and hostiles_reaching(w, policy, m.path) - m.planned_threats:
        m.path = []

    def plan() -> list[Pos] | None:
        return cost_path(w, at, grid, nav=nav_search(m, w, goal, at))

    step = bounded_step(m, w, goal, at, grid.avoid, plan, params=lambda: grid)
    # What the path crosses now it could not go round: only a hostile beyond it replans.
    m.planned_threats = hostiles_reaching(w, policy, m.path)
    return _out([set_position(step)], f"{goal} → {at}") if step is not None else None


def _act_food(w: WorldModel, ctx: PlayContext) -> StateOutcome | None:
    m = ctx.memory
    here = w.pos
    assert here is not None
    food = food_target(w, ctx)
    if food is None:
        return None
    if chebyshev(food.pos, here) <= take_reach(m, food):
        nav_stuck.finish_in_reach(m, w, "heal_food")
        return _out([take(food.id)], f"take food {food.code}")
    # Walking onto it also picks up food eaten on pickup.
    if out := _walk_toward(w, ctx, food.pos, goal="heal_food"):
        targets_mod.commit(m, w, "heal_food", food.id)
        return out
    return None


def _act_block(w: WorldModel, ctx: PlayContext) -> StateOutcome | None:
    """Break the block Heal goes for food (``food.block_target``): the food
    it drops (berries, from a bush) is taken on the next decision like any
    food lying near (A135)."""
    m = ctx.memory
    block = block_target(w, ctx)
    if block is None:
        return None
    choice = choice_at(w, ctx.knowledge, block, given_up_blocks(m.nav_stuck, w.map_id, w.tick))
    if choice is None:
        return None
    out = break_toward(w, ctx, choice, HealState.name)
    if not out.intents:
        return None
    targets_mod.commit(m, w, "heal_block", block)
    out.reason = f"break for food: {out.reason}"
    return out


def use_carried_heal(w: WorldModel, ctx: PlayContext) -> StateOutcome | None:
    """``Use`` on yourself a carried potion ``healing.spend_potion`` says is
    worth drinking now (health low, or the drink turns the fight,
    A96; out of combat only at the low line, A135: Heal calls it there only
    once no food near could be had), by the one Use-self path (``use_item.use_on_self``, A108). Retreat
    runs it too, when it is losing ground (A9).

    A supply the server refused, or whose ``Use`` did nothing, is skipped
    until what it was refused for no longer holds
    (``executor.supply_refusals``, A80, A76). The weapon to put back is
    re-armed on Heal's next decision (``_rearm_weapon``)."""
    item = carried_heal(w, ctx.memory, ctx.policy, ctx.params)
    if item is None:
        return None
    return use_on_self(w, ctx.memory, item, HealState.name, ctx.tick_hz)


def _rearm_weapon(w: WorldModel, m: Memory) -> StateOutcome:
    """Re-``Arm`` the weapon a ``Use`` on yourself swapped out (A24). Sends
    nothing when it is already armed or no longer in hand."""
    code = m.self_use_rearm
    if intents := rearm_after_use(w, m):
        return _out(intents, f"re-arm {code}")
    return _out(None, f"no re-arm needed for {code}")
