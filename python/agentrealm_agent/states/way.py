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
from ..memory import Memory
from ..pathing import grid_params, plan_sets
from ..plan import GoalOp
from ..world import Pos, WorldModel
from .ask import ask_the_planner, what_blocks
from .base import PlayContext, StateOutcome
from .break_state import break_toward


# The ``Memory.goal`` labels each executor's walks on the stuck ladder carry
# (A15). ``break_due`` reads them, to tell Break working the op's own stuck
# walk from any other stuck walk: only the op's own is left to Break, and
# only that break counts as the op's progress (dispatch); the safe default's
# ``explore`` or an earlier op's walk is never the op's (A120). The paced-op
# stall does not use them (A90). Travel's search for a hunting ground, and
# Shop's round town, walk under ``explore_area`` (A27). Loot's, Investigate's
# and Solve's walks are ``bounded_step`` walks or their own, which give up
# rather than escalate to Break, so they need no entry.
OWNER_WALKS: dict[str, tuple[str, ...]] = {
    "Travel": ("travel:", "explore_area"),
    "Shop": ("travel:", "explore_area"),  # its walk to a shop or town, and its search round town
    "Explore": ("explore_area",),
    "Level": ("level:",),
    "Boss": ("boss:",),
}


def break_due(w: WorldModel, m: Memory, owner: str | None) -> bool:
    """``owner``'s own walk is at stuck step 2: Break, below every walking
    executor, works its nominated block (A15). A walk of anyone else's at
    step 2 is not the op's way, so it never stands in for the op's ask."""
    att = nav_stuck.active(m, w)
    if owner is None or att is None or att.level != nav_stuck.BREAK:
        return False
    return att.goal.startswith(OWNER_WALKS.get(owner, ()))


def open_the_way(w: WorldModel, ctx: PlayContext, at: Pos, state: str) -> StateOutcome | None:
    """A move that opens the way to ``at`` when no way round reaches it, else None.

    The way is priced with each block on the straight route that a held item
    opens costed at its break (``grid_params`` ``break_goal``), as Heal's
    walks are (A89). When no way round reaches ``at`` and that way crosses
    such a block, the first one short of ``at`` is walked to and opened (``break_toward``):
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
    # ``at`` itself is never the block on the way: a ``use_block`` target is a
    # block, and its op is to use its item on it, not to open it.
    cut = next((p for p in cost_path(w, at, opening) or [] if p in opening.break_costs and p != at), None)
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
    m = ctx.memory
    if break_due(w, m, state):
        m.break_yield = m.nav_stuck.decision
        return StateOutcome(None, why, state=state)
    key = _shut_key(w, m, state, op, at)
    if at is not None and m.way_shut != key:
        if (out := open_the_way(w, ctx, at, state)) is not None:
            m.way_shut = None
            out.reason = f"{why}: {out.reason}"
            return out
        m.way_shut = key  # still shut until something it was found under changes
    blocked = tried = None
    if at is not None and m.no_way_on != (w.map_id, w.pos, why):
        blocked, tried = what_blocks(w, ctx, at), _tried(w, ctx)  # only for an ask that goes out
    return ask_the_planner(w, ctx, why, state, op, blocked_by=blocked, tried=tried)


def _shut_key(w: WorldModel, m: Memory, state: str, op: GoalOp | None, at: Pos | None) -> tuple:
    """What a shut way was found under: the executor, its op, the map, the
    cell, the target, the map's tiles (``Tiles.revision``), what is held, and
    the blocks backed off from (A90), whose backoff lapses by time alone."""
    held = tuple(sorted((s.code or "") for s in w.held_supplies)) + (w.armed_code or "",)
    op_key = tuple(sorted((k, repr(v)) for k, v in op.items())) if op is not None else None
    backed_off = given_up_blocks(m.nav_stuck, w.map_id, w.tick)
    return (state, op_key, w.map_id, w.pos, at, w.view.tiles.revision, held, backed_off)


def _tried(w: WorldModel, ctx: PlayContext) -> str:
    if held_capabilities(w, ctx.knowledge):
        return "a way round, then opening a block on the way with a held item"
    return "a way round; nothing held opens a block"

