"""Heal: food, carried food or potion, then safe ground (A10); re-arm the
weapon a drink swapped out (A24)."""

from __future__ import annotations

from ..config import Policy
from ..healing import (
    carried_heal,
    food_in_sight,
    hurt,
    nearest_known_safe,
    note_regen_sample,
    note_try,
    rearm_after_drink,
    regen_known,
    save_regen_yes,
    standing_in_safe_zone,
)
from ..memory import Memory, queue_signal
from ..navigation import cost_path
from ..navigation import stuck as nav_stuck
from ..navigation.rejection import navigation_avoid_costly
from ..pathing import bounded_step, grid_params, hostiles_in_range, nav_search
from ..world import Pos, WorldModel, chebyshev
from .base import PlayContext, State, StateOutcome
from .gather_safe import hostiles_near
from .intents import arm, set_position, take, use_self
from ..executor.intents import wait

# Food lying in sight that Heal tries to path to, nearest first.
FOOD_CANDIDATES = 3
# A reflex takes only food close by: walking further for food is a planner
# op (``fetch_item``), not something Heal starts on its own.
FOOD_REACH = 3


class HealState(State):
    """Reflex, above Fight. Hurt and out of combat: food within ``FOOD_REACH``, then carried
    food or a potion, then safe ground. Every walk, to food or a safe tile, is
    bounded by stuck detection (``bounded_step``): one that goes nowhere gives
    its target up.

    In a safe zone Heal walks to the zone's own unexplored edge, never out
    of it, sampling regen as it goes; with none left it sends ``Wait`` and
    starts no walk, so the sample is never cut short by walking out and
    back, and a long rest leaves stuck detection untouched.
    Once this run has measured no regen, Heal never samples or walks to a
    safe zone again: it asks the planner once for food and potions (a
    ``heal_supplies`` trigger) and sends nothing, so the plan's executor or
    the safe default moves.

    Heal also runs, even at full health or with a hostile in range, while the
    weapon a drink swapped out is still to be re-armed (A24). That is one
    ``Arm``, sent once."""

    name = "Heal"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        if ctx.policy.kind != "scripted" or not world.alive or world.pos is None:
            return False
        return _wants_heal(world, ctx) or ctx.memory.heal_rearm is not None

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not self.guard(world, ctx)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        if not _wants_heal(world, ctx):
            return _rearm_weapon(world, ctx.memory)
        return _choose(world, ctx)


def _wants_heal(w: WorldModel, ctx: PlayContext) -> bool:
    """Hurt and out of combat."""
    return hurt(w) and not hostiles_in_range(w, ctx.policy)


def _choose(w: WorldModel, ctx: PlayContext) -> StateOutcome:
    m, policy = ctx.memory, ctx.policy

    # Food lying in sight, then carried food, then a carried potion (the
    # plan's Heal row). Health changes from these must not count as regen.
    if out := _act_food(w, m, policy, ctx):
        m.heal_regen_sample = None
        return out
    if out := _act_carried(w, m):
        m.heal_regen_sample = None
        return out
    # Nothing left to drink: put the weapon back before walking.
    if m.heal_rearm is not None:
        return _rearm_weapon(w, m)

    known = regen_known(ctx.knowledge, m)
    if known == "no":
        _ask_for_supplies(w, m)
        return _out(None, "no safe-zone regen this run")
    if not standing_in_safe_zone(w):
        m.heal_regen_sample = None
        goal = "heal_rest" if known == "yes" else "heal_measure"
        if out := _walk_to_safe(w, m, policy, ctx, goal=goal):
            return out
        return _out(None, "no reachable safe tile")
    if known is None:
        verdict = note_regen_sample(m, w)
        if verdict == "yes":
            save_regen_yes(ctx.knowledge)
        elif verdict == "no":
            m.heal_regen_absent = True  # this run only: never saved (one noisy window)
            _ask_for_supplies(w, m)
            return _out(None, "no safe-zone regen this run")
    return _explore_zone(w, m, policy, ctx)


def _explore_zone(w: WorldModel, m: Memory, policy: Policy, ctx: PlayContext) -> StateOutcome:
    """Resting in a safe zone: walk to the nearest unexplored edge of the zone
    itself, or ``Wait`` when none is left.

    Never the safe default: its frontiers lie outside the zone, and leaving
    cuts the regen sample short and walks straight back (live flip-flop). A
    rest with nothing to explore starts no walk and no stuck attempt, so a
    long heal leaves stuck detection as it was.
    """
    here = w.pos
    assert here is not None
    blocked, _ = _plan_blocked(w, m, policy, ctx)
    goal = "heal_explore"
    edge = sorted(
        (chebyshev(here, p), p)
        for p in w.view.frontier() - {here}
        if standing_in_safe_zone(w, p)
        and p not in blocked
        and not hostiles_near(w, p, policy)
        and not nav_stuck.backed_off(m, goal, w.map_id, p, w.tick)
    )
    if edge:
        # Off-zone cells are walls for this walk, so it never steps out of the zone.
        outside = {p for p in w.view.tiles if not standing_in_safe_zone(w, p)}
        target = edge[0][1]
        if out := _walk_toward(w, m, policy, ctx, target, goal=goal, avoid=outside):
            out.reason = f"heal in safe ground: {out.reason}"
            return out
    return _out([wait()], "heal: rest in the safe zone")


