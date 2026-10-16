"""Explore: walk the plan's ``explore_area`` op, or the safe default (M3, A12, A34).

The executor for ``explore_area``, and the safe default when there is no
plan op to carry out (PLAN.md **Architecture: AI plans, state machine
executes**). The safe default explores safe ground (``safe_explore_path``):
off hazards, away from hostiles, and, when hurt, inside safe zones first so
the character heals while it looks around. Once that ground is all explored
or out of reach, it pushes the boundary: the nearest frontier outside, still
off hazards and away from hostiles, with the survival reflexes above it.
With no reachable frontier at all, the ground in reach is all seen and the
way on is the planner's call (which block to open, which hostile to fight or
pass, which clue to follow): it never steps in place and never parks beside
a hostile; it raises a ``no_way_on`` trigger and sends nothing (A114).
It never stands in for an executor: under any op but ``explore_area`` that
op's executor keeps its walk, clears the way or asks the planner itself, and
the safe default holds (A120).
Stuck detection escalates and gives up a walk that goes nowhere (A15); cells Step
rejections ruled out and ``avoid_blocks`` hazards stay out of the path (A14).
"""

from __future__ import annotations

import random

from agentrealm_adk.config import Policy
from agentrealm_adk.healing import hurt
from agentrealm_adk.knowledge_base import KnowledgeBase
from agentrealm_adk.memory import Memory
from agentrealm_adk.navigation import stuck as nav_stuck
from agentrealm_adk.pathing import (
    plan_sets,
    attempt_plan,
    clue_redirects,
    escalation_step,
    next_step,
    path_owned_by,
    replan,
)
from agentrealm_adk.navigation.planner import HOSTILE_DANGER_RADIUS
from agentrealm_adk.survival import approaching, is_hostile
from agentrealm_adk.world import Pos, WorldModel, chebyshev
from agentrealm_adk.hostile_ground import is_safe_ish

from ..plan import OP_STATE, GoalOp
from .ask import ask_the_planner, op_key
from .base import PlayContext, State, StateOutcome, my_op, top_executor, top_op
from .intents import set_position
from .way import break_due

# Hurt, the safe default keeps away from a known hostile this close: the
# planner's danger radius (``navigation.planner``).
CAUTION_RADIUS = HOSTILE_DANGER_RADIUS
# How long it holds with no step away before it explores again (10 s at 10 ticks/s).
KEEP_AWAY_HOLD_TICKS = 100
# Why the safe default had no step: the ground in reach is all seen.
NOTHING_TO_EXPLORE = "nothing left to explore"
# The executors whose op the safe default never stands in for (A120): every
# one but Explore's own. Each keeps its own walk, clears the way, and with no
# way on asks the planner and holds (``way.clear_the_way``).
HOLD_THEIR_WALK = frozenset(s for s in OP_STATE.values() if s is not None) - {"Explore"}


class ExploreState(State):
    """Last state for a scripted character: the executor for ``explore_area``,
    and with no plan op the safe default. Under any other op it holds,
    sending nothing, whatever that op's executor did this decision
    (``HOLD_THEIR_WALK``): it never walks the character off that op's way,
    and that op's stall clock runs (A34, A120)."""

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
        owner = top_executor(ctx)
        if owner in HOLD_THEIR_WALK:
            return hold_for(world, ctx, owner)
        out = safe_default(world, ctx)
        out.progress = False  # it moves for no op: the top op, if any, still stalls
        return out


def hold_for(world: WorldModel, ctx: PlayContext, owner: str) -> StateOutcome:
    """Hold for the op ``owner`` executes, which sent nothing this decision (A120).

    Every op whose executor cannot act asks the planner once: an op asked
    for from this cell already (``Memory.no_way_on``, whatever its reason;
    an earlier op's ask does not count), or whose executor left
    this decision to Break at stuck step 2 of its own walk
    (``Memory.break_yield``), is left to it; any other is asked for here, so
    no op waits on its stall clock unheard.
    """
    m, op = ctx.memory, top_op(ctx)
    asked = m.no_way_on is not None and op is not None and m.no_way_on[:2] == (world.map_id, world.pos)
    asked = asked and m.no_way_on[3] == op_key(op)
    if op is None or asked or m.break_yield == m.nav_stuck.decision or break_due(world, m, owner):
        return StateOutcome(None, f"{owner} holds its walk", state=ExploreState.name, wait=True, progress=False)
    return ask_the_planner(world, ctx, f"{op['op']}: {owner} has no move", ExploreState.name, op)


