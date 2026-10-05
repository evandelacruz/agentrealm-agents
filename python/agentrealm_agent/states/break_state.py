"""Break: open blocks from the plan or stuck escalation step 2 (A28)."""

from __future__ import annotations

from ..break_memory import (
    BreakChoice,
    attempt_failed,
    break_step_cost,
    nominate_on_path,
    pick_supply_for_capability,
)
from ..memory import Memory
from ..navigation import cost_path
from ..navigation import stuck as nav_stuck
from ..pathing import grid_params, nav_search, next_step
from ..plan import GoalOp, Plan
from ..world import Pos, WorldModel, chebyshev
from .base import PlayContext, State, StateOutcome
from .explore import plan_sets, reflex_outcome
from .intents import arm, set_position, use_block
from .solve import held_supply, use_reach

GOAL = "break"


def break_op(plan: Plan | None) -> GoalOp | None:
    if plan is None:
        return None
    op = plan.current()
    if op is not None and op["op"] == "break_block":
        return op
    return None


def _stuck_choice(w: WorldModel, ctx: PlayContext) -> BreakChoice | None:
    att = nav_stuck.active(ctx.memory, w)
    if att is None or att.level != nav_stuck.BREAK:
        return None
    if att.break_x is not None and att.break_y is not None and att.break_cap:
        pos = (att.break_x, att.break_y)
        if attempt_failed(ctx.knowledge, w.map_id, pos, att.break_cap):
            # That pair failed or was refused: nominate afresh, or escalate past step 2.
            nav_stuck.clear_break_target(att)
            return nominate_on_path(w, ctx.knowledge, w.pos, att.target) if w.pos is not None else None
        supply = pick_supply_for_capability(w, att.break_cap)
        if supply is None:
            return None
        return BreakChoice(pos, att.break_cap, supply, break_step_cost(ctx.knowledge, supply.code))
    if w.pos is None:
        return None
    return nominate_on_path(w, ctx.knowledge, w.pos, att.target)


def _plan_choice(w: WorldModel, ctx: PlayContext, op: GoalOp) -> BreakChoice | None:
    pos = (op["x"], op["y"])
    cap = op["capability"]
    if attempt_failed(ctx.knowledge, w.map_id, pos, cap):
        return None
    supply = pick_supply_for_capability(w, cap)
    if supply is None:
        return None
    return BreakChoice(pos, cap, supply, break_step_cost(ctx.knowledge, supply.code))


def break_outcome(
    w: WorldModel,
    m: Memory,
    plan: Plan | None,
    *,
    never_attack: list[str],
    ctx: PlayContext,
    state: str = "Break",
) -> StateOutcome:
    _, plan_avoid, _ = plan_sets(w, m, ctx.policy, ctx.knowledge)
    reflex = reflex_outcome(
        w, ctx.policy, never_attack=never_attack, state=state, knowledge=ctx.knowledge
    )
    if reflex is not None:
        if m.goal == GOAL:
            m.path, m.goal = [], ""
        return reflex

    op = break_op(plan)
    choice = _plan_choice(w, ctx, op) if op is not None else _stuck_choice(w, ctx)
    if choice is None:
        att = nav_stuck.active(m, w)
        if att is not None and att.level == nav_stuck.BREAK:
            nav_stuck.escalate(m, w, att, "no_break")
        return StateOutcome(None, "nothing to break", state=state)

    reach = use_reach(ctx.knowledge, choice.supply.code)
    intents: list[dict] = []
    if w.armed_code != choice.supply.code:
        if m.break_rearm is None and w.armed_code:
            m.break_rearm = w.armed_code
        if choice.supply.id >= 0:
            intents.append(arm(choice.supply.id))

    here = w.pos
    if here is None or w.map_id is None:
        return StateOutcome(None, "position unknown", state=state)
    if chebyshev(here, choice.pos) <= reach:
        m.break_pending = (w.map_id, choice.pos, choice.capability)
        return StateOutcome(intents + [use_block(choice.pos)], f"break {choice.capability} @ {choice.pos}", state=state)

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
    return StateOutcome(intents + [set_position(step)], f"break → {choice.pos}", state=state)


class BreakState(State):
    name = "Break"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        if ctx.policy.kind != "scripted" or not world.alive or world.pos is None:
            return False
        if break_op(ctx.plan) is not None:
            return True
        att = nav_stuck.active(ctx.memory, world)
        return att is not None and att.level == nav_stuck.BREAK

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not self.guard(world, ctx)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        out = break_outcome(
            world,
            ctx.memory,
            ctx.plan,
            never_attack=ctx.never_attack,
            ctx=ctx,
            state=self.name,
        )
        if out.intents or out.reason != "nothing to break":
            return out
        code, ctx.memory.break_rearm = ctx.memory.break_rearm, None
        if code is None or world.armed_code == code:
            return out
        supply = held_supply(world, code)
        if supply is not None:
            return StateOutcome([arm(supply.id)], f"re-arm {code}", state=self.name)
        return out
