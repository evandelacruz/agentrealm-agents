"""Heal: food, carried food or potion, safe-zone rest, town wait and buy signal (A10)."""

from __future__ import annotations

from ..config import Policy
from ..healing import (
    back_off,
    carried_heal,
    food_in_sight,
    hurt,
    nearest_known_safe,
    note_regen_sample,
    note_try,
    raise_buy_potion,
    regen_known,
    save_regen_yes,
    standing_in_safe_zone,
    wait_exhausted,
)
from ..memory import Memory
from ..navigation import cost_path
from ..navigation.rejection import navigation_avoid_costly
from ..pathing import grid_params, hostiles_in_range, nav_search, next_step
from ..world import Pos, WorldModel, chebyshev
from .base import PlayContext, State, StateOutcome
from .intents import arm, set_position, take, use_self

# Food lying in sight that Heal tries to path to, nearest first.
FOOD_CANDIDATES = 3


class HealState(State):
    """Above Explore. Every branch that sends nothing is bounded: no reachable
    safe tile yields at once, and a wait with no health back yields after
    ``HEAL_WAIT_TICKS``; Heal then stays out for ``HEAL_BACKOFF_TICKS``."""

    name = "Heal"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        return (
            ctx.policy.kind == "scripted"
            and world.alive
            and world.pos is not None
            and world.tick >= ctx.memory.heal_backoff_until
            and hurt(world)
            and not hostiles_in_range(world, ctx.policy)
        )

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not self.guard(world, ctx)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        out = _choose(world, ctx)
        if out.intents:
            ctx.memory.heal_wait = None
        return out


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

    known = regen_known(ctx.knowledge, m)
    if not standing_in_safe_zone(w):
        m.heal_regen_sample = None
        goal = {"yes": "heal_rest", "no": "heal_town"}.get(known, "heal_measure")
        if out := _walk_to_safe(w, m, policy, ctx, goal=goal):
            return out
        back_off(m, w)
        return _out(None, "no reachable safe tile, yield to Explore")

    if wait_exhausted(m, w):
        return _out(None, "no health back, yield to Explore")
    if known is None:
        verdict = note_regen_sample(m, w)
        if verdict == "yes":
            save_regen_yes(ctx.knowledge)
            known = "yes"
        elif verdict == "no":
            m.heal_regen_absent = True  # this run only: never saved (one noisy window)
            known = "no"
        else:
            return _out(None, "measure safe-zone regen")
    if known == "yes":
        return _out(None, "rest in safe zone")
    raise_buy_potion(m, why="hurt in town, no food or potion")
    return _out(None, "wait in town, buy potion")


def _out(intents: list[dict] | None, reason: str) -> StateOutcome:
    return StateOutcome(intents, reason, state=HealState.name)


def _plan_blocked(w: WorldModel, m: Memory, policy: Policy, ctx: PlayContext) -> tuple[set[Pos], set[Pos]]:
    nav_avoid, nav_costly = navigation_avoid_costly(m.nav, ctx.knowledge, w.map_id, w.tick)
    hazards = {p for p, b in w.view.tiles.items() if b in policy.avoid_blocks}
    return nav_avoid | hazards, nav_costly | hazards


def _walk_to_safe(w: WorldModel, m: Memory, policy: Policy, ctx: PlayContext, *, goal: str) -> StateOutcome | None:
    target = nearest_known_safe(w)
    if target is None:
        return None
    return _walk_toward(w, m, policy, ctx, target[1], goal=goal)


def _walk_toward(w: WorldModel, m: Memory, policy: Policy, ctx: PlayContext, at: Pos, *, goal: str) -> StateOutcome | None:
    """One step along a cost path to ``at``, or None when no seen, open step leads there."""
    plan_avoid, plan_costly = _plan_blocked(w, m, policy, ctx)
    if m.goal != goal or not m.path or m.path[-1] != at or not next_step(w, plan_avoid, m.path):
        m.path, m.goal = [], ""
        found = cost_path(w, at, grid_params(policy, plan_avoid, plan_costly), nav=nav_search(m, w, goal, at))
        if not next_step(w, plan_avoid, found):
            return None
        m.path, m.goal = found, goal
    step = next_step(w, plan_avoid, m.path)
    assert step is not None
    return _out([set_position(step)], f"{goal} → {at}")


def _act_food(w: WorldModel, m: Memory, policy: Policy, ctx: PlayContext) -> StateOutcome | None:
    here = w.pos
    assert here is not None
    for food in food_in_sight(w, m)[:FOOD_CANDIDATES]:
        if chebyshev(food.pos, here) <= 1:
            note_try(m, "take", food.id)
            return _out([take(food)], f"take food {food.code}")
        # Walking onto it also picks up food eaten on pickup (golden cap).
        if out := _walk_toward(w, m, policy, ctx, food.pos, goal="heal_food"):
            return out
    return None


def _act_carried(w: WorldModel, m: Memory) -> StateOutcome | None:
    """``Arm`` + ``Use`` self on carried food or a potion (API Use).

    The weapon stays unarmed afterwards: re-arming it is A24. A rejected
    ``Use`` is retried at most ``HEAL_MAX_TRIES`` times per supply.
    """
    item = carried_heal(w, m)
    if item is None:
        return None
    note_try(m, "use", item.supply_id)
    if w.armed_code == item.code:
        return _out([use_self(w.character_id)], f"use {item.code}")
    return _out([arm(item.supply_id), use_self(w.character_id)], f"arm and use {item.code}")
