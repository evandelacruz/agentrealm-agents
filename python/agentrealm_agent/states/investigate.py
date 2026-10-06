"""Investigate: carry out the plan's ``read`` and ``say`` ops (A30, A56).

A ``read`` names a readable cell (``x``, ``y``) or a supply (``supply_id``);
a ``say`` names an NPC by ``npc_id`` or ``npc_type``. Investigate walks into
reach and sends the ``Read`` or ``Say``, and finishes the op once the
knowledge base records it, or once the server has refused it
``MAX_REJECTIONS`` times.
"""

from __future__ import annotations

from ..investigation import (
    MAX_REJECTIONS,
    SPEECH_RANGE,
    cell_was_read,
    in_sight,
    read_key,
    read_supply_key,
    say_key,
    spoken_npc_ids,
)
from ..navigation import cost_path, nearest_target
from ..pathing import bounded_step, grid_params, nav_search
from ..plan import GoalOp
from ..scroll_investigation import supply_was_read
from ..world import Entity, Pos, WorldModel, chebyshev
from .base import PlayContext, State, StateOutcome, my_op
from .explore import plan_sets
from .intents import read_block, read_supply, say_to, set_position

GOAL = "investigate"


class InvestigateState(State):
    """Executor for ``read`` and ``say``."""

    name = "Investigate"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        if ctx.policy.kind != "scripted" or not world.alive or world.pos is None:
            return False
        return my_op(ctx, self.name) is not None

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not self.guard(world, ctx)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        op = my_op(ctx, self.name)
        assert op is not None
        return _read(world, ctx, op) if op["op"] == "read" else _say(world, ctx, op)


def _read(w: WorldModel, ctx: PlayContext, op: GoalOp) -> StateOutcome:
    kb = ctx.knowledge
    if "supply_id" in op:
        sid = op["supply_id"]
        if _settled(ctx, read_supply_key(sid), supply_was_read(kb, sid)):
            return _out(None, f"read supply {sid}: done")
        return _out([read_supply(sid)], f"read supply {sid}")
    pos = (op["x"], op["y"])
    assert w.map_id is not None and w.pos is not None
    if _settled(ctx, read_key(w.map_id, pos), cell_was_read(kb, w.map_id, pos)):
        return _out(None, f"read {pos}: done")
    if in_sight(w, w.map_id, w.pos, pos):
        return _out([read_block(pos)], f"read sign @{pos[0]},{pos[1]}")
    # A sign is usually a wall cell: walk to the nearest cell it is in sight from.
    map_id = w.map_id
    stands = {p for p in w.view.tiles if w.view.walkable(p) and in_sight(w, map_id, p, pos)} - w.occupied()
    _, plan_avoid, plan_costly = plan_sets(w, ctx.memory, ctx.policy, ctx.knowledge)
    found = nearest_target(w, stands - plan_avoid, grid_params(ctx.policy, plan_avoid, plan_costly))
    if found is None:
        return _out(None, f"read {pos}: no path")
    return _walk(w, ctx, found[0], f"read {pos}")


def _say(w: WorldModel, ctx: PlayContext, op: GoalOp) -> StateOutcome:
    npc = _npc(w, op)
    if npc is None:
        return _out(None, "no such NPC in sight")
    if _settled(ctx, say_key(npc.id), npc.id in spoken_npc_ids(ctx.knowledge)):
        return _out(None, f"said to npc {npc.id}: done")
    assert w.pos is not None
    if chebyshev(w.pos, npc.pos) <= SPEECH_RANGE:
        return _out([say_to(npc, op["text"])], f"say to npc {npc.id}")
    return _walk(w, ctx, npc.pos, f"say to npc {npc.id}")


def _npc(w: WorldModel, op: GoalOp) -> Entity | None:
    npcs = [e for e in w.entities if e.kind == "npc"]
    if "npc_id" in op:
        npcs = [e for e in npcs if e.id == op["npc_id"]]
    else:
        npcs = [e for e in npcs if e.code == op["npc_type"]]
    here = w.pos
    return min(npcs, key=lambda e: (chebyshev(e.pos, here), e.id)) if npcs and here is not None else None


def _settled(ctx: PlayContext, key: str, recorded: bool) -> bool:
    """The op is finished: the knowledge base records it, or the server refused it too often."""
    if not recorded and ctx.memory.investigate_rejections.get(key, 0) < MAX_REJECTIONS:
        return False
    assert ctx.plan is not None
    ctx.plan.finish_current("recorded" if recorded else "refused", memory=ctx.memory)
    return True


def _walk(w: WorldModel, ctx: PlayContext, goal: Pos, reason: str) -> StateOutcome:
    m, policy = ctx.memory, ctx.policy
    _, plan_avoid, plan_costly = plan_sets(w, m, policy, ctx.knowledge)

    def params():
        return grid_params(policy, plan_avoid, plan_costly)

    def plan() -> list[Pos] | None:
        return cost_path(w, goal, params(), nav=nav_search(m, w, GOAL, goal))

    step = bounded_step(m, w, GOAL, goal, plan_avoid, plan, params=params)
    return _out([set_position(step)], f"{reason} → {goal}") if step is not None else _out(None, f"{reason}: no path")


def _out(intents: list[dict] | None, reason: str) -> StateOutcome:
    return StateOutcome(intents, reason, state=InvestigateState.name)
