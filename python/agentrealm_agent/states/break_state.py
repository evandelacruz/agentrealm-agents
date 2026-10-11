"""Break: arm a capability and ``Use`` a nominated block (A28).

Module ``break_state`` (``break`` is reserved). The executor for the plan's
``break_block`` op; it also runs stuck escalation step 2 of the walk under
way (A15), and re-arms the weapon a break swapped out.

Either way a block is tried only with a capability known to open it: its
type's rule or a past break of that cell (``break_memory.known_openers``,
A103). A ``break_block`` op naming any other capability, or one that already
failed there, is dropped with the reason, so the planner hears why.
"""

from __future__ import annotations

from ..break_memory import (
    BLOCK_OPENERS,
    BREAK_GOAL,
    BreakChoice,
    attempt_failed,
    break_step_cost,
    given_up_blocks,
    known_openers,
    no_break_reason,
    nominate_on_path,
    pick_supply_for_capability,
)
from ..navigation import cost_path
from ..navigation import stuck as nav_stuck
from ..pathing import grid_params, nav_search, next_step
from ..plan import GoalOp
from ..reach import supply_reach
from ..world import WorldModel, chebyshev
from .base import PlayContext, State, StateOutcome, my_op
from ..pathing import plan_sets
from .intents import arm, set_position, use_block
from .solve import held_supply

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
    block in view is one that capability is not known to open (A103), or a
    walk to it was given up and is still backed off (A90). A block not yet
    in view is walked to first.
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
    openers = known_openers(w, ctx.knowledge, pos)
    if block in BLOCK_OPENERS and cap not in openers:
        return f"{cap} is not known to open {block} (opened by {', '.join(sorted(openers))})"
    return None


def _plan_choice(w: WorldModel, ctx: PlayContext, op: GoalOp) -> BreakChoice | None:
    pos = (op["x"], op["y"])
    cap = op["capability"]
    supply = pick_supply_for_capability(w, cap, ctx.knowledge)
    if supply is None:
        return None
    return BreakChoice(pos, cap, supply, break_step_cost(ctx.knowledge, supply.code))


def break_outcome(w: WorldModel, ctx: PlayContext, state: str = "Break") -> StateOutcome:
    """One Break round: the plan's ``break_block`` op, else stuck step 2."""
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
    return break_toward(w, ctx, choice, state)


def break_toward(w: WorldModel, ctx: PlayContext, choice: BreakChoice, state: str = "Break") -> StateOutcome:
    """Open ``choice``'s block: step toward it, then in reach arm its supply and ``Use`` it.

    Arming waits until the block is in reach. Only a decision's first intent
    is sent, so an ``Arm`` ahead of the step would leave the step unsent (A68).

    **Break** runs it, and so does **Heal** for food behind a block the
    weapon opens (A10).
    """
    m = ctx.memory
    _, plan_avoid, _ = plan_sets(w, m, ctx.policy, ctx.knowledge)
    reach = supply_reach(ctx.knowledge, choice.supply.code)

    here = w.pos
    if here is None or w.map_id is None:
        return StateOutcome(None, "position unknown", state=state)
    if chebyshev(here, choice.pos) <= reach:
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
        set(),
        m=m,
        w=w,
        knowledge=ctx.knowledge,
        break_goal=choice.pos,
    )
    path = cost_path(w, choice.pos, params, nav=nav_search(m, w, GOAL, choice.pos))
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
    nominates at step 2 (A15), and re-arms the weapon swapped out for a break."""

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
        out = break_outcome(world, ctx, self.name)
        if out.intents or out.reason != "nothing to break":
            return out
        code, ctx.memory.break_rearm = ctx.memory.break_rearm, None
        if code is None or world.armed_code == code:
            return out
        supply = held_supply(world, code)
        if supply is not None:
            return StateOutcome([arm(supply.id)], f"re-arm {code}", state=self.name)
        return out
