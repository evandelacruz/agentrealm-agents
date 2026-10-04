"""Explore: scripted reflexes and policy goals (M3 pathing, A12 cost grid)."""

from __future__ import annotations

import random

from ..brain import (
    PlayContext,
    _flee_step,
    _grid,
    _next_step,
    _replan,
    set_position,
    take,
    use_on,
    withdraw_all,
)
from ..config import Policy
from ..directives import attack_forbidden
from ..navigation import cost_path
from ..world import WorldModel, chebyshev
from .base import State, StateOutcome


class ExploreState(State):
    name = "Explore"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        return ctx.policy.kind == "scripted" and world.alive and world.pos is not None

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not self.guard(world, ctx)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        return scripted_outcome(
            world, ctx.memory, ctx.policy, ctx.rng, never_attack=ctx.never_attack, state=self.name
        )


def scripted_outcome(
    w: WorldModel,
    m,
    policy: Policy,
    rng: random.Random,
    *,
    never_attack: list[str],
    state: str = "Explore",
) -> StateOutcome:
    """Reflex list then plan (PLAN.md). M7 test seam: list[Intent] in the outcome."""
    here = w.pos
    assert here is not None
    view = w.view

    hazards = {p for p, b in view.tiles.items() if b in policy.avoid_blocks}
    blocked = set(m.blocked) | hazards
    escape: set = set()
    if here in hazards:
        safe = w.open_neighbours(here, blocked)
        if safe:
            m.path = []
            p = min(safe)
            return StateOutcome([set_position(p)], f"off {view.tiles.get(here)}", reflex=True, state=state)
        escape = hazards
    plan_avoid = blocked - escape

    hostiles = [e for e in w.entities if e.kind in policy.hostile and chebyshev(e.pos, here) <= policy.hostile_range]
    if hostiles and policy.on_hostile != "ignore":
        target = min(hostiles, key=lambda e: (chebyshev(e.pos, here), e.id))
        if policy.on_hostile == "fight":
            if target.kind == "character" and not attack_forbidden(target, never_attack):
                m.path = []
                return StateOutcome(
                    [use_on(target)], f"fight {target.kind} {target.id}", reflex=True, state=state
                )
        away = _flee_step(w, hostiles, blocked)
        if away is not None:
            m.path = []
            return StateOutcome(
                [set_position(away)], f"flee {target.kind} {target.id}", reflex=True, state=state
            )

    if policy.pickup:
        near = [e for e in w.entities if e.kind == "supply" and chebyshev(e.pos, here) <= 1]
        if near:
            s = min(near, key=lambda e: (chebyshev(e.pos, here), e.id))
            return StateOutcome([take(s)], f"take {s.code or s.id}", reflex=True, state=state)

    if policy.pickup and w.death_chest is not None and w.death_chest[0] == w.map_id:
        _, at, chest_id = w.death_chest
        if chebyshev(at, here) <= 1:
            contents = w.chest_contents.get(chest_id)
            if contents:
                return StateOutcome(
                    [withdraw_all(chest_id)], f"recover from chest {chest_id}", reflex=True, state=state
                )
            if contents is None:
                return StateOutcome(None, f"open chest {chest_id}", state=state)
        elif m.goal != "chest" or not _next_step(w, plan_avoid, m.path):
            found = cost_path(w, at, _grid(policy, plan_avoid, escape))
            if _next_step(w, plan_avoid, found):
                m.path, m.goal = found, "chest"

    step = _next_step(w, plan_avoid, m.path)
    if step is None:
        _replan(w, m, policy, rng, plan_avoid, escape)
        step = _next_step(w, plan_avoid, m.path)
    if step is not None:
        return StateOutcome([set_position(step)], f"{m.goal} → {m.path[-1]}", state=state)

    return StateOutcome(None, "no goal reachable", state=state)
