"""Explore: scripted reflexes and policy goals (M3 pathing, A12 cost grid)."""

from __future__ import annotations

import random

from ..config import Policy
from ..directives import attack_forbidden
from ..knowledge_base import KnowledgeBase
from ..memory import Memory
from ..navigation.rejection import navigation_avoid_costly
from ..pathing import flee_step, hostiles_in_range, next_step, replan
from ..world import Pos, WorldModel, chebyshev
from .base import PlayContext, State, StateOutcome
from .intents import set_position, use_on
from .pickup import pickup_outcome


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
    if w.pos is None:
        return StateOutcome(None, "position unknown", state=state)
    blocked, plan_avoid, plan_costly = plan_sets(w, m, policy, knowledge)
    reflex = reflex_outcome(w, m, policy, blocked, never_attack=never_attack, state=state, knowledge=knowledge)
    if reflex is not None:
        return reflex

    step = next_step(w, plan_avoid, m.path)
    if step is None:
        replan(w, m, policy, rng, plan_avoid, plan_costly, knowledge)
        step = next_step(w, plan_avoid, m.path)
    if step is not None:
        return StateOutcome([set_position(step)], f"{m.goal} → {m.path[-1]}", state=state)

    return StateOutcome(None, "no goal reachable", state=state)


def plan_sets(
    w: WorldModel, m: Memory, policy: Policy, knowledge: KnowledgeBase | None
) -> tuple[set[Pos], set[Pos], set[Pos]]:
    """``blocked``, ``plan_avoid``, ``plan_costly`` for this decision.

    Rejection learnings and ``avoid_blocks`` hazards stay out of every choice
    (A14, reflex 1). Standing on a hazard with no way straight off, the plan may
    cross hazards at a high price instead.
    """
    nav_avoid, nav_costly = navigation_avoid_costly(m.nav, knowledge, w.map_id, w.tick)
    hazards = {p for p, b in w.view.tiles.items() if b in policy.avoid_blocks}
    blocked = nav_avoid | hazards
    escape = hazards if w.pos in hazards else set()
    return blocked, blocked - escape, escape | nav_costly


def reflex_outcome(
    w: WorldModel,
    m: Memory,
    policy: Policy,
    blocked: set[Pos],
    *,
    never_attack: list[str],
    state: str,
    knowledge: KnowledgeBase | None = None,
) -> StateOutcome | None:
    """Reflexes 2–4 (PLAN.md): off a hazard, fight or flee, the pickup rule of A20."""
    here = w.pos
    if here is None:
        return None
    view = w.view
    if view.tiles.get(here) in policy.avoid_blocks:
        safe = w.open_neighbours(here, blocked)
        if safe:
            m.path = []
            p = min(safe)
            return StateOutcome([set_position(p)], f"off {view.tiles.get(here)}", reflex=True, state=state)

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
        return pickup_outcome(w, knowledge, state=state)
    return None
