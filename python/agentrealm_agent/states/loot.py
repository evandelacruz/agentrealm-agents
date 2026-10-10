"""Loot: carry out the plan's ``fetch_item`` op (A20).

Walks to a free supply of the op's code in sight and ``Take``s it; with none
in sight and the op naming a cell, walks there to look. The supply it walks
to is committed (A71): kept until taken, seen gone, or given up, even when
another of the same code comes nearer. A supply underfoot or adjacent that
is merely worthwhile is **Pickup**'s, a reflex.
"""

from __future__ import annotations

from .. import targets as targets_mod
from ..healing import supply_matches
from ..navigation import cost_path
from ..navigation import stuck as nav_stuck
from ..pathing import bounded_step, grid_params, nav_search
from ..world import Entity, Pos, WorldModel, chebyshev
from .base import PlayContext, State, StateOutcome, my_op
from .explore import plan_sets
from .intents import set_position, take

GOAL = "loot"


class LootState(State):
    """Executor for ``fetch_item``."""

    name = "Loot"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        if ctx.policy.kind != "scripted" or not world.alive or world.pos is None:
            return False
        return my_op(ctx, self.name) is not None

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not self.guard(world, ctx)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        op = my_op(ctx, self.name)
        assert op is not None and world.pos is not None
        code = op["code"]
        supply = _committed_supply(world, ctx, op)
        if supply is not None and chebyshev(supply.pos, world.pos) <= 1:
            nav_stuck.finish_in_reach(ctx.memory, world, GOAL)
            return StateOutcome([take(supply.id)], f"fetch {code}", state=self.name)
        target = supply.pos if supply is not None else (op["x"], op["y"]) if "x" in op else None
        if target is None:
            return StateOutcome(None, f"no {code} in sight", state=self.name)
        step = _step_toward(world, ctx, target)
        if step is None:
            return StateOutcome(None, f"no step toward {code}", state=self.name)
        return StateOutcome([set_position(step)], f"fetch {code} → {target}", state=self.name)


def _committed_supply(w: WorldModel, ctx: PlayContext, op: dict) -> Entity | None:
    """The supply this ``fetch_item`` op committed to while it is still there,
    else the nearest one of its code, committed from now on (A71).

    One out of sight is kept too: its cell is walked to and looked at, and
    only a look that finds it gone lets it go. A change of map, or stuck
    detection giving the walk to it up (``bounded_step``'s backoff), lets it go too.
    """
    m, code = ctx.memory, op["code"]
    here = w.pos
    assert here is not None

    def given_up(pos: Pos) -> bool:
        return nav_stuck.backed_off(m, GOAL, w.map_id, pos, w.tick)

    def pick() -> tuple[int | None, int, Pos] | None:
        e = _nearest_supply(w, code, skip=given_up)
        return (w.map_id, e.id, e.pos) if e is not None else None

    def keep(t: tuple[int | None, int, Pos]) -> bool:
        mid, sid, pos = t
        if mid != w.map_id or given_up(pos):
            return False
        return any(e.kind == "supply" and e.id == sid for e in w.entities) or chebyshev(pos, here) > w.perception

    held = targets_mod.hold(m, w, GOAL, pick, keep, op)
    if held is None:
        return None
    _, sid, pos = held
    return next((e for e in w.entities if e.kind == "supply" and e.id == sid), None) or Entity("supply", sid, pos, code)


def _nearest_supply(w: WorldModel, code: str, skip=lambda pos: False) -> Entity | None:
    """The nearest free (unpriced) ground supply of ``code`` in sight, leaving out cells ``skip`` names."""
    here = w.pos
    found = [
        e
        for e in w.entities
        if e.kind == "supply" and e.gem_price is None and supply_matches(code, e.code) and not skip(e.pos)
    ]
    return min(found, key=lambda e: (chebyshev(e.pos, here), e.id)) if found and here is not None else None


def _step_toward(w: WorldModel, ctx: PlayContext, goal: Pos) -> Pos | None:
    """Next step of the kept loot path to ``goal``, bounded like any walk (``bounded_step``, A15)."""
    m, policy = ctx.memory, ctx.policy
    _, plan_avoid, plan_costly = plan_sets(w, m, policy, ctx.knowledge)

    def params():
        return grid_params(policy, plan_avoid, plan_costly)

    def plan() -> list[Pos] | None:
        return cost_path(w, goal, params(), nav=nav_search(m, w, GOAL, goal))

    return bounded_step(m, w, GOAL, goal, plan_avoid, plan, params=params)
