"""Clear the way to the top op's target, else ask the planner and hold (A120).

Evan's rule for every op with a target: "if a goal or target is obstructed
we need to try to unobstruct it, not get distracted". The op's own executor
keeps the walk, and the safe default never stands in for it
(``explore.HOLD_THEIR_WALK``). Obstructed, it works down one ladder:

1. **Route round.** The executor's own walk does this: its path search goes
   round blocks and the reach of known hostiles, through unseen ground too.
2. **Open the block.** With no way round, the first block on the cheapest
   way that a held item is known to open (the opener table,
   ``break_memory.known_openers``) is walked to and opened
   (``open_the_way``, ``break_toward``). A walk on the stuck ladder (A15)
   leaves that to Break at step 2 instead (``break_due``).
3. **A hostile on the way** is the engagement's to fight, and Fight acts
   above every executor, or the planner's, with a ``fight`` op (A114). The
   executor never starts a fight of its own: it names the hostile in its ask.
4. **Ask.** One ``no_way_on`` with why, what blocks the way and what was
   tried (``ask.ask_the_planner``), and the decision holds while the planner
   answers. The survival reflexes act above the hold. Unanswered, the op
   stalls and is moved below (A104).
"""

from __future__ import annotations

from ..break_memory import choice_at, given_up_blocks, held_capabilities
from ..navigation import cost_path
from ..navigation import stuck as nav_stuck
from ..pathing import grid_params, plan_sets
from ..plan import GoalOp
from ..world import Pos, WorldModel
from .ask import ask_the_planner, what_blocks
from .base import PlayContext, StateOutcome
from .break_state import break_toward


def break_due(w: WorldModel, ctx: PlayContext) -> bool:
    """A walk is at stuck step 2: Break, below every walking executor, works its nominated block (A15)."""
    att = nav_stuck.active(ctx.memory, w)
    return att is not None and att.level == nav_stuck.BREAK


def open_the_way(w: WorldModel, ctx: PlayContext, at: Pos, state: str) -> StateOutcome | None:
    """A move that opens the way to ``at`` when no way round reaches it, else None.

    The way is priced with each block on the straight route that a held item
    opens costed at its break (``grid_params`` ``break_goal``), as Heal's
    walks are (A89). When no way round reaches ``at`` and that way crosses
    such a block, the first one is walked to and opened (``break_toward``):
    a step toward it, or in reach its opener armed and ``Use``d.
    """
    if w.pos is None or w.map_id is None or w.pos == at:
        return None
    m = ctx.memory
    _, plan_avoid, plan_costly = plan_sets(w, m, ctx.policy, ctx.knowledge)
    opening = grid_params(
        ctx.policy, plan_avoid, plan_costly, allow_goal_door=True, m=m, w=w, knowledge=ctx.knowledge, break_goal=at
    )
    if not opening.break_costs:
        return None
    if cost_path(w, at, grid_params(ctx.policy, plan_avoid, plan_costly, allow_goal_door=True)) is not None:
        return None  # a way round: the executor's walk takes it
    cut = next((p for p in cost_path(w, at, opening) or [] if p in opening.break_costs), None)
    choice = choice_at(w, ctx.knowledge, cut, given_up_blocks(m.nav_stuck, w.map_id, w.tick)) if cut else None
    if choice is None:
        return None
    out = break_toward(w, ctx, choice, state)
    return out if out.intents else None


def clear_the_way(
    w: WorldModel, ctx: PlayContext, at: Pos | None, why: str, state: str, op: GoalOp | None
) -> StateOutcome:
    """The executor's walk to ``at`` has no step: open the way, else ask and hold.

    ``at`` is None when the op has no target to walk to (none in sight): it
    asks at once. At stuck step 2 it sends nothing and holds nothing, so
    Break, below, opens the nominated block this decision; the safe default
    holds meanwhile (``explore.HOLD_THEIR_WALK``).
    """
    if break_due(w, ctx):
        return StateOutcome(None, why, state=state)
    if at is not None and (out := open_the_way(w, ctx, at, state)) is not None:
        out.reason = f"{why}: {out.reason}"
        return out
    blocked = what_blocks(w, ctx, at) if at is not None else None
    tried = _tried(w, ctx) if at is not None else None
    return ask_the_planner(w, ctx, why, state, op, blocked_by=blocked, tried=tried)


def _tried(w: WorldModel, ctx: PlayContext) -> str:
    if held_capabilities(w, ctx.knowledge):
        return "a way round, then opening a block on the way with a held item"
    return "a way round; nothing held opens a block"