def safe_default(world: WorldModel, ctx: PlayContext) -> StateOutcome:
    """The safe default: explore safe ground, then push past it.

    Hurt with a known hostile near, it explores nothing: it steps away from
    the hostile, or holds when no step gets further (``keep_away``). With
    nothing left to explore it asks the planner (``ask_the_planner``)."""
    if not hurt(world):
        ctx.memory.keep_away_hold = None  # healed: a later hold starts afresh
    elif (out := keep_away(world, ctx)) is not None:
        return out
    out = explore_outcome(world, ctx.memory, ctx.policy, ctx.rng, knowledge=ctx.knowledge, op=None)
    if out.intents:
        ctx.memory.no_way_on = None  # ground to explore again: a later ask is new
    if out.reason != NOTHING_TO_EXPLORE:
        return out
    return ask_the_planner(world, ctx, NOTHING_TO_EXPLORE, "Explore")


def keep_away(w: WorldModel, ctx: PlayContext, state: str = "Explore") -> StateOutcome | None:
    """One step that takes us further from the known hostiles within
    ``CAUTION_RADIUS``, safe-ish ground first, else hold while one of them
    is closing in (``survival.approaching``); None with none near, or with
    no step away from a hostile that is not closing.

    Free-play run 6: the character started hurt beside a hostile's post, and
    before the planner's first reply the safe default walked ten steps
    toward it and died. The reflexes (Retreat, Flee, Fight, Heal) still act
    above this; it covers the ground between them, a hostile near but not in
    range. A hold lasts at most ``KEEP_AWAY_HOLD_TICKS``, then the safe
    default explores again, still away from hostiles, so a hostile that
    stays put never holds it for good. One that is not closing holds
    nothing: standing off it is idle (A82, free-play run 8: Gather, blocked
    by a hostile 10–13 cells off, fell back here and stood off it for 177 s).
    """
    m = ctx.memory
    near = [e for e in w.entities if is_hostile(w, ctx.policy, e) and chebyshev(e.pos, w.pos) <= CAUTION_RADIUS]
    if not near:
        m.keep_away_hold = None
        return None

    def gap(p: Pos) -> int:
        return min(chebyshev(e.pos, p) for e in near)

    blocked, _, _ = plan_sets(w, ctx.memory, ctx.policy, ctx.knowledge)
    options = [p for p in w.open_neighbours(w.pos, blocked) if gap(p) > gap(w.pos)]
    if not options:
        if not any(approaching(w, e) for e in near):
            return None  # the hold's start stays: closing in again does not restart the cap
        if m.keep_away_hold is None:
            m.keep_away_hold = w.tick
        if w.tick - m.keep_away_hold >= KEEP_AWAY_HOLD_TICKS:
            return None
        m.path = []
        return StateOutcome(None, "hurt, hostile near: hold", state=state, wait=True)
    m.keep_away_hold = None
    m.path = []  # one step at a time: no walk queue toward anything
    step = min(options, key=lambda p: (not is_safe_ish(w, p, ctx.policy), -gap(p), p))
    return StateOutcome([set_position(step)], "hurt, hostile near: step away", state=state)


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

    With no step, sends nothing. With no op and no frontier to walk to at
    all, its reason is ``NOTHING_TO_EXPLORE``, and the safe default asks the
    planner (``ask_the_planner``); a frontier whose first step is only taken
    for now, or that stuck detection is still escalating, is not that.
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
            nav_stuck.observe(att, w, m.path, nav_stuck.corridor_for(m, w, att))
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
    if op is None and missed is None:
        # No frontier the safe default may walk to at all, not one whose step is only taken now.
        return StateOutcome(None, NOTHING_TO_EXPLORE, state=state)
    if op is None:
        # A frontier is there, its step only taken for now or its walk escalating: hold, never step in place.
        return StateOutcome(None, "no explore step yet", state=state, wait=True)
    return StateOutcome(None, "no explore_area step", state=state)


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
            nav_stuck.observe(att, w, found, nav_stuck.corridor_for(m, w, att))
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
