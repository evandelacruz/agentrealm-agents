"""Explore: scripted reflexes and policy goals (M3 pathing, A12 cost grid)."""

from __future__ import annotations

import random

from ..config import Policy
from ..knowledge_base import KnowledgeBase
from ..memory import Memory
from ..plan import Plan
from ..navigation.rejection import navigation_avoid_costly
from ..navigation import stuck as nav_stuck
from ..pathing import grid_params, next_step, path_owned_by_plan, replan
from ..world import Pos, WorldModel
from .base import PlayContext, State, StateOutcome
from .intents import set_position
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
            plan=ctx.plan,
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
    plan: Plan | None = None,
    state: str = "Explore",
) -> StateOutcome:
    """Reflex list then plan (PLAN.md). M7 test seam: list[Intent] in the outcome."""
    if w.pos is None:
        return StateOutcome(None, "position unknown", state=state)
    _, plan_avoid, plan_costly = plan_sets(w, m, policy, knowledge)
    reflex = reflex_outcome(w, policy, never_attack=never_attack, state=state, knowledge=knowledge)
    if reflex is not None:
        return reflex

    if plan is not None:
        plan.advance(w)
        op = plan.current()
        if op is not None and op["op"] == "wait":
            return StateOutcome(None, "plan wait", state=state)

    params = grid_params(policy, plan_avoid, plan_costly, m=m)
    att = m.nav_stuck.attempt
    if att is not None and m.path:
        nav_stuck.maybe_escalate(m, w, att.target, params)
    step = nav_stuck.reveal_step(w, m, plan_avoid) if nav_stuck.in_reveal(m) else None
    if step is None:
        step = next_step(w, plan_avoid, m.path) if path_owned_by_plan(plan, m, policy.goals) else None
    if step is None:
        replan(w, m, policy, rng, plan_avoid, plan_costly, knowledge, plan=plan)
        step = next_step(w, plan_avoid, m.path)
    if step is None and att is not None:
        nav_stuck.maybe_escalate(m, w, att.target, params)
        if nav_stuck.in_reveal(m):
            step = nav_stuck.reveal_step(w, m, plan_avoid)
        elif not m.path:
            replan(w, m, policy, rng, plan_avoid, plan_costly, knowledge, plan=plan)
            step = next_step(w, plan_avoid, m.path)
    if step is not None:
        label = m.path[-1] if m.path else att.target if att else "?"
        return StateOutcome([set_position(step)], f"{m.goal} → {label}", state=state)

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
    policy: Policy,
    *,
    never_attack: list[str],
    state: str,
    knowledge: KnowledgeBase | None = None,
) -> StateOutcome | None:
    """Reflex 4 (PLAN.md): the pickup rule of A20.

    **Fight** (A23) owns hostile swings. Stepping off a hazard is **Escape**
    and fleeing is **Flee** (A9); both outrank every state that calls this.
    """
    here = w.pos
    if here is None:
        return None
    if policy.pickup:
        return pickup_outcome(w, knowledge, state=state)
    return None
