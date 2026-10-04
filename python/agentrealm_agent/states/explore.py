"""Explore: scripted reflexes and policy goals (M3 pathing, A12 cost grid)."""

from __future__ import annotations

import random

from ..config import Policy
from ..knowledge_base import KnowledgeBase
from ..memory import Memory
from ..plan import Plan
from ..navigation.rejection import navigation_avoid_costly
from ..navigation import stuck as nav_stuck
from ..pathing import attempt_plan, escalation_step, next_step, path_owned_by_plan, replan
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
        plan.advance(w, m)
        op = plan.current()
        if op is not None and op["op"] == "wait":
            return StateOutcome(None, "plan wait", state=state)

    owned = path_owned_by_plan(plan, m, policy.goals)
    target_plan = attempt_plan(m, w, policy, plan_avoid, plan_costly)
    step = _escalated_step(w, m, plan_avoid, target_plan) if owned else None
    if step is None and owned:
        step = next_step(w, plan_avoid, m.path)
        att = nav_stuck.active(m, w)
        if step is not None and att is not None and m.goal == att.goal:
            nav_stuck.observe(att, w, m.path)
    if step is None:
        missed = replan(w, m, policy, rng, plan_avoid, plan_costly, knowledge, plan=plan)
        step = next_step(w, plan_avoid, m.path)
        if step is None and missed is not None:
            step = _missed_step(w, m, plan_avoid, target_plan, *missed)
    if step is not None:
        att = nav_stuck.active(m, w)
        label = m.path[-1] if m.path else att.target if att else step
        note = nav_stuck.level_note(att) if att is not None and att.goal == m.goal else ""
        return StateOutcome([set_position(step)], f"{m.goal} → {label}{note}", state=state)

    return StateOutcome(None, "no goal reachable", state=state)


def _missed_step(
    w: WorldModel, m: Memory, avoid: set[Pos], target_plan, goal: str, target: Pos, routed: bool
) -> Pos | None:
    """No goal got a step. A goal on this map with no route at all is stuck at
    once; one whose route starts on a taken cell waits out its window (A15)."""
    att = nav_stuck.track(m, w, goal, target)
    if att is None:
        return None
    if att.level == nav_stuck.REVEAL:
        return escalation_step(m, w, att, avoid, target_plan, None)
    reason = nav_stuck.stuck_reason(att, w.tick) if routed else "no_path"
    return escalation_step(m, w, att, avoid, target_plan, reason) if reason else None


def _escalated_step(w: WorldModel, m: Memory, avoid: set[Pos], target_plan) -> Pos | None:
    """The active attempt's move when it is stuck or revealing (A15), else None.

    None also when the attempt was just given up: the caller replans, and
    the backoff keeps that target out of the new choice.
    """
    att = nav_stuck.active(m, w)
    if att is None or m.goal != att.goal:
        return None
    if nav_stuck.done(att, w):
        nav_stuck.finish(m, att)
        return None
    if att.level == nav_stuck.REVEAL:
        return escalation_step(m, w, att, avoid, target_plan, None)
    reason = nav_stuck.stuck_reason(att, w.tick)
    return escalation_step(m, w, att, avoid, target_plan, reason) if reason else None


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
