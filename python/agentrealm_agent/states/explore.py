"""Explore: walk the plan's ``explore_area`` op, or the safe default (M3, A12, A34).

The executor for ``explore_area``, and the safe default when there is no
plan op to carry out (PLAN.md **Architecture: AI plans, state machine
executes**). The safe default explores safe ground (``safe_explore_path``):
off hazards, away from hostiles, and, when hurt, inside safe zones first so
the character heals while it looks around. Once that ground is all explored
or out of reach, it pushes the boundary: the nearest frontier outside, still
off hazards and away from hostiles, with the survival reflexes above it.
With no reachable frontier at all, the ground in reach is all seen, so it
opens a way on: the cheapest block on the edge of that ground that a held
item is known to open (``openings``, A114), walked to and broken as Break
breaks any block. With nothing to open either, it never steps in place: it
raises a ``no_way_on`` trigger and waits for the planner. Stuck detection
escalates and gives up a walk that goes nowhere (A15); cells Step
rejections ruled out and ``avoid_blocks`` hazards stay out of the path (A14).
"""

from __future__ import annotations

import random

from ..config import Policy
from ..healing import hurt
from ..break_memory import given_up_blocks
from ..knowledge_base import KnowledgeBase
from ..memory import Memory, queue_signal
from ..navigation import stuck as nav_stuck
from ..openings import cheapest_opening
from ..pathing import (
    attempt_plan,
    clue_redirects,
    escalation_step,
    grid_params,
    next_step,
    path_owned_by,
    plan_sets,
    replan,
)
from ..plan import GoalOp
from ..navigation.planner import HOSTILE_DANGER_RADIUS
from ..survival import approaching, is_hostile
from ..world import Pos, WorldModel, chebyshev
from .base import PlayContext, State, StateOutcome, my_op
from .break_state import break_toward
from .gather_safe import is_safe_ish
from .intents import set_position

# Hurt, the safe default keeps away from a known hostile this close: the
# planner's danger radius (``navigation.planner``).
CAUTION_RADIUS = HOSTILE_DANGER_RADIUS
# How long it holds with no step away before it explores again (10 s at 10 ticks/s).
KEEP_AWAY_HOLD_TICKS = 100
# Why the safe default had no step: the ground in reach is all seen.
NOTHING_TO_EXPLORE = "nothing left to explore"


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
    """The safe default: explore safe ground, then push past it, then open a way on.

    Hurt with a known hostile near, it explores nothing: it steps away from
    the hostile, or holds when no step gets further (``keep_away``). With
    nothing left to explore it opens a way, or asks the planner
    (``open_a_way``)."""
    if not hurt(world):
        ctx.memory.keep_away_hold = None  # healed: a later hold starts afresh
    elif (out := keep_away(world, ctx)) is not None:
        return out
    out = explore_outcome(world, ctx.memory, ctx.policy, ctx.rng, knowledge=ctx.knowledge, op=None)
    if out.intents:
        ctx.memory.opening = ctx.memory.no_way_on = None  # ground to explore again: a later ask is new
        return out
    return open_a_way(world, ctx)


def open_a_way(w: WorldModel, ctx: PlayContext, state: str = "Explore") -> StateOutcome:
    """The ground in reach is all seen: open the cheapest way on, or ask the planner (A114).

    The way on is a block on the edge of the reached ground with something
    past it that a held item is known to open, by the walk to it plus the
    break (``openings.cheapest_opening``); one picked is kept until it is
    opened, fails or its walk is given up. It is broken as Break breaks any
    block (``break_toward``). With none, it never steps in place: it raises
    one ``no_way_on`` trigger for this cell, and waits for the planner,
    who weighs the clues and State ``ways_to_open``.
    """
    m = ctx.memory
    if w.pos is None or w.map_id is None:
        return StateOutcome(None, "position unknown", state=state)
    _, plan_avoid, plan_costly = plan_sets(w, m, ctx.policy, ctx.knowledge)
    priced = grid_params(ctx.policy, plan_avoid, plan_costly, m=m, w=w, knowledge=ctx.knowledge).priced
    skip = given_up_blocks(m.nav_stuck, w.map_id, w.tick)
    kept = m.opening[1] if m.opening is not None and m.opening[0] == w.map_id else None
    found = cheapest_opening(w, ctx.knowledge, plan_avoid, skip, priced, kept)
    if found is not None and found.choice is not None:
        out = break_toward(w, ctx, found.choice, state)
        if out.intents:
            m.opening, m.no_way_on = (w.map_id, found.pos), None
            out.reason = f"{NOTHING_TO_EXPLORE}: open the {found.block} at {found.pos}: {out.reason}"
            return out
    m.opening = None
    here = (w.map_id, w.pos)
    if m.no_way_on != here:
        m.no_way_on = here
        why = f"{NOTHING_TO_EXPLORE}, and nothing held is known to open a block on the edge of the ground in reach"
        queue_signal(m, {"trigger": "no_way_on", "map_id": w.map_id, "x": w.pos[0], "y": w.pos[1], "tick": w.tick, "why": why})
    return StateOutcome(None, f"{NOTHING_TO_EXPLORE}, nothing to open: asked the planner", state=state, wait=True)


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

    With no step, sends nothing: the safe default then opens a way on
    (``open_a_way``).
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
    return StateOutcome(None, NOTHING_TO_EXPLORE, state=state)


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
