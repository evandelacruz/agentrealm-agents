"""Solve: plan ``compose`` and ``use_block`` ops (A39)."""

from __future__ import annotations

from collections.abc import Collection

from ..break_memory import held_supply
from ..config import Policy
from ..fragments import compose_supply_ids, fragment_set_complete, holds_whole
from ..knowledge_base import KnowledgeBase
from ..memory import Memory
from ..navigation import cost_path
from ..pathing import enters_reach, grid_params, nav_search, next_step, plan_sets
from ..plan import GoalOp
from ..reach import supply_reach
from ..world import Pos, WorldModel, chebyshev
from .base import PlayContext, State, StateOutcome, my_op, op_walk
from .intents import arm, compose, set_position, use_block
from .way import clear_the_way

GOAL = "solve"


def solve_outcome(
    w: WorldModel,
    m: Memory,
    policy: Policy,
    op: GoalOp | None,
    *,
    knowledge: KnowledgeBase | None = None,
    state: str = "Solve",
    hostile_reach: Collection[Pos] = (),
) -> StateOutcome:
    """One Solve round for the top ``compose`` or ``use_block`` op, else the re-arm.

    A ``Compose``, ``Arm`` or ``Use`` is a try that has not finished the op
    (``progress=False``), so a rejected or ineffective one cannot pin the
    stack: only a step toward the target resets its stall clock (A34).
    The walk to a cell to use from keeps out of ``hostile_reach`` (A128).
    """
    if op is None:
        return _rearm_outcome(w, m, state)
    _, plan_avoid, plan_costly = plan_sets(w, m, policy, knowledge)
    if op["op"] == "compose":
        out = _compose_outcome(w, op, state)
    else:
        out = _use_block_outcome(w, m, policy, op, plan_avoid, plan_costly | set(hostile_reach), knowledge, state, hostile_reach)
    out.progress = bool(out.intents) and any(i.get("verb") == "SetPosition" for i in out.intents)
    return out


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
    hostile_reach: Collection[Pos] = (),
) -> StateOutcome:
    here = w.pos
    if here is None:
        return StateOutcome(None, "position unknown", state=state)
    target = (op["x"], op["y"])
    code = op["code"]
    supply = held_supply(w, code)
    if supply is None:
        return StateOutcome(None, f"missing {code}", state=state)

    reach = supply_reach(knowledge, code)
    intents: list[dict] = []
    if w.armed_code != code:
        if m.solve_rearm is None and w.armed_code is not None:
            m.solve_rearm = w.armed_code
        intents.append(arm(supply.id))

    if chebyshev(here, target) <= reach:
        return StateOutcome(intents + [use_block(target)], f"use {code} @ {target}", state=state)

    step = _step_to_use(w, m, policy, target, reach, blocked, costly, hostile_reach)
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
    hostile_reach: Collection[Pos] = (),
) -> Pos | None:
    """Next step toward a cell from which ``target`` is within ``reach``, on
    a way that keeps out of ``hostile_reach`` (A128)."""
    stands = _use_stands(w, target, reach, blocked)
    if not stands:
        return None
    if m.goal == GOAL and m.path and m.path[-1] in stands and not enters_reach(w, m.path, hostile_reach):
        step = next_step(w, blocked, m.path)
        if step is not None:
            return step
    params = grid_params(policy, blocked, costly)
    best: tuple[list[Pos], Pos] | None = None
    for stand in sorted(stands, key=lambda p: (chebyshev(w.pos, p), p)):
        path = cost_path(w, stand, params, nav=nav_search(m, w, GOAL, stand))
        if not next_step(w, blocked, path) or enters_reach(w, path, hostile_reach):
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
    """Executor for ``compose`` and ``use_block``; then re-arms what a
    ``use_block`` swapped out. With no stand in reach of its target it opens
    the block on the way a held item opens; with no way, or nothing to try
    (the item or a fragment missing), it asks the planner and holds (A120)."""

    name = "Solve"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        return (
            ctx.policy.kind == "scripted"
            and world.alive
            and world.pos is not None
            and (my_op(ctx, self.name) is not None or ctx.memory.solve_rearm is not None)
        )

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not self.guard(world, ctx)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        op = my_op(ctx, self.name)
        reach = op_walk(world, ctx)[2] if op is not None else ()
        out = solve_outcome(world, ctx.memory, ctx.policy, op, knowledge=ctx.knowledge, state=self.name, hostile_reach=reach)
        if op is None or out.intents or out.wait or ctx.plan is None or ctx.plan.current() is not op:
            return out
        # No try and no step: clear the way to the target, else ask the planner and hold (A120).
        walks = op["op"] == "use_block" and held_supply(world, op["code"]) is not None
        at = (op["x"], op["y"]) if walks else None
        out = clear_the_way(world, ctx, at, out.reason, self.name, op)
        out.progress = bool(out.intents) and any(i.get("verb") == "SetPosition" for i in out.intents)
        return out
