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
   (``open_the_way``, ``break_toward``). Once the walk has no step, only
   a way round over known ground counts: unexplored ground beyond the block
   is priced at fog cost, so a block on the only known way is opened before
   any ask. A walk on the stuck ladder (A15) leaves that to Break at step 2
   instead (``break_due``).
3. **A hostile on the way** is the engagement's to fight, and Fight acts
   above every executor, or the planner's, with a ``fight`` op (A114). The
   executor never starts a fight of its own: it names the hostile in its ask.
4. **Ask.** One ``no_way_on`` with why, what blocks the way and what was
   tried: the searches, ``Use``s and recorded breaks, never what is merely
   held (``ask.ask_the_planner``), and the decision holds while the planner
   answers. The survival reflexes act above the hold. Unanswered, the op
   stalls and is moved below (A104); a give-up that lapses while it is on
   top has it walk again.
"""

from __future__ import annotations

import dataclasses

from ..break_memory import attempts_at, breakable_block, choice_at, given_up_blocks, straight_route
from ..navigation import cost_path
from ..navigation import stuck as nav_stuck
from ..memory import Memory
from ..pathing import grid_params, plan_sets
from ..plan import GoalOp
from ..world import Pos, WorldModel
from .ask import ask_key, ask_the_planner, op_key, what_blocks
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


def open_the_way(
    w: WorldModel, ctx: PlayContext, at: Pos, state: str, *, no_step: bool = False
) -> StateOutcome | None:
    """A move that opens the way to ``at`` when no way round reaches it, else None.

    The way is priced with each block on the straight route that a held item
    opens costed at its break (``grid_params`` ``break_goal``), as Heal's
    walks are (A89), and unexplored ground passable at fog cost, as every
    walk prices it. When no way round reaches ``at`` and that way crosses
    such a block, the first one short of ``at`` is walked to and opened (``break_toward``):
    a step toward it, or in reach its opener armed and ``Use``d.

    ``no_step``: the executor's walk, which routes through unexplored ground
    too, found no step. A way round through unexplored ground is then one it
    could not take: only a way round over known ground counts, and when the
    cheapest way still goes round through unexplored ground, the block is
    the first one on the straight route a held item opens (A129).
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
    round_grid = grid_params(ctx.policy, plan_avoid, plan_costly, allow_goal_door=True)
    if no_step:
        round_grid = dataclasses.replace(round_grid, fog_cost=None)
    if cost_path(w, at, round_grid) is not None:
        return None  # a way round: the executor's walk takes it
    # ``at`` itself is never the block on the way: a ``use_block`` target is a
    # block, and its op is to use its item on it, not to open it.
    on_way = [p for p in cost_path(w, at, opening) or [] if p in opening.break_costs and p != at]
    if not on_way and no_step:
        on_way = [p for p in straight_route(w.pos, at) if p in opening.break_costs and p != at]
    cut = on_way[0] if on_way else None
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
    key = _shut_key(w, ctx, state, op, at)
    if at is not None and m.way_shut != key:
        if (out := open_the_way(w, ctx, at, state, no_step=True)) is not None:
            m.way_shut = None
            out.reason = f"{why}: {out.reason}"
            return out
        m.way_shut = key  # still shut until something it was found under changes
    blocked = tried = None
    if at is not None and m.no_way_on != ask_key(w, why, op):
        blocked, tried = what_blocks(w, ctx, at), _tried(w, ctx, at)  # only for an ask that goes out
    return ask_the_planner(w, ctx, why, state, op, blocked_by=blocked, tried=tried)


def _shut_key(w: WorldModel, ctx: PlayContext, state: str, op: GoalOp | None, at: Pos | None) -> tuple:
    """What a shut way was found under: the executor, its op, the map, the
    cell, the target, the map's tiles (``Tiles.revision``), what is held, and
    what lapses by time alone: the walks given up and still backed off (A90:
    a block, the op's own walk, any other) and the cells the route learnings
    keep out or price (``plan_sets``). A give-up that lapses with the op on
    top has the way searched again (A129)."""
    m = ctx.memory
    held = tuple(sorted((s.code or "") for s in w.held_supplies)) + (w.armed_code or "",)
    backed_off = frozenset(k for k, r in m.nav_stuck.given_up.items() if r.map_id == w.map_id and w.tick < r.until)
    _, avoid, costly = plan_sets(w, m, ctx.policy, ctx.knowledge)
    learned = (frozenset(avoid), frozenset(costly))
    return (state, op_key(op), w.map_id, w.pos, at, w.view.tiles.revision, held, backed_off, learned)


def _tried(w: WorldModel, ctx: PlayContext, at: Pos) -> str:
    """What was actually tried on the way to ``at``: the walk's search for a
    way round, then each break of a block on the straight route the
    knowledge base records and a ``Use`` still in flight. Never what is
    merely held (A129)."""
    assert w.pos is not None
    m = ctx.memory
    parts = ["a way round"]
    for p in straight_route(w.pos, at):
        block = breakable_block(w, p)
        for cap, result in attempts_at(ctx.knowledge, w.map_id, p):
            parts.append(f"{cap} on {block or 'the block'} at {p[0]},{p[1]}: {result}")
        if m.break_pending is not None and m.break_pending[:2] == (w.map_id, p):
            parts.append(f"{m.break_pending[2]} on {block or 'the block'} at {p[0]},{p[1]}: sent")
    return ", then ".join(parts)
