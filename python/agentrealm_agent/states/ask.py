"""Ask the planner for the way on, and hold the decision while it answers (A114, A120).

Shared by the states that can find no way on and must not stand still
pretending otherwise: the safe default with nothing left to explore, and
Travel with no step toward its destination. The ask is asynchronous: one
``no_way_on`` trigger per cell and reason, then the decision holds with
nothing sent until the planner's reply changes the stack. The survival
reflexes sit above every state that asks, so a hold never stands taking hits.
"""

from __future__ import annotations

from ..memory import queue_signal
from ..plan import GoalOp
from ..world import WorldModel
from .base import PlayContext, StateOutcome


def ask_the_planner(w: WorldModel, ctx: PlayContext, why: str, state: str, op: GoalOp | None = None) -> StateOutcome:
    """Raise one ``no_way_on`` trigger for this cell and ``why``, and hold.

    ``op`` is the op the ask is for (Travel's ``travel``), None for the safe
    default's. A second ask from the same cell is new when its reason is: an
    A120 ask after an A114 one on the same cell still reaches the planner.
    The planner reads State ``map`` (the ground round the character, blocks,
    hostiles, signs and doors by cell), what each held item does and the
    clues, and opens a block with ``break_block``, fights, or goes on.
    """
    m = ctx.memory
    if w.pos is not None and w.map_id is not None and m.no_way_on != (w.map_id, w.pos, why):
        m.no_way_on = (w.map_id, w.pos, why)
        signal = {"trigger": "no_way_on", "map_id": w.map_id, "x": w.pos[0], "y": w.pos[1], "tick": w.tick, "why": why}
        if op is not None:
            signal["op"] = dict(op)
        queue_signal(m, signal)
    return StateOutcome(None, f"{why}: asked the planner", state=state, wait=True, progress=False)
