"""Gather: carry out the plan's ``gather_gems`` op from grass, bushes and gem piles (A22).

It works any known ground off hazards with no known hostile near
(``gather_ground``), the open field and safe zones alike, but cuts field
cells (not known safe) ahead of safe ones: a cut on town grass was seen to
have no effect (A63 run 2). With nothing to cut while standing on safe
ground it walks out to the nearest known field ground, else the nearest
frontier. Grass and bushes in a region our own cuts showed barren are left
alone (A63), unless the op names that region with ``x, y``, and so are cells
it cut too recently to have grown back, cells whose last ``Use`` had no
effect, and regions or safe zones where cuts keep having none
(``GemYieldTracker.uncuttable``)."""

from __future__ import annotations

from typing import Callable, Iterable

from ..config import Policy
from ..gem_yield import GemYieldTracker, barren_regions, exhausted_cells, region_of
from ..knowledge_base import KnowledgeBase
from ..memory import Memory
from ..navigation import cost_path, nearest_target
from ..pathing import grid_params, next_step
from ..plan import GoalOp
from ..world import Entity, Pos, WorldModel, chebyshev
from ..zone_discovery import safe_tiles
from .base import PlayContext, State, StateOutcome, my_op
from .explore import plan_sets, safe_default
from .gather_safe import gather_ground
from .intents import set_position, take, use_block

# Authored gem piles spawn as ground supplies (Obs, GAME_NOTES.md Gems).
# Gem caches (gem_cache_5/7/10) are a different drop and are not piles.
# A priced supply with the same code is shop stock: taking it is a purchase.
GEM_PILE_SUPPLY_CODES: frozenset[str] = frozenset({"gem"})
# Bushes are not walkable, so they are cut from a neighbouring cell: the
# pocket knife's range is 1 (GAME_NOTES.md Olympuff starting kit).
BUSH_REACH = 1
GOAL = "gather"
OUT = "out"  # ``m.gather_target`` kind: walking off safe ground to field ground or the frontier
# Known targets tried per replan, nearest first: bounds the searches when the
# closest ones turn out unreachable (across water, say).
GATHER_CANDIDATES = 16
# Gather's last decision, for the planner's State (``Memory.gather_status``).
CUTTING = "cutting"
NO_EFFECT = "cuts have no effect here"
HEADING_OUT = "heading out of safe ground"
NONE_CUTTABLE = "no cuttable cell in view"
REGION_BARREN = "region barren"


class GatherState(State):
    """Executor for ``gather_gems``: ``Use`` grass and bushes or ``Take`` gem
    piles until the gem counter reaches the op's count, walking to the
    nearest known one when none is in reach, field cells before safe ones.
    Grass and bushes in a barren region (unless the op names it), or where
    cuts had no effect, are skipped. On safe ground with nothing to cut, it
    heads out to field ground or the frontier;
    with nowhere to head, it explores (the safe default) to reveal more."""

    name = "Gather"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        if ctx.policy.kind != "scripted" or not world.alive or world.pos is None:
            return False
        return my_op(ctx, self.name) is not None

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not self.guard(world, ctx)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        op = my_op(ctx, self.name)
        out = gather_outcome(world, ctx.memory, ctx.policy, knowledge=ctx.knowledge, op=op, gem_cuts=ctx.gem_cuts)
        if out.intents is not None:
            return out
        out = safe_default(world, ctx)
        out.state, out.reason = self.name, f"look for gems: {out.reason}"
        return out


def is_gem_pile(e: Entity) -> bool:
    """A free ground gem: an authored pile, or one a grass or bush drop left."""
    return (
        e.kind == "supply"
        and e.code in GEM_PILE_SUPPLY_CODES
        and e.gem_price is None
    )


