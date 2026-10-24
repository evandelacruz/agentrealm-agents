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
   A hostile's ground that is the only thing between the walk and its
   target is passed when, priced as a chaser, it never comes within range
   and health covers it (``pathing.HostileReach``, A146, A151, A162); when
   it does not and that one hostile in view is one the engagement would
   fight from the land cell where it first comes within range, the walk
   lands there (``land_to_fight``, A151), and the engagement takes it ashore.
4. **Ask.** One ``no_way_on`` with why, what blocks the way and what was
   tried: the searches, ``Use``s and recorded breaks, never what is merely
   held (``ask.ask_the_planner``), and the decision holds while the planner
   answers. The survival reflexes act above the hold. Unanswered, the op
   stalls and is moved below (A104); a give-up that lapses while it is on
   top has it walk again.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Collection

from agentrealm_adk.break_memory import attempts_at, breakable_block, choice_at, given_up_blocks, straight_route
from agentrealm_adk.navigation import cost_path
from agentrealm_adk.navigation.planner import CostGridParams
from agentrealm_adk.navigation import stuck as nav_stuck
from agentrealm_adk.config import Policy
from agentrealm_adk.directives import attack_forbidden
from agentrealm_adk.engagement import would_fight
from agentrealm_adk.fording import on_water
from agentrealm_adk.hostile_ground import chase
from agentrealm_adk.memory import Memory
from agentrealm_adk.pathing import HostileReach, bounded_step, grid_params, plan_sets
from agentrealm_adk.world import Entity, Pos, WorldModel, chebyshev

from ..plan import GoalOp
from .intents import set_position
from .ask import ask_key, ask_the_planner, op_key, what_blocks
from .base import PlayContext, StateOutcome, op_walk
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
    "Shop": ("travel:", "explore_area"),  # its walk to a seller, and its search round one
    "Explore": ("explore_area",),
    "Level": ("level:",),
    "Boss": ("boss:",),
}


# The walk ``land_to_fight`` takes to land beside a hostile that shuts the way (A151).
LAND_GOAL = "land"

# The newest blocks the way-clearing chose that ``tried`` remembers (``Memory.way_blocks``).
WAY_BLOCKS_KEPT = 16


def break_due(w: WorldModel, m: Memory, owner: str | None) -> bool:
    """``owner``'s own walk is at stuck step 2: Break, below every walking
    executor, works its nominated block (A15). A walk of anyone else's at
    step 2 is not the op's way, so it never stands in for the op's ask."""
    att = nav_stuck.active(m, w)
    if owner is None or att is None or att.level != nav_stuck.BREAK:
        return False
    return att.goal.startswith(OWNER_WALKS.get(owner, ()))


