"""Explore: scripted reflexes and policy goals (M3 pathing, A12 cost grid)."""

from __future__ import annotations

import random

from ..config import Policy
from ..directives import attack_forbidden
from ..knowledge_base import KnowledgeBase
from ..memory import Memory
from ..plan import Plan
from ..navigation.rejection import navigation_avoid_costly
from ..pathing import next_step, path_owned_by_plan, replan
from ..world import Entity, Pos, WorldModel, chebyshev
from .base import PlayContext, State, StateOutcome
from .intents import set_position, take, use_on


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
    reflex = reflex_outcome(w, policy, never_attack=never_attack, state=state)
    if reflex is not None:
        return reflex

    if plan is not None:
        plan.advance(w)
        op = plan.current()
        if op is not None and op["op"] == "wait":
            return StateOutcome(None, "plan wait", state=state)

    step = next_step(w, plan_avoid, m.path) if path_owned_by_plan(plan, m) else None
    if step is None:
        replan(w, m, policy, rng, plan_avoid, plan_costly, knowledge, plan=plan)
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
    policy: Policy,
    *,
    never_attack: list[str],
    state: str,
) -> StateOutcome | None:
    """Reflexes 3–4 (PLAN.md): fight a character, take a supply.

    Stepping off a hazard is **Escape** and fleeing is **Flee** (A9); both
    outrank every state that calls this.
    """
    here = w.pos
    if here is None:
        return None
    target = fight_target(w, policy, never_attack)
    if target is not None:
        return StateOutcome([use_on(target)], f"fight {target.kind} {target.id}", reflex=True, state=state)

    if policy.pickup:
        near = [e for e in w.entities if e.kind == "supply" and chebyshev(e.pos, here) <= 1]
        if near:
            s = min(near, key=lambda e: (chebyshev(e.pos, here), e.id))
            return StateOutcome([take(s)], f"take {s.code or s.id}", reflex=True, state=state)
    return None


def fight_target(w: WorldModel, policy: Policy, never_attack: list[str]) -> Entity | None:
    """With ``on_hostile = "fight"``, the nearest hostile in range if it may be hit.

    Only characters can be hit today (A23 adds NPCs); a hostile that cannot be
    hit is fled from instead (**Flee**).
    """
    here = w.pos
    if here is None or policy.on_hostile != "fight":
        return None
    hostiles = [e for e in w.entities if e.kind in policy.hostile and chebyshev(e.pos, here) <= policy.hostile_range]
    if not hostiles:
        return None
    target = min(hostiles, key=lambda e: (chebyshev(e.pos, here), e.id))
    if target.kind == "character" and not attack_forbidden(target, never_attack):
        return target
    return None
