"""Gather: carry out the plan's ``gather_gems`` op from grass and gem piles (A22, A88).

Gems come from cutting grass, which drops one some of the time, and from
gem piles on the ground, a gem for sure. Bushes drop berries, not gems
(GAME_NOTES.md Gems), so Gather never cuts one; food is Heal's and Loot's.

Each decision Gather runs one search for the place to work next:

- **Where it may work** (``Ground``): ground ``gather_ground`` allows (known,
  off hazards, clear of every known hostile's bar, out of ground a
  remembered hostile holds). For cutting, not a region its cuts showed
  barren, nor ground where cuts keep having no effect
  (``GemYieldTracker.uncuttable``), nor a cell cut too recently to have
  grown back (``gem_yield.exhausted_cells``).
- **What it may go for there** (``_targets``): grass to cut, gem piles to
  take, and the edge of unexplored ground, where more grass may lie; only
  inside the op's target region while Gather works one.
- **What each costs**: the walk there (``_price``: its steps, and the
  steps ground near a hostile adds), plus the work there (``GemOdds.work``):
  the actions that get the gems the op still needs at the yield there
  (``gem_yield.region_yield``, the map's ``expected_yield`` on unsampled
  ground): a cut for each gem a cut is expected to drop; a pile is one
  ``Take`` for a sure gem, then cuts for the rest; an edge first pays the
  steps past it to grass. The cheapest wins. No walk crosses a known
  hostile's reach (``route_bar``).
- **Commitment** (A71): the target picked is kept until it is reached, is
  no longer a target, has no way there, or its first step stays taken for
  ``HOLD_TICKS``, and let go for its price only by the one rule
  (``targets.outpriced``, A109).

What is in reach (a gem pile beside it, the grass it stands on) costs no
walk, so it is worked unless the walk Gather would take costs less in all.

The calls that depend on the situation are the planner's. Which region:
a ``gather_gems`` with ``x, y`` names a target region; Gather works only
there while it has a target there that a way reaches (its middle while no
cell of it is known), and works anywhere otherwise. Whether to
work near a hostile: ``fight`` lifts the remembered ground and the route
bar. Gather itself only keeps clear: a hostile that stays near without
attacking bars the cells near it, and the price sends Gather to other grass.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from typing import Callable

from .. import targets as targets_mod
from ..config import Policy
from ..executor import DEFAULT_TICK_RATE_HZ
from ..gem_yield import (
    GATHER_BLOCKS,
    REGION_SIZE,
    STALL_SECONDS,
    GemYieldTracker,
    barren_regions,
    blocks_to_region,
    cut_or_since,
    exhausted_cells,
    region_corner,
    region_of,
    region_yields,
)
from ..supplies import heals
from ..hostile_ground import Danger, danger, reach_cells
from ..item_table import InventorySupply
from ..knowledge_base import KnowledgeBase
from ..memory import Memory
from ..navigation import CostGridParams, cost_path, nearest_target
from ..navigation import stuck as nav_stuck
from ..pathing import grid_params, next_step
from ..plan import GoalOp
from ..equip import CUT, does_job, hunting_ceiling, weapon_to_arm
from ..survival import is_hostile
from ..world import Entity, Pos, WorldModel, chebyshev
from ..zone_discovery import safe_tiles
from .base import PlayContext, State, StateOutcome, my_op
from ..pathing import plan_sets
from .explore import safe_default
from .gather_safe import gather_ground, route_bar, route_clear
from .intents import arm, arm_and_use, set_position, take, use_block
from .solve import held_supply

# Authored gem piles spawn as ground supplies (Obs, GAME_NOTES.md Gems).
# Gem caches (gem_cache_5/7/10) are a different drop and are not piles.
# A priced supply with the same code is shop stock: taking it is a purchase.
GEM_PILE_SUPPLY_CODES: frozenset[str] = frozenset({"gem"})
GOAL = "gather"
# ``m.gather_target`` kinds: what Gather walks to.
GRASS = "grass"
PILE = "pile"
FRONTIER = "frontier"  # the edge of unexplored ground
REGION = "region"  # the middle of a target region no cell of which is known yet
# Targets of one price searched for per search, nearest first: bounds the
# search when the closest ones turn out unreachable (across water, say).
GATHER_CANDIDATES = 16
# Gather waits this long for a taken first step toward its target (an
# occupant), then gives the target up (5 s at 10 ticks/s, as a walk's fog hold).
HOLD_TICKS = 50
# Gather's last decision, for the planner's State (``Memory.gather_status``).
CUTTING = "cutting"
TAKING = "taking a gem"
WALKING = "walking to {}"
NO_EFFECT = "cuts have no effect here"
NONE_CUTTABLE = "no cuttable cell in view"
REGION_BARREN = "region barren"
BLOCKED = "blocked by hostile"
WAITING = "way to {} taken, waiting"
STALLED = "{}, no cut for {} s"
WALK_TARGETS = {
    GRASS: "grass",
    PILE: "a gem pile",
    REGION: "a target region",
    FRONTIER: "unexplored ground",
}
IN_REGION = "{} (region {},{})"  # any status while Gather works only in a target region

Target = tuple[str, Pos]


class GatherState(State):
    """Executor for ``gather_gems``: ``Use`` grass or ``Take`` gem piles
    until the gem counter reaches the op's count, walking to the cheapest
    place to work when nothing is in reach. A cut goes out with a tool that
    cuts, armed in the same queue when the armed item is not known to cut;
    the weapon it swapped out is armed again once no ``gather_gems`` op is
    on top. With nowhere to go, it explores (the safe default) to reveal
    more."""

    name = "Gather"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        if ctx.policy.kind != "scripted" or not world.alive or world.pos is None:
            return False
        return my_op(ctx, self.name) is not None or ctx.memory.gather_rearm is not None

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not self.guard(world, ctx)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        op = my_op(ctx, self.name)
        if op is None:
            return _rearm_weapon(world, ctx.memory)
        tick_hz = ctx.plan.tick_hz if ctx.plan is not None else DEFAULT_TICK_RATE_HZ
        out = gather_outcome(
            world, ctx.memory, ctx.policy, knowledge=ctx.knowledge, op=op, gem_cuts=ctx.gem_cuts, tick_hz=tick_hz
        )
        if out.intents is not None or out.wait:
            return out
        out = safe_default(world, ctx)
        out.state, out.reason = self.name, f"look for gems: {out.reason}"
        return out


def is_gem_pile(e: Entity) -> bool:
    """A free ground gem: an authored pile, or one a grass cut or a kill dropped."""
    return e.kind == "supply" and e.code in GEM_PILE_SUPPLY_CODES and e.gem_price is None


@dataclass(frozen=True)
class GemOdds:
    """What a place to work costs beyond the walk there: the gems the op
    still needs, the gems a cut drops in each region of this map
    (``gem_yield.region_yield``; the map's ``expected_yield`` where no cut
    was made), and the steps past an edge of unexplored ground to grass."""

    need: int
    rate: float
    by_region: dict[tuple[int, int], float]
    past_edge: float = 0.0

    def work(self, kind: str, at: Pos) -> float:
        """The actions at ``at`` that get the gems still needed, each priced
        like a step: a cut for each gem a cut there is expected to drop; a
        pile is one ``Take`` for a sure gem, then cuts for the rest; an edge
        first pays the steps past it to grass."""
        per_gem = 1 / max(self.by_region.get(region_of(at), self.rate), 1e-6)
        if kind == PILE:
            return 1 + (self.need - 1) * per_gem
        return self.need * per_gem + (self.past_edge if kind == FRONTIER else 0)


@dataclass
class Ground:
    """Where Gather may work this decision, worked out once (``ground``).

    ``danger``: known hostiles' ground, and whether the op chose to fight
    for it. ``skip``: regions not cut (barren, unless the op names it, or
    where cuts have no effect). ``spent``: cells not cut now (cut too
    recently, or held out after a cut with no effect). ``dead_regions`` and
    ``dead_cells``: ground where cuts keep having no effect
    (``_cut_ground``). ``named``: the region the op names, if any;
    ``region``: the target region Gather works only in, else None.
    ``targets``: every place it may go for, by kind and cell, with its work
    (``GemOdds.work``); ``anywhere``: the same with no region bound
    (``lift_region``)."""

    danger: Danger
    skip: set[tuple[int, int]]
    spent: set[Pos]
    dead_regions: set[tuple[int, int]]
    dead_cells: set[Pos]
    named: tuple[int, int] | None
    region: tuple[int, int] | None
    odds: GemOdds
    targets: dict[Target, float]
    anywhere: dict[Target, float]

    def lift_region(self) -> None:
        """Work anywhere: nothing in the target region can be reached."""
        self.region, self.targets = None, self.anywhere


def ground(
    w: WorldModel,
    m: Memory,
    policy: Policy,
    knowledge: KnowledgeBase | None,
    op: GoalOp | None,
    gem_cuts: GemYieldTracker | None,
) -> Ground:
    """This decision's ``Ground``."""
    d = danger(w, policy, bool(op.get("fight")) if op is not None else False)
    named = region_of((op["x"], op["y"])) if op is not None and "x" in op and "y" in op else None
    safe = safe_tiles(w, w.map_id) if w.map_id is not None else set()
    dead_regions, dead_cells = gem_cuts.uncuttable(w, safe) if gem_cuts is not None else (set(), set())
    skip = (barren_regions(knowledge, w.map_id) - {named}) | dead_regions
    spent = exhausted_cells(knowledge, w.map_id, w.tick, gem_cuts)
    rate, by_region = region_yields(knowledge, w.map_id)
    need = max(1, int(op["count"]) - (w.gems or 0)) if op is not None and "count" in op else 1
    odds = GemOdds(need, rate, by_region, _steps_to_grass(w))
    g = Ground(d, skip, spent, dead_regions, dead_cells, named, None, odds, {}, {})
    found = g.anywhere = _targets(w, m, policy, g)
    if named is not None and not _seen_region(w, named):
        x0, y0 = region_corner(named)
        middle = (x0 + REGION_SIZE // 2, y0 + REGION_SIZE // 2)
        g.region, found = named, {(REGION, middle): odds.work(GRASS, middle)}
    elif named is not None and any(region_of(p) == named for _, p in found):
        g.region, found = named, {t: v for t, v in found.items() if region_of(t[1]) == named}
    g.targets = found
    return g


def _targets(w: WorldModel, m: Memory, policy: Policy, g: Ground) -> dict[Target, float]:
    """Every place Gather may go for, with its work: grass to cut and edges
    of unexplored ground (off skipped regions and ground where cuts have no
    effect), and gem piles, all on ``gather_ground``. A pile out of sight
    that Gather is walking to stays one until a look at its cell shows it
    gone. An edge the explorers' stuck detection gave up on and still backs
    off is not one (``nav_stuck.filter_frontiers``)."""
    here = w.pos
    assert here is not None

    def may_work(p: Pos) -> bool:
        return gather_ground(w, p, policy, g.danger)

    def may_cut(p: Pos) -> bool:
        return _cut_ground(g, p) and may_work(p)

    out: list[Target] = [
        (GRASS, p) for p, block in w.view.tiles.items() if block in GATHER_BLOCKS and p not in g.spent and may_cut(p)
    ]
    piles = {e.pos for e in w.entities if is_gem_pile(e)}
    kept = m.gather_target
    if kept is not None and kept[0] == PILE and chebyshev(kept[1], here) > w.perception:
        piles.add(kept[1])
    out += [(PILE, p) for p in piles if may_work(p)]
    edges = nav_stuck.filter_frontiers(m.nav_stuck, w.map_id, w.view.frontier(), w.tick)
    out += [(FRONTIER, p) for p in edges if p != here and may_cut(p)]
    return {t: g.odds.work(*t) for t in out}


def gather_outcome(
    w: WorldModel,
    m: Memory,
    policy: Policy,
    *,
    knowledge: KnowledgeBase | None = None,
    op: GoalOp | None = None,
    gem_cuts: GemYieldTracker | None = None,
    state: str = "Gather",
    tick_hz: int = DEFAULT_TICK_RATE_HZ,
) -> StateOutcome:
    """Gather's move this decision, or no intents with nothing to work.

    Sets ``m.gather_status`` from results, not intents alone. With intents:
    ``TAKING`` for a pile ``Take``, ``WALKING`` to a pile; else ``NO_EFFECT``
    when standing where cuts are known not to work, or the latest cut had no
    effect in this region and none worked since; else ``WALKING`` and the
    target for any other walk; else ``CUTTING``. With none, ``BLOCKED`` when
    known grass is barred only by a hostile near it, ``REGION_BARREN`` when
    standing in a skipped barren region, else ``NONE_CUTTABLE``. Any status
    but ``CUTTING`` adds how long it has been when no cut has taken effect
    for ``STALL_SECONDS`` (``STALLED``).
    """
    here = w.pos
    if here is None:
        return StateOutcome(None, "position unknown", state=state)
    g = ground(w, m, policy, knowledge, op, gem_cuts)
    out = _work(w, m, policy, knowledge, g, state)
    walk = m.gather_target[0] if m.gather_target is not None and m.goal == GOAL else None
    verb = out.intents[0].get("verb") if out.intents else None
    if out.wait and m.gather_target is not None:
        m.gather_status = WAITING.format(WALK_TARGETS[m.gather_target[0]])
    elif out.intents is None:
        if _barred_by_hostile(w, policy, g):
            m.gather_status = BLOCKED
        else:
            m.gather_status = REGION_BARREN if region_of(here) in g.skip - g.dead_regions else NONE_CUTTABLE
    elif verb == "Take":
        m.gather_status = TAKING
    elif walk == PILE:
        m.gather_status = WALKING.format(WALK_TARGETS[PILE])
    elif here in g.dead_cells or region_of(here) in g.dead_regions or (
        gem_cuts is not None and gem_cuts.no_effect_near(w.map_id, here, w.tick)
    ):
        m.gather_status = NO_EFFECT
    elif verb == "SetPosition":
        m.gather_status = WALKING.format(WALK_TARGETS.get(walk or "", "a cell to cut"))
    else:
        m.gather_status = CUTTING
    _note_in_region(w, m, g.named, tick_hz)
    idle = _seconds_without_cut(w, m, gem_cuts, tick_hz)
    if m.gather_status != CUTTING and idle >= STALL_SECONDS:
        m.gather_status = STALLED.format(m.gather_status, idle)
    if g.region is not None:
        m.gather_status = IN_REGION.format(m.gather_status, *region_corner(g.region))
        out.reason = IN_REGION.format(out.reason, *region_corner(g.region))
    return out


def _note_in_region(w: WorldModel, m: Memory, target: tuple[int, int] | None, tick_hz: int) -> None:
    """Keep ``Memory.gather_in_region``: the tick Gather last got nearer its
    target region, or arrived in it. A walk that keeps closing in is never
    a stall; one that is blocked, or Gather in the region with no cut, is.
    ``STALL_SECONDS`` without working the region starts it over."""
    if target is None or w.pos is None:
        return
    d = blocks_to_region(w.pos, target)
    rec = m.gather_in_region
    if rec is None or rec[0] != target or w.tick - rec[3] >= STALL_SECONDS * tick_hz or d < rec[1]:
        rec = (target, d, w.tick, w.tick)
    m.gather_in_region = (target, rec[1], rec[2], w.tick)


def _seconds_without_cut(w: WorldModel, m: Memory, gem_cuts: GemYieldTracker | None, tick_hz: int) -> int:
    """Seconds since the latest cut that took effect, or since this spell of
    Gather began when later. A gap of ``STALL_SECONDS`` between two Gather
    decisions (another op or state ran) starts a new spell."""
    since, seen = m.gather_spell or (w.tick, w.tick)
    if w.tick - seen >= STALL_SECONDS * tick_hz:
        since = w.tick
    m.gather_spell = (since, w.tick)
    return max(0, w.tick - cut_or_since(gem_cuts, since)) // max(1, tick_hz)


def _barred_by_hostile(w: WorldModel, policy: Policy, g: Ground) -> bool:
    """Known grass Gather would cut, but for a hostile near it, in view or remembered."""
    if not any(is_hostile(w, policy, e) for e in w.entities) and not any(
        is_hostile(w, policy, s.entity) for s in w.sightings.values()
    ):
        return False
    return any(
        block in GATHER_BLOCKS
        and p not in g.spent
        and _cut_ground(g, p)
        and block not in policy.avoid_blocks
        and not gather_ground(w, p, policy, g.danger)
        for p, block in w.view.tiles.items()
    )


def _cut_ground(g: Ground, p: Pos) -> bool:
    """``p`` lies where cutting is worth it: not a skipped region, nor ground
    where cuts keep having no effect."""
    return region_of(p) not in g.skip and p not in g.dead_cells


def _work(
    w: WorldModel, m: Memory, policy: Policy, knowledge: KnowledgeBase | None, g: Ground, state: str
) -> StateOutcome:
    """Take or cut what is in reach when no walk is cheaper (``_in_reach``),
    else take a step toward the target: the committed one while it is kept
    (A71), else the cheapest."""
    here = w.pos
    assert here is not None
    _, blocked, costly = plan_sets(w, m, policy, knowledge)
    params = cache(lambda: _walk_params(w, policy, g, blocked, costly))
    if g.region is not None and not _reaches_region(w, m, g, params(), blocked):
        g.lift_region()  # the region holds Gather only while a way reaches something there
    if m.gather_target not in g.targets:  # a target that no longer qualifies is let go, whoever walked last
        if m.goal == GOAL:
            m.path, m.goal = [], ""
        m.gather_target = None
    if m.goal == GOAL and not route_clear(w, policy, m.path, g.danger):
        m.path, m.goal = [], ""  # the way ahead entered a known reach: a way round, or the target goes
    near = _in_reach(w, m, g, params, blocked)
    if near is not None and near[0] == PILE:
        pile = min((e for e in w.entities if is_gem_pile(e) and e.pos == near[1]), key=lambda e: e.id)
        return StateOutcome([take(pile.id)], f"take {pile.code or pile.id}", state=state)
    if near is not None:
        return _cut(w, m, knowledge, here, "cut grass", state)
    step = next_step(w, blocked, m.path) if m.goal == GOAL else None
    if step is None:
        if _replan(w, m, g, params(), blocked):
            assert m.gather_target is not None
            return StateOutcome(None, f"gather → {m.gather_target[1]}: way taken, waiting", state=state, wait=True, progress=False)
        step = next_step(w, blocked, m.path) if m.goal == GOAL else None
    if step is not None:
        return StateOutcome([set_position(step)], f"gather → {m.path[-1]}", state=state)
    return StateOutcome(None, "no gather target", state=state)


def _reaches_region(w: WorldModel, m: Memory, g: Ground, params: CostGridParams, blocked: set[Pos]) -> bool:
    """A target in the target region is in reach or a way reaches it: the
    committed one, or any other there."""
    if _reach(w, g):
        return True
    kept = m.gather_target
    if kept in g.targets and ((m.goal == GOAL and next_step(w, blocked, m.path)) or _path_to(w, kept, params)):
        return True
    return _cheapest(w, g, params, blocked) is not None


def _walk_params(w: WorldModel, policy: Policy, g: Ground, blocked: set[Pos], costly: set[Pos]) -> CostGridParams:
    """The grid every Gather walk is planned on: known hostiles' reach is a
    wall (``route_bar``) and costly, and ground near a hostile adds its steps
    (``Danger.priced_cells``)."""
    params = grid_params(policy, blocked | route_bar(w, policy, g.danger), costly | reach_cells(w, policy, g.danger))
    params.priced = g.danger.priced_cells()
    return params


def _in_reach(
    w: WorldModel, m: Memory, g: Ground, params: Callable[[], CostGridParams], blocked: set[Pos]
) -> Target | None:
    """The target in reach (a gem pile beside the character, the grass it
    stands on) to work now, or None. It costs no walk, so it is worked
    unless the walk Gather would take, the committed one or else the
    cheapest, costs less in all: poor grass underfoot is not cut on the way
    to better ground. Every target costs at least a step per cell of
    straight-line distance, so the walk is priced only when that could be
    cheaper."""
    here = w.pos
    assert here is not None
    reach = _reach(w, g)
    if not reach:
        return None
    best = min(reach, key=lambda t: (reach[t], t))
    floor = min((v + max(1, chebyshev(t[1], here)) for t, v in g.targets.items() if t not in reach), default=None)
    if floor is None or reach[best] <= floor:
        return best
    walk = _walk_price(w, m, g, params(), blocked)
    return best if walk is None or reach[best] <= walk else None


def _reach(w: WorldModel, g: Ground) -> dict[Target, float]:
    """The targets in reach, with their work: a gem pile beside the character, the grass it stands on."""
    here = w.pos
    assert here is not None
    return {t: v for t, v in g.targets.items() if (t[0] == PILE and chebyshev(t[1], here) <= 1) or t == (GRASS, here)}


def _walk_price(w: WorldModel, m: Memory, g: Ground, params: CostGridParams, blocked: set[Pos]) -> float | None:
    """The price of the walk Gather would take: to its committed target, else
    to the cheapest one; None with no walk."""
    kept = m.gather_target
    if kept is not None:
        path = m.path if m.goal == GOAL and m.path else _path_to(w, kept, params)
        if path:
            return _price(path, params) + g.targets[kept]
    found = _cheapest(w, g, params, blocked)
    return found[2] if found is not None else None


def _replan(w: WorldModel, m: Memory, g: Ground, params: CostGridParams, blocked: set[Pos]) -> bool:
    """Plan to the committed target while it is kept, else commit to the
    cheapest (``_cheapest``); leave ``m.path`` alone with none.

    A kept target is given up when no way reaches it, when its walk was let
    go for its price (``_outpriced``, A109), or when its first step has been
    taken for ``HOLD_TICKS``; until then it is held, and True says so: the
    caller waits instead of letting the safe default walk away and back
    (A71). A failed plan keeps another state's path.
    """
    kept = m.gather_target
    if m.goal == GOAL:
        m.path, m.goal = [], ""
    m.gather_target = None
    if kept is not None:
        path = _path_to(w, kept, params)
        if path and _outpriced(w, m, g, kept, path, params, blocked):
            path = None
        held = _keep(w, m, kept, path, blocked)
        if held is not None:
            return held
    found = _cheapest(w, g, params, blocked)
    if found is not None:
        m.path, m.goal, m.gather_target = found[1], GOAL, found[0]
        m.gather_walk = (found[0], (w.pos, *found[1]))
    return False


def _cheapest(
    w: WorldModel, g: Ground, params: CostGridParams, blocked: set[Pos], leave_out: Target | None = None
) -> tuple[Target, list[Pos], float] | None:
    """The cheapest target, its path and its price: its walk (``_price``)
    plus its work (``GemOdds.work``).

    Targets of one work (one kind in one region) differ only in their walk,
    so each such set is searched once for its cheapest walk (``_nearest``).
    A walk takes at least a step per cell of straight-line distance, so the
    sets go cheapest that floor first, and the search stops once no floor
    left can beat the best price."""
    here = w.pos
    assert here is not None
    by_work: dict[float, dict[Pos, Target]] = {}
    for t, work in g.targets.items():
        if t != leave_out and t[1] != here:
            by_work.setdefault(work, {}).setdefault(t[1], t)
    floors = sorted((work + min(chebyshev(p, here) for p in cells), work) for work, cells in by_work.items())
    best: tuple[Target, list[Pos], float] | None = None
    for floor, work in floors:
        if best is not None and floor >= best[2]:
            break
        cells = by_work[work]
        found = _nearest(w, cells, params, blocked)
        if found is None:
            continue
        price = _price(found[1], params) + work
        if best is None or price < best[2]:
            best = (cells[found[0]], found[1], price)
    return best


def _nearest(
    w: WorldModel, cells: dict[Pos, Target], params: CostGridParams, blocked: set[Pos]
) -> tuple[Pos, list[Pos]] | None:
    """The cheapest of ``cells`` to walk to, and its path, when its first
    step is open; the ``GATHER_CANDIDATES`` nearest are searched, a cell in
    priced ground its ``params.priced`` steps further."""
    here = w.pos
    assert here is not None
    if any(t[0] == REGION for t in cells.values()):
        (goal,) = cells
        path = cost_path(w, goal, params)
        return (goal, path) if path and next_step(w, blocked, path) else None
    priced = params.priced
    near = set(sorted(cells, key=lambda p: (chebyshev(p, here) + priced.get(p, 0), p))[:GATHER_CANDIDATES])
    found = nearest_target(w, near, params)
    return found if found and next_step(w, blocked, found[1]) else None


def _path_to(w: WorldModel, target: Target, params: CostGridParams) -> list[Pos] | None:
    """A path onto a kept target; None when nothing reaches it."""
    if target[0] == REGION:
        return cost_path(w, target[1], params) or None
    found = nearest_target(w, {target[1]}, params)
    return found[1] if found and found[1] else None


def _price(path: list[Pos], params: CostGridParams) -> int:
    """A walk's price: its steps, and the steps priced ground adds to each
    cell of it, the target's included (``Danger.priced_cells``)."""
    return len(path) + sum(params.priced.get(p, 0) for p in path)


def _outpriced(
    w: WorldModel, m: Memory, g: Ground, kept: Target, path: list[Pos], params: CostGridParams, blocked: set[Pos]
) -> bool:
    """The kept target's price on ``path`` is more than the rest of the walk
    it was last priced on, from the cell the character stands on (a wall
    found ahead, a long way round), and more than the cheapest other target
    by a clear margin (``targets.outpriced``, A109). The others are priced
    only on such a rise. Standing off that walk (another state walked the
    character away) is no rise. Either way the walk on ``path`` becomes the
    price the next replan measures against (``m.gather_walk``)."""
    work = g.targets[kept]
    walk = m.gather_walk[1] if m.gather_walk is not None and m.gather_walk[0] == kept else ()
    m.gather_walk = (kept, (w.pos, *path))
    here = w.pos
    last = _price(list(walk[walk.index(here) + 1 :]), params) + work if here in walk else None

    def cheapest() -> float | None:
        found = _cheapest(w, g, params, blocked, leave_out=kept)
        return found[2] if found is not None else None

    return targets_mod.outpriced(_price(path, params) + work, last, cheapest)


def _keep(w: WorldModel, m: Memory, kept: Target, path: list[Pos] | None, blocked: set[Pos]) -> bool | None:
    """Keep ``kept`` on ``path`` (a way to it, or None): commit to it with
    its first step open (False), hold it while that step stays taken for
    under ``HOLD_TICKS`` (True, A71); None to pick again."""
    if path and next_step(w, blocked, path):
        m.path, m.goal, m.gather_target = path, GOAL, kept
        m.gather_hold = None
        return False
    if path:
        if m.gather_hold is None or m.gather_hold[0] != kept:
            m.gather_hold = (kept, w.tick)
        if w.tick - m.gather_hold[1] < HOLD_TICKS:
            m.gather_target = kept
            return True
    m.gather_hold = None  # no way there, or the way stayed taken: pick again
    return None


def _steps_to_grass(w: WorldModel) -> float:
    """Steps past the edge of unexplored ground to grass: grass is expected
    there as often as on the ground known, one cell in (walkable cells + 1)
    / (grass cells + 1) of this map. An edge stops being one once Gather
    walks up to it: the terrain read there fills every cell of the
    perception window, ground or none (``WorldModel.apply_terrain``)."""
    view = w.view
    walkable = sum(1 for p in view.tiles if view.walkable(p))
    grass = sum(1 for block in view.tiles.values() if block in GATHER_BLOCKS)
    return (walkable + 1) / (grass + 1)


def _seen_region(w: WorldModel, region: tuple[int, int]) -> bool:
    """Any cell of ``region`` known."""
    x0, y0 = region_corner(region)
    tiles = w.view.tiles
    return any((x0 + dx, y0 + dy) in tiles for dx in range(REGION_SIZE) for dy in range(REGION_SIZE))


def _cut_tool(w: WorldModel, knowledge: KnowledgeBase | None) -> InventorySupply | None:
    """The held supply ``_cut`` arms before it cuts, by the one weapon-to-arm
    rule for cutting (``equip.weapon_to_arm``, A99), or None to cut with what
    is in hand: the armed one is that pick, or nothing held is known to cut."""
    tool = weapon_to_arm(w, knowledge, CUT, hunting_ceiling(w, knowledge))
    return tool if tool is not None and tool.id >= 0 and tool.code != w.armed_code else None


def _cut(w: WorldModel, m: Memory, knowledge: KnowledgeBase | None, p: Pos, reason: str, state: str) -> StateOutcome:
    """``Use`` on ``p`` with a tool that cuts: when what is armed is not known
    to cut (a potion a drink left armed, say), arm a held one that is, in the
    same paced queue. With none held, cut with what is in hand."""
    tool = _cut_tool(w, knowledge)
    if tool is None:
        return StateOutcome([use_block(p)], reason, state=state)
    queue = arm_and_use(w, m, tool.id, use_block(p))
    if not queue:  # the cut's cooldown leaves no room for the Arm and the Use together
        return StateOutcome(None, f"wait out the cooldown to arm {tool.code}, {reason}", state=state, wait=True, progress=False)
    if m.gather_rearm is None and w.armed_code and not heals(w.armed_code) and not does_job(w.armed_code, CUT, knowledge):
        # A weapon that cannot cut goes back once the gather work ends; one
        # that cuts was only bettered. Food or a potion is Heal's to put back.
        m.gather_rearm = (w.armed_code, tool.code)
    return StateOutcome(queue, f"arm {tool.code}, {reason}", state=state, paced=True)


def _rearm_weapon(w: WorldModel, m: Memory) -> StateOutcome:
    """Arm the weapon a cut swapped out, once no ``gather_gems`` op is on top.

    Sent once, and only while the cutting tool is still armed: something
    else arming since (Equip's upgrade, say) is not undone.
    """
    weapon, tool = m.gather_rearm or ("", "")
    m.gather_rearm = None
    supply = held_supply(w, weapon) if w.armed_code == tool else None
    if supply is None:
        return StateOutcome(None, "no gather op", state=GatherState.name)
    return StateOutcome([arm(supply.id)], f"re-arm {weapon}", state=GatherState.name)
