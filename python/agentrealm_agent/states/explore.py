"""Explore: scripted reflexes and policy goals (M3 pathing, A12 cost grid)."""

from __future__ import annotations

import random

from ..config import Policy
from ..directives import attack_forbidden
from ..knowledge_base import KnowledgeBase
from ..memory import Memory
from ..navigation import cost_path
from ..navigation.rejection import navigation_avoid_costly
from ..pathing import flee_step, grid_params, hostiles_in_range, nav_search, next_step, replan
from ..world import Pos, WorldModel, chebyshev
from .base import PlayContext, State, StateOutcome
from .intents import set_position, take, use_on, withdraw_all


class ExploreState(State):
    name = "Explore"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        return ctx.policy.kind == "scripted" and world.alive and world.pos is not None

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not self.guard(world, ctx)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        return scripted_outcome(
            world,
            ctx.memory,
            ctx.policy,
            ctx.rng,
            never_attack=ctx.never_attack,
            knowledge=ctx.knowledge,
            state=self.name,
        )


def scripted_outcome(
    w: WorldModel,
    m: Memory,
    policy: Policy,
    rng: random.Random,
    *,
    never_attack: list[str],
    knowledge: KnowledgeBase | None = None,
    state: str = "Explore",
) -> StateOutcome:
    """Reflex list then plan (PLAN.md). M7 test seam: list[Intent] in the outcome."""
    here = w.pos
    if here is None:
        return StateOutcome(None, "position unknown", state=state)
    view = w.view

    # Rejection learnings stay out of every choice below (A14, reflex 1).
    nav_avoid, nav_costly = navigation_avoid_costly(m.nav, knowledge, w.map_id, w.tick)
    hazards = {p for p, b in view.tiles.items() if b in policy.avoid_blocks}
    blocked = nav_avoid | hazards
    escape: set[Pos] = set()
    if here in hazards:
        safe = w.open_neighbours(here, blocked)
        if safe:
            m.path = []
            p = min(safe)
            return StateOutcome([set_position(p)], f"off {view.tiles.get(here)}", reflex=True, state=state)
        escape = hazards
    plan_avoid = blocked - escape
    plan_costly = escape | nav_costly

    hostiles = hostiles_in_range(w, policy)
    if hostiles and policy.on_hostile != "ignore":
        target = min(hostiles, key=lambda e: (chebyshev(e.pos, here), e.id))
        if policy.on_hostile == "fight":
            if target.kind == "character" and not attack_forbidden(target, never_attack):
                return StateOutcome(
                    [use_on(target)], f"fight {target.kind} {target.id}", reflex=True, state=state
                )
        away = flee_step(w, hostiles, blocked)
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
        elif m.goal != "chest" or not next_step(w, plan_avoid, m.path):
            found = cost_path(w, at, grid_params(policy, plan_avoid, plan_costly), nav=nav_search(m, w, "chest", at))
            if next_step(w, plan_avoid, found):
                m.path, m.goal = found, "chest"

    step = next_step(w, plan_avoid, m.path)
    if step is None:
        replan(w, m, policy, rng, plan_avoid, plan_costly, knowledge)
        step = next_step(w, plan_avoid, m.path)
    if step is not None:
        return StateOutcome([set_position(step)], f"{m.goal} → {m.path[-1]}", state=state)

    return StateOutcome(None, "no goal reachable", state=state)
