"""Break: arm a capability and ``Use`` a nominated block (A28).

Module ``break_state`` (``break`` is reserved). The executor for the plan's
``break_block`` op; it also runs stuck escalation step 2 of the walk under
way (A15), and re-arms the weapon a break swapped out.

Either way a block is tried only with a capability known to open it: its
type's rule or a past break of that cell (``break_memory.known_openers``,
A103). A ``break_block`` op naming any other capability, or one that already
failed there, is dropped with the reason, so the planner hears why. An
oddity State lists may need any of the five, so a ``break_block`` on one
tries any capability held (A160).
"""

from __future__ import annotations

from collections.abc import Collection

from agentrealm_adk.break_memory import (
    BLOCK_OPENERS,
    BREAK_GOAL,
    BreakChoice,
    attempt_failed,
    break_step_cost,
    given_up_blocks,
    held_supply,
    known_openers,
    no_break_reason,
    nominate_on_path,
    pick_supply_for_capability,
)
from agentrealm_adk.navigation import cost_path
from agentrealm_adk.oddities import is_oddity
from agentrealm_adk.navigation import stuck as nav_stuck
from agentrealm_adk.pathing import HostileReach, enters_reach, grid_params, nav_search, next_step, plan_sets
from agentrealm_adk.reach import supply_reach
from agentrealm_adk.world import VOID, Pos, WorldModel, chebyshev

from ..plan import GoalOp
from .base import PlayContext, State, StateOutcome, my_op, op_walk
from .ask import ask_key, ask_the_planner, what_blocks
from .intents import arm, set_position, use_block

GOAL = BREAK_GOAL
GOAL_STATE = "Break"  # only Break's own arming counts against stuck step 2, not Heal's


def _stuck_choice(w: WorldModel, ctx: PlayContext) -> BreakChoice | None:
    att = nav_stuck.active(ctx.memory, w)
    if att is None or att.level != nav_stuck.BREAK:
        return None
    if att.break_x is not None and att.break_y is not None and att.break_cap:
        pos = (att.break_x, att.break_y)
        if not attempt_failed(ctx.knowledge, w.map_id, pos, att.break_cap):
            supply = pick_supply_for_capability(w, att.break_cap, ctx.knowledge)
            if supply is None:
                return None
            return BreakChoice(pos, att.break_cap, supply, break_step_cost(ctx.knowledge, supply.code))
        # That pair failed or was refused: nominate afresh, or escalate past step 2.
        nav_stuck.clear_break_target(att)
    if w.pos is None:
        return None
    choice = nominate_on_path(w, ctx.knowledge, w.pos, att.target, given_up_blocks(ctx.memory.nav_stuck, w.map_id, w.tick))
    if choice is not None:
        # Kept on the attempt: Break walks to it until it opens, or a give-up of it ends step 2.
        nav_stuck.set_break_target(att, choice.pos, choice.capability)
    return choice


def _op_refusal(w: WorldModel, ctx: PlayContext, op: GoalOp) -> str | None:
    """Why the ``break_block`` op cannot open its block, or None to go on.

    Its capability failed on that cell before, nothing held has it, the
    cell in view holds no block at all (walkable ground, where a hostile may
    stand, or no ground, A114), the block is of a type whose openers are
    known and that capability is not one of them (A103) and it is no oddity
    (an odd block may need any of the five, A160), or a walk to it was
    given up and is still backed off (A90). A block of a type with no known
    openers may be tried with any capability, so its opener can be learned.
    A cell not yet in view is walked to first.
    """
    pos = (op["x"], op["y"])
    cap = op["capability"]
    block = w.view.tiles.get(pos)
    if pos in given_up_blocks(ctx.memory.nav_stuck, w.map_id, w.tick):
        return f"no way to the {block or 'block'} at {pos[0]},{pos[1]}: the walk to it was given up"
    if attempt_failed(ctx.knowledge, w.map_id, pos, cap):
        return f"{cap} already failed on the {block or 'block'} at {pos[0]},{pos[1]}"
    if pick_supply_for_capability(w, cap, ctx.knowledge) is None:
        return f"nothing held can {cap}"
    if block is not None and (block == VOID or w.walkable(pos)):
        return f"the {block or 'empty cell'} at {pos[0]},{pos[1]} holds no block to break"
    openers = known_openers(w, ctx.knowledge, pos)
    if block in BLOCK_OPENERS and cap not in openers and not is_oddity(w, ctx.knowledge, pos):
        return f"{cap} is not known to open {block} (opened by {', '.join(sorted(openers))})"
    return None


def _plan_choice(w: WorldModel, ctx: PlayContext, op: GoalOp) -> BreakChoice | None:
    pos = (op["x"], op["y"])
    cap = op["capability"]
    supply = pick_supply_for_capability(w, cap, ctx.knowledge)
    if supply is None:
        return None
    return BreakChoice(pos, cap, supply, break_step_cost(ctx.knowledge, supply.code))


