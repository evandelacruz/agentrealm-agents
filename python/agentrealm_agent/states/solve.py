"""Solve: plan ``compose`` and ``use_block`` ops (A39)."""

from __future__ import annotations

from ..config import Policy
from ..fragments import compose_supply_ids, fragment_set_complete, holds_whole
from ..item_table import InventorySupply
from ..knowledge_base import KnowledgeBase
from ..memory import Memory
from ..navigation import cost_path
from ..pathing import grid_params, nav_search, next_step
from ..plan import PLAN_STALL_SECONDS, SOLVE_OPS, GoalOp, Plan
from ..world import Pos, WorldModel, chebyshev
from .base import PlayContext, State, StateOutcome
from .explore import plan_sets, reflex_outcome
from .intents import arm, compose, set_position, use_block

GOAL = "solve"
DEFAULT_USE_REACH = 1


def solve_op(plan: Plan | None) -> GoalOp | None:
    if plan is None:
        return None
    op = plan.current()
    if op is not None and op["op"] in SOLVE_OPS:
        return op
    return None


def use_reach(knowledge: KnowledgeBase | None, code: str) -> int:
    if knowledge is not None:
        row = knowledge.items.get(code)
        if isinstance(row, dict):
            reach = row.get("attack_range")
            if isinstance(reach, int) and not isinstance(reach, bool) and reach > 0:
                return reach
    return DEFAULT_USE_REACH


def held_supply(w: WorldModel, code: str) -> InventorySupply | None:
    """A supply of ``code`` in hand. One only in the carried chest does not count:
    Solve does not withdraw it, so the op stalls and is dropped (A39)."""
    for s in w.held_supplies:
        if s.code == code:
            return s
    return None


def solve_outcome(
    w: WorldModel,
    m: Memory,
    policy: Policy,
    plan: Plan,
    *,
    never_attack: list[str],
    knowledge: KnowledgeBase | None = None,
    state: str = "Solve",
) -> StateOutcome:
    plan.advance(w, m)
    op = plan.current()
    if op is None or op["op"] not in SOLVE_OPS:
        return _rearm_outcome(w, m, state)

    _, plan_avoid, plan_costly = plan_sets(w, m, policy, knowledge)
    reflex = reflex_outcome(w, policy, never_attack=never_attack, state=state, knowledge=knowledge)
    if reflex is not None:
        if m.goal == GOAL:
            m.path, m.goal = [], ""
        return reflex

    if op["op"] == "compose":
        out = _compose_outcome(w, op, state)
    else:
        out = _use_block_outcome(w, m, policy, op, plan_avoid, plan_costly, knowledge, state)
    return _track_progress(plan, w, m, op, out, state)


def _track_progress(
    plan: Plan, w: WorldModel, m: Memory, op: GoalOp, out: StateOutcome, state: str
) -> StateOutcome:
    """Drop the op once it has made no progress for ``PLAN_STALL_SECONDS`` (A34).

    A step toward the target is progress. Sending nothing (pieces or supply
    missing, target unreachable) is not, and neither is a `Compose`, `Arm` or
    `Use` that has not finished the op, so a rejected or ineffective try
    cannot pin the stack either. While the op sends nothing, dispatch falls
    through to the states below (A44).
    """
    if out.intents and any(i.get("verb") == "SetPosition" for i in out.intents):
        plan.stalled_since_tick = None
        return out
    if not plan.note_stalled(w.tick):
        return out
    plan.drop_current(f"{out.reason}; no progress for {PLAN_STALL_SECONDS}s", memory=m)
    if m.goal == GOAL:
        m.path, m.goal = [], ""
    return StateOutcome(None, f"dropped {op['op']}: {out.reason}", state=state)


def _rearm_outcome(w: WorldModel, m: Memory, state: str) -> StateOutcome:
    """Arm what was armed before a ``use_block`` swapped it out, once (A39).

    Sent a single time: a rejected `Arm` (or a weapon no longer in hand) is not
    retried, so it cannot hold the round.
    """
    code, m.solve_rearm = m.solve_rearm, None
    if code is None or w.armed_code == code:
        return StateOutcome(None, "no solve op", state=state)
    supply = held_supply(w, code)
    if supply is None:
        return StateOutcome(None, f"cannot re-arm {code}: not in hand", state=state)
    return StateOutcome([arm(supply.id)], f"re-arm {code}", state=state)


