"""Detour: take a valuable off the walk, then resume it (A71, A73).

A reflex that extends **Pickup**: Pickup takes what is in reach; Detour
walks to a valuable that comes into view off the path being walked, and
Pickup takes it once it is in reach. No planner call.

A valuable is a free gem, a life (a code the item table learned, A47), or
food while hurt. Every one in view is priced by the steps going by it adds
to the walk: the walking distance from where the character stands to the
find, then on to the best cell of the route still ahead (the queued steps,
then the path), less the steps the walk takes to that cell anyway
(``extra_steps``, a search over known ground, so a wall between the route
and the find counts). There is no fixed reach: each kind has its own
allowance (``allowance``). A gem pile is worth ``GEM_PILE_STEPS``, and each
gem pile next to it adds ``GEM_CLUSTER_STEPS`` (one stop takes them all);
a life is worth ``LIFE_STEPS``; food while hurt ``FOOD_STEPS``. Hostile
safety is Gather's: the find must be on ground ``gather_ground`` allows
(off hazards, clear of every known hostile's bar), and the survival
reflexes above it still win.

A detour is a short insert, never a new target: the walk it interrupts
keeps its committed target (``targets``). Once the find is taken, seen gone
or given up (no step, or ``DETOUR_TICKS`` without taking it, after which
that find is skipped for the run), the state that was walking plans again
to the same target.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

from ..healing import FOOD_CODES, hurt
from ..knowledge_base import knowledge_items
from ..memory import Memory
from ..loot import GEM_SUPPLY_CODES, Pickup, is_life_supply, loot_score, pickup_room
from ..navigation import cost_path
from ..navigation.rejection import navigation_avoid_costly
from ..pathing import bounded_step, grid_params, nav_search, route_ahead
from ..survival import hostile_reach
from ..world import DOORS, NEIGHBOURS, Entity, Pos, WorldModel, chebyshev
from .base import PlayContext, State, StateOutcome
from .gather_safe import gather_ground
from .intents import set_position

GOAL = "detour"
# The most steps going by a find may add to the walk, by kind (A73; free-play
# run 4 walked past a gem triple 5 cells off that cost about 7).
GEM_PILE_STEPS = 8
# Each gem pile next to the find adds this much: one stop takes them all.
GEM_CLUSTER_STEPS = 4
# A cluster counts at most this many piles.
GEM_CLUSTER_MAX = 3
LIFE_STEPS = 16
FOOD_STEPS = 4
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


def allowance(w: WorldModel, e: Entity, finds: list[Entity], items: dict) -> int:
    """The most steps going by ``e`` may add to the walk: the best of what it is.

    ``finds`` are the valuables in view; the gem piles among them next to a
    gem pile ``e`` make it a cluster."""
    steps = 0
    if e.code in GEM_SUPPLY_CODES:
        piles = sum(1 for f in finds if f.code in GEM_SUPPLY_CODES and chebyshev(f.pos, e.pos) <= 1)
        steps = GEM_PILE_STEPS + GEM_CLUSTER_STEPS * (min(piles, GEM_CLUSTER_MAX) - 1)
    if is_life_supply(e.code, items):
        steps = max(steps, LIFE_STEPS)
    if hurt(w) and e.code in FOOD_CODES:
        steps = max(steps, FOOD_STEPS)
    return steps


def extra_steps(w: WorldModel, here: Pos, find: Pos, route: list[Pos], limit: int, avoid: Collection[str] = ()) -> int | None:
    """Steps going by ``find`` adds to walking ``route`` from ``here``,
    rejoining it at its best cell; None when that is more than ``limit``.

    ``route`` must start at the step after ``here`` (``route_ahead``): cell i
    is i + 1 steps away. Distances are walks over known ground (walkable or a
    door, never a tile in ``avoid``; the route's own cells count as open),
    searched from ``find`` no further than the straight line to ``here``
    plus ``limit``. A find beside the route costs at most 2."""
    if not route:
        return None
    # Straight lines never overestimate a walk: past the limit on them, no search.
    reach = chebyshev(here, find)
    if reach + min(chebyshev(find, p) - (i + 1) for i, p in enumerate(route)) > limit:
        return None
    on_route = set(route) | {here}

    def open_(p: Pos) -> bool:
        tile = w.view.tiles.get(p)
        return p in on_route or (tile not in avoid and (w.view.walkable(p) or tile in DOORS))

    dist, frontier = {find: 0}, [find]
    for steps in range(1, reach + limit + 1):
        nxt = []
        for x, y in frontier:
            for dx, dy in NEIGHBOURS:
                p = (x + dx, y + dy)
                if p not in dist and open_(p):
                    dist[p] = steps
                    nxt.append(p)
        frontier = nxt
        if not frontier:
            break
    if here not in dist:
        return None
    rejoin = [dist[p] - (i + 1) for i, p in enumerate(route) if p in dist]
    if not rejoin:
        return None
    extra = dist[here] + min(rejoin)
    return extra if extra <= limit else None


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
    finds = [e for e in w.entities if valuable(w, e, items)]
    best: tuple[int, int, Entity] | None = None
    for e in finds:
        if e.id in m.detour_skipped or chebyshev(e.pos, here) <= 1 or not gather_ground(w, e.pos, ctx.policy):
            continue  # in reach is Pickup's
        extra = extra_steps(w, here, e.pos, route, allowance(w, e, finds, items), ctx.policy.avoid_blocks)
        if extra is None:
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