def gather_outcome(
    w: WorldModel,
    m: Memory,
    policy: Policy,
    *,
    knowledge: KnowledgeBase | None = None,
    op: GoalOp | None = None,
    gem_cuts: GemYieldTracker | None = None,
    state: str = "Gather",
) -> StateOutcome:
    """Gather's move this decision, or no intents with nothing to work.

    Sets ``m.gather_status`` from results, not intents alone. With intents:
    ``HEADING_OUT`` when walking off safe ground; ``CUTTING`` for a pile
    ``Take`` or walk; else ``NO_EFFECT`` when standing where cuts are known
    not to work, or the latest cut had no effect in this region and none
    worked since; else ``CUTTING``. With none, ``REGION_BARREN`` when
    standing in a skipped barren region, else ``NONE_CUTTABLE``.
    """
    here = w.pos
    if here is None:
        return StateOutcome(None, "position unknown", state=state)
    skip = _barren_to_skip(w, knowledge, op)
    safe = safe_tiles(w, w.map_id) if w.map_id is not None else set()
    dead_regions, dead_cells = gem_cuts.uncuttable(w, safe) if gem_cuts is not None else (set(), set())
    exhausted = exhausted_cells(knowledge, w.map_id, w.tick, gem_cuts) | dead_cells
    out = _gather_step(w, m, policy, here, skip | dead_regions, exhausted, safe, knowledge, state)
    if out.intents is None:
        m.gather_status = REGION_BARREN if region_of(here) in skip else NONE_CUTTABLE
    elif out.intents[0].get("verb") == "SetPosition" and m.gather_target is not None and m.gather_target[0] == OUT:
        m.gather_status = HEADING_OUT
    elif out.intents[0].get("verb") == "Take" or (m.gather_target is not None and m.gather_target[0] == "pile"):
        m.gather_status = CUTTING
    elif here in dead_cells or region_of(here) in dead_regions or (
        gem_cuts is not None and gem_cuts.no_effect_near(w.map_id, here, w.tick)
    ):
        m.gather_status = NO_EFFECT
    else:
        m.gather_status = CUTTING
    return out


def _gather_step(
    w: WorldModel,
    m: Memory,
    policy: Policy,
    here: Pos,
    skip: set[tuple[int, int]],
    exhausted: set[Pos],
    safe: set[Pos],
    knowledge: KnowledgeBase | None,
    state: str,
) -> StateOutcome:
    view = w.view
    cuttable = {
        p
        for p, block in view.tiles.items()
        if block in ("grass", "bush") and p not in exhausted and region_of(p) not in skip and gather_ground(w, p, policy)
    }
    # Field cells first; safe ones only when no field cell is left to cut.
    preferred = {p for p in cuttable if p not in safe} or cuttable

    _, plan_avoid, plan_costly = plan_sets(w, m, policy, knowledge)
    piles = [e for e in w.entities if is_gem_pile(e) and chebyshev(e.pos, here) <= 1 and gather_ground(w, e.pos, policy)]
    if piles:
        _end_walk_out(m)
        s = min(piles, key=lambda e: (chebyshev(e.pos, here), e.id))
        return StateOutcome([take(s.id)], f"take {s.code or s.id}", state=state)

    if view.tiles.get(here) == "grass" and here in preferred:
        _end_walk_out(m)
        return StateOutcome([use_block(here)], "cut grass", state=state)

    bushes = [p for p in preferred if view.tiles[p] == "bush" and chebyshev(p, here) <= BUSH_REACH]
    if bushes:
        _end_walk_out(m)
        p = min(bushes, key=lambda pos: (chebyshev(pos, here), pos))
        return StateOutcome([use_block(p)], "cut bush", state=state)

    # Follow only a path Gather planned, toward a target that still qualifies.
    if m.goal == GOAL and not _still_wanted(w, m.gather_target, policy, preferred, here in safe):
        m.path, m.goal, m.gather_target = [], "", None
    step = next_step(w, plan_avoid, m.path) if m.goal == GOAL else None
    if step is None:
        _replan_gather(w, m, policy, plan_avoid, plan_costly, preferred, safe)
        step = next_step(w, plan_avoid, m.path) if m.goal == GOAL else None
    if step is not None:
        return StateOutcome([set_position(step)], f"gather → {m.path[-1]}", state=state)

    return StateOutcome(None, "no gather target", state=state)


def _end_walk_out(m: Memory) -> None:
    """Something to cut or take turned up: a walk out of safe ground is over."""
    if m.goal == GOAL and m.gather_target is not None and m.gather_target[0] == OUT:
        m.path, m.goal, m.gather_target = [], "", None