def _ask_for_supplies(w: WorldModel, m: Memory) -> None:
    """Hurt, nothing to eat or drink, and no regen: ask the planner for food
    and potions (a ``fetch_item`` or ``buy``), once until health is full again
    (``note_heal_window`` re-arms it)."""
    if m.heal_supplies_asked:
        return
    m.heal_supplies_asked = True
    queue_signal(
        m,
        {"trigger": "heal_supplies", "health": w.health, "max_health": w.max_health, "regen": "no", "tick": w.tick},
    )


def _out(intents: list[dict] | None, reason: str) -> StateOutcome:
    return StateOutcome(intents, reason, state=HealState.name)


def _plan_blocked(w: WorldModel, m: Memory, policy: Policy, ctx: PlayContext) -> tuple[set[Pos], set[Pos]]:
    nav_avoid, nav_costly = navigation_avoid_costly(m.nav, ctx.knowledge, w.map_id, w.tick)
    hazards = {p for p, b in w.view.tiles.items() if b in policy.avoid_blocks}
    return nav_avoid | hazards, nav_costly | hazards


def _walk_to_safe(w: WorldModel, m: Memory, policy: Policy, ctx: PlayContext, *, goal: str) -> StateOutcome | None:
    target = nearest_known_safe(w, skip=lambda p: nav_stuck.backed_off(m, goal, w.map_id, p, w.tick))
    if target is None:
        return None
    return _walk_toward(w, m, policy, ctx, target[1], goal=goal)


def _walk_toward(
    w: WorldModel, m: Memory, policy: Policy, ctx: PlayContext, at: Pos, *, goal: str, avoid: set[Pos] = frozenset()
) -> StateOutcome | None:
    """One step along a cost path to ``at``, or None when there is no step there now.

    The walk is bounded like any other (``bounded_step``, A15): no path, or
    no progress in its window, gives ``at`` up with a backoff, so Heal tries
    the next food, a carried supply or a safe tile instead of pacing.
    """
    plan_avoid, plan_costly = _plan_blocked(w, m, policy, ctx)
    plan_avoid = plan_avoid | avoid

    def params():
        return grid_params(policy, plan_avoid, plan_costly)

    def plan() -> list[Pos] | None:
        return cost_path(w, at, params(), nav=nav_search(m, w, goal, at))

    step = bounded_step(m, w, goal, at, plan_avoid, plan, params=params)
    return _out([set_position(step)], f"{goal} → {at}") if step is not None else None


def _act_food(w: WorldModel, m: Memory, policy: Policy, ctx: PlayContext) -> StateOutcome | None:
    here = w.pos
    assert here is not None
    close = [f for f in food_in_sight(w, m) if chebyshev(f.pos, here) <= FOOD_REACH]
    for food in close[:FOOD_CANDIDATES]:
        if chebyshev(food.pos, here) <= 1:
            nav_stuck.finish_in_reach(m, w, "heal_food")
            note_try(m, "take", food.id)
            return _out([take(food.id)], f"take food {food.code}")
        # Walking onto it also picks up food eaten on pickup (golden cap).
        if out := _walk_toward(w, m, policy, ctx, food.pos, goal="heal_food"):
            return out
    return None


def _act_carried(w: WorldModel, m: Memory) -> StateOutcome | None:
    """``Arm`` + ``Use`` self on carried food or a potion (API Use).

    The weapon armed before the first drink is remembered in ``heal_rearm``
    and put back by ``_rearm_weapon`` once there is nothing left to drink
    (A24), so a second potion does not cost a re-arm in between. A rejected
    ``Use`` is retried at most ``HEAL_MAX_TRIES`` times per supply.
    """
    item = carried_heal(w, m)
    if item is None:
        return None
    note_try(m, "use", item.id)
    if w.armed_code == item.code:
        return _out([use_self(w.character_id)], f"use {item.code}")
    if m.heal_rearm is None and w.armed_code is not None:
        m.heal_rearm = w.armed_code
    return _out([arm(item.id), use_self(w.character_id)], f"arm and use {item.code}")


def _rearm_weapon(w: WorldModel, m: Memory) -> StateOutcome:
    """Re-``Arm`` the weapon a drink swapped out (A24). Sends nothing when it is
    already armed or no longer in hand."""
    code = m.heal_rearm
    if intents := rearm_after_drink(w, m):
        return _out(intents, f"re-arm {code}")
    return _out(None, f"no re-arm needed for {code}")
