"""Heal: food, potion, safe-zone rest, town wait and buy signal (A10)."""

from __future__ import annotations

from ..config import Policy
from ..healing import (
    carried_potion,
    flush_regen_measurement,
    food_in_sight,
    hostiles_in_range,
    hurt,
    nearest_known_safe,
    note_regen_sample,
    raise_buy_potion,
    regen_measurement,
    reset_regen_sample,
    standing_in_safe_zone,
)
from ..memory import Memory
from ..navigation import cost_path
from ..navigation.rejection import navigation_avoid_costly
from ..pathing import grid_params, nav_search, next_step
from ..world import Pos, WorldModel, chebyshev
from .base import PlayContext, State, StateOutcome
from .intents import arm, set_position, take, use_self


class HealState(State):
    name = "Heal"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        return (
            ctx.policy.kind == "scripted"
            and world.alive
            and world.pos is not None
            and hurt(world)
            and not hostiles_in_range(world, ctx.policy)
        )

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not hurt(world) or bool(hostiles_in_range(world, ctx.policy))

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        m, policy = ctx.memory, ctx.policy
        here = world.pos
        assert here is not None

        food_act = _act_food(world, m, policy)
        if food_act is not None:
            reset_regen_sample(m)
            return food_act

        potion_act = _act_potion(world, m)
        if potion_act is not None:
            reset_regen_sample(m)
            return potion_act

        kb = ctx.knowledge
        known = regen_measurement(kb)

        if known == "yes":
            if standing_in_safe_zone(world):
                return StateOutcome(None, "rest in safe zone", state=self.name)
            if walk := _walk_to_safe(world, m, policy, ctx, goal="heal_safe"):
                return walk
            return StateOutcome(None, "seek safe zone to rest", state=self.name)

        if known == "no":
            return _town_wait(world, m, policy, ctx, self.name)

        # Measure whether health returns in a safe zone (M7).
        if standing_in_safe_zone(world):
            note_regen_sample(m, world)
            if m.heal_regen_measured == "yes":
                flush_regen_measurement(m, kb)
                return StateOutcome(None, "rest in safe zone", state=self.name)
            if m.heal_regen_measured == "no":
                flush_regen_measurement(m, kb)
                return _town_wait(world, m, policy, ctx, self.name)
            return StateOutcome(None, "measure safe-zone regen", state=self.name)

        if walk := _walk_to_safe(world, m, policy, ctx, goal="heal_measure"):
            return walk
        return _town_wait(world, m, policy, ctx, self.name)


def _plan_blocked(w: WorldModel, m: Memory, policy: Policy, ctx: PlayContext) -> tuple[set[Pos], set[Pos]]:
    nav_avoid, nav_costly = navigation_avoid_costly(m.nav, ctx.knowledge, w.map_id, w.tick)
    hazards = {p for p, b in w.view.tiles.items() if b in policy.avoid_blocks}
    blocked = nav_avoid | hazards
    return blocked, nav_costly | hazards


def _town_wait(
    world: WorldModel,
    m: Memory,
    policy: Policy,
    ctx: PlayContext,
    state: str,
) -> StateOutcome:
    here = world.pos
    assert here is not None
    if not standing_in_safe_zone(world):
        if walk := _walk_to_safe(world, m, policy, ctx, goal="heal_town"):
            return walk
    raise_buy_potion(m, why="hurt in town, no food or potion")
    return StateOutcome(None, "wait in town, buy potion", state=state)


def _walk_to_safe(
    w: WorldModel,
    m: Memory,
    policy: Policy,
    ctx: PlayContext,
    *,
    goal: str,
) -> StateOutcome | None:
    target = nearest_known_safe(w)
    if target is None:
        return None
    return _walk_toward(w, m, policy, ctx, target, goal=goal)


def _walk_toward(
    w: WorldModel,
    m: Memory,
    policy: Policy,
    ctx: PlayContext,
    target: tuple[int, Pos],
    *,
    goal: str,
) -> StateOutcome | None:
    map_id, at = target
    if w.map_id != map_id:
        m.path, m.goal = [], ""
        return None
    plan_avoid, plan_costly = _plan_blocked(w, m, policy, ctx)
    if m.goal != goal or not m.path:
        found = cost_path(w, at, grid_params(policy, plan_avoid, plan_costly), nav=nav_search(m, w, goal, at))
        if next_step(w, plan_avoid, found):
            m.path, m.goal = found, goal
    step = next_step(w, plan_avoid, m.path)
    if step is not None:
        return StateOutcome([set_position(step)], f"{goal} → {at}", state="Heal")
    return None


def _act_food(w: WorldModel, m: Memory, policy: Policy) -> StateOutcome | None:
    foods = food_in_sight(w)
    if not foods:
        return None
    here = w.pos
    assert here is not None
    target = foods[0]
    if target.pos == here:
        return StateOutcome([take(target)], f"take food {target.code}", state="Heal")
    if chebyshev(target.pos, here) <= 1:
        return StateOutcome([take(target)], f"take food {target.code}", state="Heal")
    # Walk onto the food tile (pickup-on-step for types like golden_cap).
    return StateOutcome([set_position(target.pos)], f"walk to food {target.code}", state="Heal")


def _act_potion(w: WorldModel, m: Memory) -> StateOutcome | None:
    """Drink a carried potion with Arm + Use self. Re-arming the weapon is A24."""
    potion = carried_potion(w)
    if potion is None:
        return None
    cid = w.character_id
    if w.armed_code and w.armed_code == potion.code:
        return StateOutcome([use_self(cid)], f"drink {potion.code}", state="Heal")
    return StateOutcome(
        [arm(potion.supply_id), use_self(cid)],
        f"arm and drink {potion.code}",
        state="Heal",
    )