def _barren_to_skip(w: WorldModel, knowledge: KnowledgeBase | None, op: GoalOp | None) -> set[tuple[int, int]]:
    """Barren regions of this map (A63), less the one the op names with ``x, y``."""
    skip = barren_regions(knowledge, w.map_id)
    if skip and op is not None and "x" in op and "y" in op:
        skip.discard(region_of((op["x"], op["y"])))
    return skip


def _still_wanted(
    w: WorldModel, target: tuple[str, Pos] | None, policy: Policy, preferred: set[Pos], on_safe: bool
) -> bool:
    if target is None:
        return False
    kind, pos = target
    if kind == "pile":
        return any(is_gem_pile(e) and e.pos == pos for e in w.entities) and gather_ground(w, pos, policy)
    if kind == OUT:
        # Kept while on safe ground: it was planned because no cut was
        # reachable, so re-checking ``preferred`` each tick would only replan
        # it (A15). Off safe ground the field is in view: replan to cut it.
        return on_safe
    return w.view.tiles.get(pos) == kind and pos in preferred


def _replan_gather(
    w: WorldModel,
    m: Memory,
    policy: Policy,
    blocked: set[Pos],
    costly: set[Pos],
    preferred: set[Pos],
    safe: set[Pos],
) -> None:
    """Plan to the nearest pile, then bush, then grass (``preferred``: field
    cells before safe ones), then, on safe ground, out to field ground or the
    frontier; leave ``m.path`` alone if none.

    The nearest ``GATHER_CANDIDATES`` of each kind are tried. A failed plan
    keeps another state's path.
    """
    if m.goal == GOAL:
        m.path, m.goal, m.gather_target = [], "", None
    params = grid_params(policy, blocked, costly)
    here = w.pos
    assert here is not None

    piles = [e for e in w.entities if is_gem_pile(e) and gather_ground(w, e.pos, policy)]
    if piles:
        target = min(piles, key=lambda e: (chebyshev(e.pos, here), e.id)).pos
        path = cost_path(w, target, params)
        if next_step(w, blocked, path):
            m.path, m.goal, m.gather_target = path, GOAL, ("pile", target)
            return

    bush_at: dict[Pos, Pos] = {}
    for p in sorted(preferred):
        if w.view.tiles[p] != "bush":
            continue
        for stand in w.neighbours(p):
            if w.view.walkable(stand) and stand not in w.occupied():
                bush_at.setdefault(stand, p)
    if bush_at:
        found = nearest_target(w, _nearest(here, bush_at), params)
        if found and next_step(w, blocked, found[1]):
            m.path, m.goal, m.gather_target = found[1], GOAL, ("bush", bush_at[found[0]])
            return

    grass = {p for p in preferred if w.view.tiles[p] == "grass"}
    if grass:
        found = nearest_target(w, _nearest(here, grass), params)
        if found and next_step(w, blocked, found[1]):
            m.path, m.goal, m.gather_target = found[1], GOAL, ("grass", found[0])
            return

    if here in safe:
        _plan_out(w, m, policy, blocked, params, safe)


def _plan_out(w: WorldModel, m: Memory, policy: Policy, blocked: set[Pos], params, safe: set[Pos]) -> None:
    """Head off safe ground: to the nearest known walkable field cell, else the nearest frontier."""
    here = w.pos
    assert here is not None
    view = w.view
    field = {p for p in view.tiles if p not in safe and view.walkable(p) and gather_ground(w, p, policy)}
    frontier = {p for p in view.frontier() if p != here and gather_ground(w, p, policy)}
    for cells in (field, frontier):
        if not cells:
            continue
        found = nearest_target(w, _nearest(here, cells), params)
        if found and next_step(w, blocked, found[1]):
            m.path, m.goal, m.gather_target = found[1], GOAL, (OUT, found[0])
            return


def _nearest(here: Pos, cells: Iterable[Pos]) -> set[Pos]:
    """The ``GATHER_CANDIDATES`` cells closest to ``here`` (ties to the smaller cell)."""
    return set(sorted(cells, key=lambda p: (chebyshev(p, here), p))[:GATHER_CANDIDATES])
