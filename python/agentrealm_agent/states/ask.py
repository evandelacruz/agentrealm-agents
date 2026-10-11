"""Ask the planner for the way on, and hold the decision while it answers (A114, A120).

Shared by the states that can find no way on and must not stand still
pretending otherwise: the safe default with nothing left to explore, and
every executor whose op's way is shut once it has tried to clear it
(``way.clear_the_way``). The ask is asynchronous: one ``no_way_on``
trigger per cell and reason, then the decision holds with nothing sent
until the planner's reply changes the stack. The survival reflexes sit
above every state that asks, so a hold never stands taking hits.
"""

from __future__ import annotations

from ..break_memory import breakable_block, choice_at, given_up_blocks, straight_route
from ..memory import queue_signal
from ..navigation import cost_path
from ..pathing import grid_params, plan_sets
from ..plan import GoalOp
from ..survival import hostiles_reaching
from ..world import Pos, WorldModel
from .base import PlayContext, StateOutcome


def ask_the_planner(
    w: WorldModel,
    ctx: PlayContext,
    why: str,
    state: str,
    op: GoalOp | None = None,
    *,
    blocked_by: str | None = None,
    tried: str | None = None,
) -> StateOutcome:
    """Raise one ``no_way_on`` trigger for this cell and ``why``, and hold.

    ``op`` is the op the ask is for, None for the safe default's;
    ``blocked_by`` says what shuts the way (blocks, hostiles, unseen
    ground) and ``tried`` what the executor tried first. A second ask from
    the same cell is new only when its reason is: an executor's ask after
    the safe default's on the same cell still reaches the planner, and a
    hostile moving about does not ask again. The planner reads State ``map``
    (the ground round the character, blocks, hostiles, signs and doors by
    cell), what each held item does and the clues, and opens a block with
    ``break_block``, fights, or goes another way.
    """
    m = ctx.memory
    if w.pos is not None and w.map_id is not None and m.no_way_on != (w.map_id, w.pos, why):
        m.no_way_on = (w.map_id, w.pos, why)
        signal = {"trigger": "no_way_on", "map_id": w.map_id, "x": w.pos[0], "y": w.pos[1], "tick": w.tick, "why": why}
        if op is not None:
            signal["op"] = dict(op)
        if blocked_by:
            signal["blocked_by"] = blocked_by
        if tried:
            signal["tried"] = tried
        queue_signal(m, signal)
    return StateOutcome(None, f"{why}: asked the planner", state=state, wait=True, progress=False)


def what_blocks(w: WorldModel, ctx: PlayContext, at: Pos) -> str:
    """What shuts the way to ``at``, for the planner: the known hostiles whose
    reach the way (or, with none, the straight route) crosses, and the first
    block on the straight route, with whether a held item opens it."""
    if w.pos is None:
        return "position unknown"
    m = ctx.memory
    _, plan_avoid, plan_costly = plan_sets(w, m, ctx.policy, ctx.knowledge)
    way = cost_path(w, at, grid_params(ctx.policy, plan_avoid, plan_costly, allow_goal_door=True))
    route = straight_route(w.pos, at)
    cells = way or [w.pos, *route]
    parts = []
    ids = hostiles_reaching(w, ctx.policy, cells)
    for e in sorted((e for e in w.entities if (e.kind, e.id) in ids), key=lambda e: (e.kind, e.id)):
        parts.append(f"hostile {e.code or e.kind} {e.id} at {e.pos[0]},{e.pos[1]}")
    skip = given_up_blocks(m.nav_stuck, w.map_id, w.tick)
    if (p := next((p for p in route if breakable_block(w, p) is not None), None)) is not None:
        opens = choice_at(w, ctx.knowledge, p, skip) is not None
        parts.append(f"{w.view.tiles.get(p)} at {p[0]},{p[1]}{'' if opens else ', which nothing held opens'}")
    if not parts:
        parts.append("no known way: walls, no ground or unseen ground")
    return "; ".join(parts)