def _compose_outcome(w: WorldModel, op: GoalOp, state: str) -> StateOutcome:
    whole = op["composes_into"]
    if holds_whole(w.held_supplies, whole):
        return StateOutcome(None, f"already have {whole}", state=state)
    if not fragment_set_complete(w.held_supplies, whole):
        return StateOutcome(None, f"fragments missing for {whole}", state=state)
    ids = compose_supply_ids(w.held_supplies, whole)
    return StateOutcome([compose(ids)], f"compose {whole}", state=state)


def _use_block_outcome(
    w: WorldModel,
    m: Memory,
    policy: Policy,
    op: GoalOp,
    blocked: set[Pos],
    costly: set[Pos],
    knowledge: KnowledgeBase | None,
    state: str,
) -> StateOutcome:
    here = w.pos
    if here is None:
        return StateOutcome(None, "position unknown", state=state)
    target = (op["x"], op["y"])
    code = op["code"]
    supply = held_supply(w, code)
    if supply is None:
        return StateOutcome(None, f"missing {code}", state=state)

    reach = use_reach(knowledge, code)
    intents: list[dict] = []
    if w.armed_code != code:
        if m.solve_rearm is None and w.armed_code is not None:
            m.solve_rearm = w.armed_code
        intents.append(arm(supply.id))

    if chebyshev(here, target) <= reach:
        return StateOutcome(intents + [use_block(target)], f"use {code} @ {target}", state=state)

    step = _step_to_use(w, m, policy, target, reach, blocked, costly)
    if step is None:
        return StateOutcome(None, f"cannot reach {target}", state=state)
    return StateOutcome(intents + [set_position(step)], f"solve → {target}", state=state)


def _step_to_use(
    w: WorldModel,
    m: Memory,
    policy: Policy,
    target: Pos,
    reach: int,
    blocked: set[Pos],
    costly: set[Pos],
) -> Pos | None:
    """Next step toward a cell from which ``target`` is within ``reach``."""
    stands = _use_stands(w, target, reach, blocked)
    if not stands:
        return None
    if m.goal == GOAL and m.path and m.path[-1] in stands:
        step = next_step(w, blocked, m.path)
        if step is not None:
            return step
    params = grid_params(policy, blocked, costly)
    best: tuple[list[Pos], Pos] | None = None
    for stand in sorted(stands, key=lambda p: (chebyshev(w.pos, p), p)):
        path = cost_path(w, stand, params, nav=nav_search(m, w, GOAL, stand))
        if not next_step(w, blocked, path):
            continue
        if best is None or len(path) < len(best[0]):
            best = (path, stand)
    if best is None:
        m.path, m.goal = [], ""
        return None
    m.path, m.goal = best[0], GOAL
    return next_step(w, blocked, m.path)


def _use_stands(w: WorldModel, target: Pos, reach: int, blocked: set[Pos]) -> set[Pos]:
    out: set[Pos] = set()
    for p, _block in w.view.tiles.items():
        if chebyshev(p, target) > reach:
            continue
        if p in blocked:
            continue
        if w.view.walkable(p) and p not in w.occupied():
            out.add(p)
    if w.pos is not None and chebyshev(w.pos, target) <= reach and w.pos not in blocked:
        out.add(w.pos)
    return out


class SolveState(State):
    """Priority 4: compose fragments and use keys or tools at blocks from the plan,
    then re-arm what a ``use_block`` swapped out."""

    name = "Solve"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        return (
            ctx.policy.kind == "scripted"
            and world.alive
            and world.pos is not None
            and (solve_op(ctx.plan) is not None or ctx.memory.solve_rearm is not None)
        )

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not self.guard(world, ctx)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        plan = ctx.plan
        if plan is None:
            return StateOutcome(None, "no plan", state=self.name)
        return solve_outcome(
            world,
            ctx.memory,
            ctx.policy,
            plan,
            never_attack=ctx.never_attack,
            knowledge=ctx.knowledge,
            state=self.name,
        )
