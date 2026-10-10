"""Detour: take a valuable a few steps off the walk, then resume it (A71).

A reflex that extends **Pickup**: Pickup takes what is in reach; Detour
walks to a valuable that comes into view a few steps off the path being
walked, and Pickup takes it once it is in reach. No planner call.

A valuable is a free gem, a life (a code the item table learned, A47), or
food while hurt. It is a detour when it lies within ``DETOUR_REACH`` of a
cell of the route still ahead (the queued steps, then the path) and going
by it adds at most ``DETOUR_EXTRA_STEPS`` steps, counted from where the
character stands. Hostile safety is Gather's: the find must be
on ground ``gather_ground`` allows (off hazards, clear of every known
hostile's bar), and the survival reflexes above it still win.

The walk it interrupts keeps its committed target (``targets``): once the
find is taken, seen gone or given up (no step, or ``DETOUR_TICKS`` without
taking it, after which that find is skipped for the run), the state that was
walking plans again to the same target.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..healing import FOOD_CODES, hurt
from ..knowledge_base import knowledge_items
from ..memory import Memory
from ..loot import GEM_SUPPLY_CODES, Pickup, is_life_supply, loot_score, pickup_room
from ..navigation import cost_path
from ..navigation.rejection import navigation_avoid_costly
from ..pathing import bounded_step, grid_params, nav_search, route_ahead
from ..survival import hostile_reach
from ..world import Entity, Pos, WorldModel, chebyshev
from .base import PlayContext, State, StateOutcome
from .gather_safe import gather_ground
from .intents import set_position

GOAL = "detour"
# A find may lie this far from a cell of the path being walked.
DETOUR_REACH = 3
# Going by the find may add at most this many steps to the walk.
DETOUR_EXTRA_STEPS = 4
# A detour that has not taken its find in this long is given up (10 s at 10 ticks/s).
DETOUR_TICKS = 100


@dataclass
class Detour:
    """The find a detour walks to, and the walk it left."""

    supply_id: int
    pos: Pos
    code: str
    resumes: str  # ``Memory.goal`` of the walk it left
    since: int  # tick it began


class DetourState(State):
    """Reflex, after Recover. With ``policy.pickup`` on: a valuable a few
    steps off the walk under way is walked to, and the walk resumes after."""

    name = "Detour"

    def guard(self, world: WorldModel, ctx: PlayContext) -> bool:
        if ctx.policy.kind != "scripted" or not ctx.policy.pickup or not world.alive or world.pos is None:
            return False
        return ctx.memory.detour is not None or detour_find(world, ctx) is not None

    def done(self, world: WorldModel, ctx: PlayContext) -> bool:
        return not self.guard(world, ctx)

    def act(self, world: WorldModel, ctx: PlayContext) -> StateOutcome:
        m = ctx.memory
        d = m.detour
        if d is not None and not _still_on(world, d):
            if world.tick - d.since >= DETOUR_TICKS:
                m.detour_skipped.add(d.supply_id)  # timed out: given up for the run, never re-picked
            _end(m, d)
            d = None
        if d is None:
            find = detour_find(world, ctx)
            if find is None:
                return StateOutcome(None, "no find off the walk", state=self.name)
            d = m.detour = Detour(find.id, find.pos, find.code, m.goal, world.tick)
        step = _step_toward(world, ctx, d.pos)
        if step is None:
            m.detour_skipped.add(d.supply_id)
            _end(m, d)
            return StateOutcome(None, f"no step to {d.code} at {d.pos}", state=self.name)
        back = f", then back to {d.resumes}" if d.resumes else ""
        return StateOutcome([set_position(step)], f"detour → {d.code} at {d.pos}{back}", reflex=True, state=self.name)


def valuable(w: WorldModel, e: Entity, items: dict) -> bool:
    """A free ground supply worth a detour that fits: a gem, a life, or food while hurt."""
    if e.kind != "supply" or e.gem_price is not None or not e.code:
        return False
    if not (e.code in GEM_SUPPLY_CODES or is_life_supply(e.code, items) or (hurt(w) and e.code in FOOD_CODES)):
        return False
    return pickup_room(w, Pickup(e.id, e.code, e.pos, None, loot_score(e.code, items)), items) is not False


def extra_steps(here: Pos, find: Pos, path: list[Pos]) -> int | None:
    """Steps going by ``find`` adds to walking ``path`` from ``here``, rejoining
    it at the best cell within ``DETOUR_REACH`` of ``find``; None when no cell is.

    ``path`` must start at the step after ``here`` (``route_ahead``): cell i
    is i + 1 steps away. A find beside the route then costs at most 2."""
    to_find = chebyshev(here, find)
    costs = [to_find + chebyshev(find, p) - (i + 1) for i, p in enumerate(path) if chebyshev(find, p) <= DETOUR_REACH]
    return min(costs) if costs else None


def detour_find(w: WorldModel, ctx: PlayContext) -> Entity | None:
    """The valuable worth a detour off the walk under way: fewest extra steps, then id."""
    m = ctx.memory
    here = w.pos
    if here is None or m.goal in ("", GOAL):
        return None
    route = route_ahead(w, m)  # the queued steps too: priced from where we stand
    if not route:
        return None
    items = knowledge_items(ctx.knowledge)
    best: tuple[int, int, Entity] | None = None
    for e in w.entities:
        if e.id in m.detour_skipped or chebyshev(e.pos, here) <= 1 or not valuable(w, e, items):
            continue  # in reach is Pickup's
        extra = extra_steps(here, e.pos, route)
        if extra is None or extra > DETOUR_EXTRA_STEPS or not gather_ground(w, e.pos, ctx.policy):
            continue
        if best is None or (extra, e.id) < best[:2]:
            best = (extra, e.id, e)
    return best[2] if best is not None else None


def _still_on(w: WorldModel, d: Detour) -> bool:
    """The find is still there and the detour has time left."""
    there = any(e.kind == "supply" and e.id == d.supply_id for e in w.entities)
    return there and w.tick - d.since < DETOUR_TICKS


def _end(m: Memory, d: Detour) -> None:
    """The find is taken, gone or given up: the walk it left resumes toward its target."""
    if m.goal == GOAL:
        m.path, m.goal = [], ""
    m.detour = None


def _step_toward(w: WorldModel, ctx: PlayContext, at: Pos) -> Pos | None:
    """A bounded walk to ``at`` (``bounded_step``) that goes round every known hostile's reach."""
    m, policy = ctx.memory, ctx.policy
    nav_avoid, nav_costly = navigation_avoid_costly(m.nav, ctx.knowledge, w.map_id, w.tick)
    hazards = {p for p, b in w.view.tiles.items() if b in policy.avoid_blocks}
    avoid, costly = nav_avoid | hazards, nav_costly | hazards | hostile_reach(w, policy)

    def params():
        return grid_params(policy, avoid, costly)

    def plan() -> list[Pos] | None:
        return cost_path(w, at, params(), nav=nav_search(m, w, GOAL, at))

    return bounded_step(m, w, GOAL, at, avoid, plan, params=params)
