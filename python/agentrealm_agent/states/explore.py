"""Explore: walk the plan's ``explore_area`` op, or the safe default (M3, A12, A34).

The executor for ``explore_area``, and the safe default when there is no
plan op to carry out (PLAN.md **Architecture: AI plans, state machine
executes**). The safe default explores safe ground (``safe_explore_path``):
off hazards, away from hostiles, and, when hurt, inside safe zones so the
character heals while it looks around. With no frontier left it steps to a
safe neighbour, so a decision with nothing to do is never idle. Stuck
detection escalates and gives up a walk that goes nowhere (A15); cells Step
rejections ruled out and ``avoid_blocks`` hazards stay out of the path (A14).
"""

from __future__ import annotations

import random

from ..config import Policy
from ..healing import hurt
from ..knowledge_base import KnowledgeBase
from ..memory import Memory
from ..navigation.rejection import navigation_avoid_costly
from ..navigation import stuck as nav_stuck
from ..pathing import (
    attempt_plan,
    clue_redirects,
    escalation_step,
    next_step,
    path_owned_by,
    replan,
)
from ..plan import GoalOp
from ..world import Pos, WorldModel
from .base import PlayContext, State, StateOutcome, my_op
from .gather_safe import hostiles_near, is_safe_ish
from .intents import set_position


class ExploreState(State):
    """Last state for a scripted character: the executor for ``explore_area``,
    and otherwise the safe default. It runs whenever no reflex or executor
    sent an intent: no plan op, or the top op's executor had nothing to do
    this decision (that op's stall clock then runs, A34)."""

    name = "Explore"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        return ctx.policy.kind == "scripted" and world.alive and world.pos is not None

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not self.guard(world, ctx)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        op = my_op(ctx, self.name)
        if op is not None:
            out = explore_outcome(world, ctx.memory, ctx.policy, ctx.rng, knowledge=ctx.knowledge, op=op)
            if out.intents:
                return out
        out = safe_default(world, ctx)
        out.progress = False  # it moves for no op: the top op, if any, still stalls
        return out


def safe_default(world: WorldModel, ctx: PlayContext) -> StateOutcome:
    """The safe default: explore safe ground (never idle)."""
    return explore_outcome(world, ctx.memory, ctx.policy, ctx.rng, knowledge=ctx.knowledge, op=None)


def explore_outcome(
    w: WorldModel,
    m: Memory,
    policy: Policy,
    rng: random.Random,
    *,
    knowledge: KnowledgeBase | None = None,
    op: GoalOp | None = None,
    state: str = "Explore",
) -> StateOutcome:
    """One step toward the ``explore_area`` ``op``, or with no op the safe default's.

    With an op and no step, sends nothing. The safe default always sends a
    step, or waits only when boxed in with no open safe neighbour.
    """
    if w.pos is None:
        return StateOutcome(None, "position unknown", state=state)
    _, plan_avoid, plan_costly = plan_sets(w, m, policy, knowledge)
    clue_redirects(w, m, policy, plan_avoid, plan_costly, knowledge, op)
    owned = path_owned_by(op, m)
    target_plan = attempt_plan(m, w, policy, plan_avoid, plan_costly, knowledge)
    step = _escalated_step(w, m, plan_avoid, target_plan, knowledge) if owned else None
    if step is None and owned:
        step = next_step(w, plan_avoid, m.path)
        att = nav_stuck.active(m, w)
        if step is not None and att is not None and m.goal == att.goal:
            nav_stuck.observe(att, w, m.path)
    if step is None:
        missed = replan(w, m, policy, plan_avoid, plan_costly, knowledge, op=op)
        step = next_step(w, plan_avoid, m.path)
        if step is None and missed is not None:
            step = _missed_step(w, m, plan_avoid, target_plan, knowledge, *missed)
    if step is not None:
        att = nav_stuck.active(m, w)
        label = m.path[-1] if m.path else att.target if att else step
        note = nav_stuck.level_note(att) if att is not None and att.goal == m.goal else ""
        return StateOutcome([set_position(step)], f"{m.goal} → {label}{note}", state=state)
    if op is not None:
        return StateOutcome(None, "no explore_area step", state=state)
    return _look_around(w, policy, rng, plan_avoid, state)


def _look_around(w: WorldModel, policy: Policy, rng: random.Random, blocked: set[Pos], state: str) -> StateOutcome:
    """No frontier in safe ground: step to a safe neighbour rather than stand idle."""
    options = [p for p in w.open_neighbours(w.pos, blocked) if not hostiles_near(w, p, policy)]
    if hurt(w):
        options = [p for p in options if is_safe_ish(w, p, policy)] or options
    if not options:
        return StateOutcome(None, "boxed in", state=state, wait=True)
    return StateOutcome([set_position(rng.choice(sorted(options)))], "look around: nothing left to explore", state=state)


def _missed_step(
    w: WorldModel,
    m: Memory,
    avoid: set[Pos],
    target_plan,
    knowledge: KnowledgeBase | None,
    goal: str,
    leg: nav_stuck.Leg,
    routed: bool,
) -> Pos | None:
    """No goal got a step. A goal on this map with no route at all is stuck at
    once; one whose route starts on a taken cell waits out its window (A15)."""
    att = nav_stuck.track(m, w, goal, leg)
    if att is None:
        return None
    if att.level == nav_stuck.REVEAL:
        return escalation_step(m, w, att, avoid, target_plan, None, knowledge)
    if routed:
        found = target_plan(att)
        if found and not next_step(w, avoid, found):
            m.path, m.goal = found, goal
            nav_stuck.observe(att, w, found)
    reason = nav_stuck.stuck_reason(att, w.tick) if routed else "no_path"
    return escalation_step(m, w, att, avoid, target_plan, reason, knowledge) if reason else None


def _escalated_step(
    w: WorldModel, m: Memory, avoid: set[Pos], target_plan, knowledge: KnowledgeBase | None
) -> Pos | None:
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
        return escalation_step(m, w, att, avoid, target_plan, None, knowledge)
    reason = nav_stuck.stuck_reason(att, w.tick)
    return escalation_step(m, w, att, avoid, target_plan, reason, knowledge) if reason else None


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