def break_outcome(
    w: WorldModel, ctx: PlayContext, state: str = "Break", reach: HostileReach | None = None
) -> StateOutcome:
    """One Break round: the plan's ``break_block`` op, else stuck step 2.
    ``reach``: the op walk's (``base.op_walk``), when the caller has it."""
    m = ctx.memory
    op = my_op(ctx, state)
    att = nav_stuck.active(m, w)
    if op is not None:
        refusal = _op_refusal(w, ctx, op)
        if refusal is not None and ctx.plan is not None:
            ctx.plan.drop_current(refusal, memory=m)
            return StateOutcome(None, f"break_block dropped: {refusal}", state=state)
        choice = _plan_choice(w, ctx, op)
    elif att is not None and att.level == nav_stuck.BREAK:
        if att.arm_decisions >= nav_stuck.ARM_DECISION_LIMIT:
            # Arming again and again opened nothing: on to step 3 (A15).
            nav_stuck.escalate(m, w, att, "arm_only")
            return StateOutcome(None, "arming made no progress", state=state)
        choice = _stuck_choice(w, ctx)
    else:
        choice = None
    if choice is None:
        att = nav_stuck.active(m, w)
        if att is not None and att.level == nav_stuck.BREAK:
            skip = given_up_blocks(m.nav_stuck, w.map_id, w.tick)
            why = no_break_reason(w, ctx.knowledge, w.pos, att.target, skip) if w.pos is not None else "no_break"
            nav_stuck.escalate(m, w, att, why)
        return StateOutcome(None, "nothing to break", state=state)
    return break_toward(w, ctx, choice, state, op_walk(w, ctx)[2] if reach is None else reach)


def in_break_reach(w: WorldModel, ctx: PlayContext, choice: BreakChoice) -> bool:
    """Whether ``choice``'s block is in reach of its supply's ``Use`` from where we stand."""
    return w.pos is not None and chebyshev(w.pos, choice.pos) <= supply_reach(ctx.knowledge, choice.supply.code)


def break_toward(
    w: WorldModel, ctx: PlayContext, choice: BreakChoice, state: str = "Break", reach: Collection[Pos] = ()
) -> StateOutcome:
    """Open ``choice``'s block: step toward it, then in reach arm its supply and ``Use`` it.

    Arming waits until the block is in reach. Only a decision's first intent
    is sent, so an ``Arm`` ahead of the step would leave the step unsent (A68).

    **Break** runs it, and so does **Heal** once the block its priced way
    crosses is in reach (A10, A89); Heal walks to it on its own ground.

    ``reach``: the ground an op's walk keeps out of (``base.op_walk``, A128),
    priced costly; a walk to the block that still enters it is no step.
    """
    m = ctx.memory
    _, plan_avoid, _ = plan_sets(w, m, ctx.policy, ctx.knowledge)
    here = w.pos
    if here is None or w.map_id is None:
        return StateOutcome(None, "position unknown", state=state)
    if in_break_reach(w, ctx, choice):
        intents: list[dict] = []
        if w.armed_code != choice.supply.code:
            if m.break_rearm is None and w.armed_code:
                m.break_rearm = w.armed_code
            if choice.supply.id >= 0:
                intents.append(arm(choice.supply.id))
                att = nav_stuck.active(m, w)
                if state == GOAL_STATE and att is not None and att.level == nav_stuck.BREAK:
                    att.arm_decisions += 1
        m.break_pending = (w.map_id, choice.pos, choice.capability)
        return StateOutcome(
            intents + [use_block(choice.pos)], f"break {choice.capability} @ {choice.pos}", state=state, progress=False
        )

    params = grid_params(
        ctx.policy,
        plan_avoid,
        set(reach),
        m=m,
        w=w,
        knowledge=ctx.knowledge,
        break_goal=choice.pos,
    )
    path = cost_path(w, choice.pos, params, nav=nav_search(m, w, GOAL, choice.pos))
    if enters_reach(w, path, reach):
        return StateOutcome(None, f"cannot reach {choice.pos} clear of the hostiles seen", state=state)
    step = next_step(w, plan_avoid, path)
    if step is None:
        att = nav_stuck.active(m, w)
        if att is not None and att.level == nav_stuck.BREAK:
            stuck_reason = nav_stuck.stuck_reason(att, w.tick)
            if stuck_reason:
                nav_stuck.escalate(m, w, att, stuck_reason)
        return StateOutcome(None, f"cannot reach {choice.pos}", state=state)
    m.path, m.goal = path or [], GOAL
    return StateOutcome([set_position(step)], f"break → {choice.pos}", state=state)


class BreakState(State):
    """Executor for ``break_block``. Also opens the block stuck escalation
    nominates at step 2 (A15), and re-arms the weapon swapped out for a break.
    With no way to its op's block it asks the planner and holds (A120)."""

    name = "Break"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        if ctx.policy.kind != "scripted" or not world.alive or world.pos is None:
            return False
        if my_op(ctx, self.name) is not None:
            return True
        att = nav_stuck.active(ctx.memory, world)
        if att is not None and att.level == nav_stuck.BREAK:
            return True
        # A break opened its block and the attempt went back to walking: the
        # weapon armed before it is still to be restored.
        m = ctx.memory
        return m.break_rearm is not None and m.break_pending is None

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not self.guard(world, ctx)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        op = my_op(ctx, self.name)
        reach = op_walk(world, ctx)[2] if op is not None else None  # once per decision (A128)
        out = break_outcome(world, ctx, self.name, reach)
        if op is not None and not out.intents and ctx.plan is not None and ctx.plan.current() is op:
            # No way to the block: ask the planner, saying what shuts it, and hold (A120).
            blocked = tried = None
            if ctx.memory.no_way_on != ask_key(world, out.reason, op):  # only for an ask that goes out
                blocked, tried = what_blocks(world, ctx, (op["x"], op["y"]), reach), "a way to the block, round every other"
            return ask_the_planner(world, ctx, out.reason, self.name, op, blocked_by=blocked, tried=tried)
        if out.intents or out.reason != "nothing to break":
            return out
        code, ctx.memory.break_rearm = ctx.memory.break_rearm, None
        if code is None or world.armed_code == code:
            return out
        supply = held_supply(world, code)
        if supply is not None:
            return StateOutcome([arm(supply.id)], f"re-arm {code}", state=self.name)
        return out