def open_the_way(
    w: WorldModel,
    ctx: PlayContext,
    at: Pos,
    state: str,
    *,
    no_step: bool = False,
    reach: Collection[Pos] | None = None,
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

    ``reach`` is the ground the op's walk keeps out of (``base.op_walk``,
    A128; worked out here when None): the block is walked to only on a way
    clear of it (``break_toward``), so a block beside a pack is not opened
    and the executor asks instead.
    """
    if w.pos is None or w.map_id is None or w.pos == at:
        return None
    m = ctx.memory
    _, plan_avoid, plan_costly = plan_sets(w, m, ctx.policy, ctx.knowledge)
    opening = _opening_grid(w, ctx, at, plan_avoid, plan_costly)
    if not opening.break_costs:
        return None
    round_grid = grid_params(ctx.policy, plan_avoid, plan_costly, allow_goal_door=True)
    if no_step:
        round_grid = dataclasses.replace(round_grid, fog_cost=None)
    if cost_path(w, at, round_grid) is not None:
        return None  # a way round: the executor's walk takes it
    # ``at`` itself is never the block on the way: a ``use_block`` target is a
    # block, and its op is to use its item on it, not to open it.
    path = cost_path(w, at, opening) or []
    cells = _way_cells(w.pos, at, path) if no_step else path
    cut = next((p for p in cells if p in opening.break_costs and p != at), None)
    choice = choice_at(w, ctx.knowledge, cut, given_up_blocks(m.nav_stuck, w.map_id, w.tick)) if cut else None
    if choice is None:
        return None
    out = break_toward(w, ctx, choice, state, op_walk(w, ctx)[2] if reach is None else reach)
    if not out.intents:
        return None
    opened = (w.map_id, at, choice.pos)
    m.way_blocks = [*(b for b in m.way_blocks if b[:3] != opened), (*opened, w.tick)][-WAY_BLOCKS_KEPT:]
    return out


def _opening_grid(
    w: WorldModel, ctx: PlayContext, at: Pos, plan_avoid: set[Pos], plan_costly: set[Pos]
) -> CostGridParams:
    """The grid the way to ``at`` is priced on: each block on the straight
    route a held item opens costed at its break, unexplored ground at fog
    cost, from the cost sets every walk plans with (``plan_sets``).
    ``open_the_way`` picks its block from the cheapest way on it, and
    ``_tried`` reads the same cells."""
    return grid_params(
        ctx.policy, plan_avoid, plan_costly, allow_goal_door=True, m=ctx.memory, w=w, knowledge=ctx.knowledge, break_goal=at
    )


def _way_cells(start: Pos, at: Pos, path: list[Pos]) -> list[Pos]:
    """The cells of the priced way from ``start``, then the rest of the straight route to ``at``."""
    return list(dict.fromkeys([*path, *straight_route(start, at)]))


def clear_the_way(
    w: WorldModel,
    ctx: PlayContext,
    at: Pos | None,
    why: str,
    state: str,
    op: GoalOp | None,
    *,
    reach: Collection[Pos] | None = None,
) -> StateOutcome:
    """The executor's walk to ``at`` has no step: open the way, else ask and hold.

    ``at`` is None when the op has no target to walk to (none in sight): it
    asks at once. When the executor's own walk to ``at`` was given up, the
    ask's ``tried`` leads with how each level of its ladder failed
    (``walk_given_up``, A125). At stuck step 2 it sends nothing and holds
    nothing, so Break, below, opens the nominated block this decision; the
    safe default holds meanwhile (``explore.HOLD_THEIR_WALK``). A block is
    opened only on a way clear of ``reach`` (``open_the_way``, A128).
    """
    m = ctx.memory
    if break_due(w, m, state):
        m.break_yield = m.nav_stuck.decision
        return StateOutcome(None, why, state=state)
    key = _shut_key(w, ctx, state, op, at)
    if at is not None and m.way_shut != key:
        out = open_the_way(w, ctx, at, state, no_step=True, reach=reach)
        if out is None and isinstance(reach, HostileReach):
            out = land_to_fight(w, ctx, at, state, reach)
        if out is not None:
            m.way_shut = None
            out.reason = f"{why}: {out.reason}"
            return out
        m.way_shut = key  # still shut until something it was found under changes
    blocked = tried = None
    if at is not None and m.no_way_on != ask_key(w, why, op):
        hostile_reach = reach if isinstance(reach, HostileReach) else None
        blocked = what_blocks(w, ctx, at, hostile_reach)  # only for an ask that goes out
        walked = walk_given_up(w, m, state, at)
        tried = f"{walked}, then {_tried(w, ctx, at)}" if walked else _tried(w, ctx, at)
    return ask_the_planner(w, ctx, why, state, op, blocked_by=blocked, tried=tried)


def land_to_fight(w: WorldModel, ctx: PlayContext, at: Pos, state: str, reach: HostileReach) -> StateOutcome | None:
    """A step toward land to fight from, when one hostile's ground is all
    that shuts the way to ``at`` and the pass through it is not covered
    (A151, A120, A162): that hostile is in view and not forbidden, and a
    land cell within its range is reached on a way where, priced as a
    chaser (``hostile_ground.chase``: it steps toward us as we walk), it
    first comes within range of us on land, and the engagement would fight
    it from that cell (``engagement.would_fight`` as it would stand there,
    so the decision it makes there is fight). A chaser in the open meets
    the walk before the landing; one the ground stops, at it. On land the
    engagement takes it and Fight arms the weapon (A133); afloat it never
    says fight (A146), so no meeting on water will do. The landings are
    tried nearest first, each by a way that keeps out of its range but on
    that cell, and the search runs only while the way is not known shut
    (``clear_the_way``'s ``Memory.way_shut``). None when no such landing
    exists: then the executor asks."""
    if w.pos is None:
        return None
    m, policy = ctx.memory, ctx.policy
    _, plan_avoid, plan_costly = plan_sets(w, m, policy, ctx.knowledge)
    way = cost_path(w, at, grid_params(policy, plan_avoid, plan_costly, allow_goal_door=True))
    crossed = reach.crossed(way or [])
    if len(crossed) != 1 or not crossed[0].in_view or attack_forbidden(crossed[0].entity, ctx.never_attack):
        return None
    ground, e = crossed[0], crossed[0].entity
    near = {p for p in reach.cells if chebyshev(p, e.pos) <= policy.hostile_range}
    others = set(reach.cells) - ground.cells
    land = w.walkable_test(swing=True)

    def ashore(p: Pos) -> bool:
        return land(p) and not on_water(w, m, p)

    # One search per landing, nearest first, until one is reached. No cheaper
    # test rules a landing out first: the chase meets the walk where it comes
    # within range of us, which for a chaser in the open lies outside its
    # range of where it stands now, so a decision on that ground says nothing.
    landings = [p for p in near if ashore(p) and p != e.pos]
    for cell in sorted(landings, key=lambda p: (chebyshev(p, w.pos), p)):
        avoid = plan_avoid | others | (near - {cell})
        path = cost_path(w, cell, grid_params(policy, avoid, plan_costly))
        meet = _meeting(w, policy, e, path)
        if meet is not None and ashore(meet) and would_fight(w, policy, ctx.params, also=e, m=m, at=meet):
            break
    else:
        return None

    def params():
        return grid_params(policy, avoid, plan_costly, m=m)

    step = bounded_step(m, w, LAND_GOAL, cell, avoid, lambda: cost_path(w, cell, params()), params=params)
    if step is None:
        return None
    return StateOutcome([set_position(step)], f"land at {cell[0]},{cell[1]} to fight {e.code or e.kind} {e.id}", state=state)


def _meeting(w: WorldModel, policy: Policy, e: Entity, path: list[Pos] | None) -> Pos | None:
    """The first cell of ``path`` where ``e``, chasing us as we walk it
    (``hostile_ground.chase``), is within ``policy.hostile_range``: where
    the engagement takes it. None with no path or no such cell."""
    if not path:
        return None
    return next((p for c, p in zip(chase(w, e.pos, path), path) if chebyshev(c, p) <= policy.hostile_range), None)


def walk_given_up(w: WorldModel, m: Memory, owner: str, at: Pos) -> str | None:
    """How ``owner``'s own walk to ``at`` failed, when the stuck ladder gave it
    up and it is still backed off (``OWNER_WALKS``): each level's reason in
    order (A125). A walk to another map is given up under its destination
    there, so ``at``, None for it, never names it: that ask goes out at once."""
    walks = OWNER_WALKS.get(owner, ())
    for rec in m.nav_stuck.given_up.values():
        mine = (rec.map_id, rec.target) == (w.map_id, at) and rec.goal.startswith(walks)
        if mine and rec.holds(w.tick) and rec.reasons:
            return f"the walk, given up ({', '.join(rec.reasons)})"
    return None


def _shut_key(w: WorldModel, ctx: PlayContext, state: str, op: GoalOp | None, at: Pos | None) -> tuple:
    """What a shut way was found under: the executor, its op, the map, the
    cell, the target, the map's tiles (``Tiles.revision``), what is held, and
    what lapses: the walks given up and still holding (A90: a block, the
    op's own walk, any other; each lifts once its backoff ends and its
    ground changes, A171) and the cells the route learnings keep out or
    price (``plan_sets``), by time. A give-up that lapses with the op on
    top has the way searched again (A129)."""
    m = ctx.memory
    held = tuple(sorted((s.code or "") for s in w.held_supplies)) + (w.armed_code or "",)
    backed_off = frozenset(
        k for k, r in m.nav_stuck.given_up.items() if r.map_id == w.map_id and nav_stuck.is_backed_off(m.nav_stuck, k, w.tick)
    )
    _, avoid, costly = plan_sets(w, m, ctx.policy, ctx.knowledge)
    learned = (frozenset(avoid), frozenset(costly))
    return (state, op_key(op), w.map_id, w.pos, at, w.view.tiles.revision, held, backed_off, learned)


def _tried(w: WorldModel, ctx: PlayContext, at: Pos) -> str:
    """What this op actually tried on the way to ``at``: the walk's search for
    a way round, then each break the knowledge base records since the op
    reached the top (``Plan.top_since_tick``), and a ``Use`` still in flight,
    on a block of the way: the cells ``open_the_way`` picks from
    (``_opening_grid``, ``_way_cells``) and the blocks it chose for ``at``
    from other cells since then (``Memory.way_blocks``). Never what is merely
    held, nor an older break the knowledge base keeps from another op or run (A129)."""
    parts = ["a way round"]
    since = ctx.plan.top_since_tick if ctx.plan is not None else None
    if w.pos is None or w.map_id is None or since is None:
        return parts[0]
    m = ctx.memory
    chosen = [b for mid, to, b, tick in m.way_blocks if (mid, to) == (w.map_id, at) and tick >= since]
    _, plan_avoid, plan_costly = plan_sets(w, m, ctx.policy, ctx.knowledge)
    path = cost_path(w, at, _opening_grid(w, ctx, at, plan_avoid, plan_costly)) or []
    for p in dict.fromkeys([*_way_cells(w.pos, at, path), *chosen]):
        block = breakable_block(w, p)
        for cap, result in attempts_at(ctx.knowledge, w.map_id, p, since):
            parts.append(f"{cap} on {block or 'the block'} at {p[0]},{p[1]}: {result}")
        if m.break_pending is not None and m.break_pending[:2] == (w.map_id, p):
            parts.append(f"{m.break_pending[2]} on {block or 'the block'} at {p[0]},{p[1]}: sent")
    return ", then ".join(parts)
