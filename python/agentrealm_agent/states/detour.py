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
(off hazards, clear of every known hostile's bar, in view or remembered),
the path walked there must stay out of every known hostile's reach
(``route_clear``), faded ground the find lies in counts its steps against
the allowance (``Danger.price``), and the survival reflexes above it
still win. A hostile that came out to fight us holds the ground out to
where it did (``WorldModel.note_came_for_us``), so a find it turned us
back from is no longer on ground Gather may work. A find
is priced by risk too (``risk_allowed``): one within ``RISK_RADIUS`` of a
known hostile, in view or remembered, is taken only while one hit from
each of them would leave health above Retreat's floor. A detour is
opportunistic, so it takes no risk the goal did not ask for (A82). A
``gather_gems`` op with ``fight`` on top lifts the remembered, route and
risk tests, as it does for Gather. A find whose ground or path turns out
held (a hit on the way, say), or whose risk health no longer covers, is
dropped, and one with no clear path is skipped for the run (free-play run 5).

A detour is a short insert, never a new target: the walk it interrupts
keeps its committed target (``targets``). Once the find is taken, seen gone
or given up (no step, or ``DETOUR_TICKS`` without taking it, after which
that find is skipped for the run), the state that was walking plans again
to the same target.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

from ..healing import hurt
from ..supplies import is_food
from ..hostile_ground import GATHER_HOSTILE_RADIUS, Danger, danger, hostiles_within, reach_cells
from ..knowledge_base import knowledge_items
from ..memory import Memory
from ..loot import GEM_SUPPLY_CODES, Pickup, is_life_supply, loot_score, pickup_room
from ..navigation import cost_path
from ..navigation.rejection import navigation_avoid_costly
from ..pathing import bounded_step, grid_params, nav_search, route_ahead
from ..survival import health_floor
from ..threat import type_key_for_entity
from ..world import DOORS, NEIGHBOURS, Entity, Pos, WorldModel, chebyshev
from .base import PlayContext, State, StateOutcome, top_op
from .gather_safe import gather_ground, route_clear
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
# A find this close to a known hostile is a risk: the radius in which Gather
# keeps off the hostile that hit us and the path planner prices danger (#162).
RISK_RADIUS = GATHER_HOSTILE_RADIUS


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
        known = danger(world, ctx.policy, chosen_fight(ctx))  # once per decision
        d = m.detour
        if d is not None and not _still_on(world, d, ctx, known):
            if world.tick - d.since >= DETOUR_TICKS:
                m.detour_skipped.add(d.supply_id)  # timed out: given up for the run, never re-picked
            _end(m, d)
            d = None
        if d is None:
            find = detour_find(world, ctx, known)
            if find is None:
                return StateOutcome(None, "no find off the walk", state=self.name)
            d = m.detour = Detour(find.id, find.pos, find.code, m.goal, world.tick)
        step = _step_toward(world, ctx, d.pos, known)
        if step is None or not route_clear(world, ctx.policy, m.path, known):
            m.detour_skipped.add(d.supply_id)
            _end(m, d)
            why = "no step" if step is None else "no way clear of hostiles"
            return StateOutcome(None, f"{why} to {d.code} at {d.pos}", state=self.name)
        back = f", then back to {d.resumes}" if d.resumes else ""
        return StateOutcome([set_position(step)], f"detour → {d.code} at {d.pos}{back}", reflex=True, state=self.name)


def valuable(w: WorldModel, e: Entity, items: dict) -> bool:
    """A free ground supply worth a detour that fits: a gem, a life, or food while hurt."""
    if e.kind != "supply" or e.gem_price is not None or not e.code:
        return False
    if not (e.code in GEM_SUPPLY_CODES or is_life_supply(e.code, items) or (hurt(w) and is_food(e.code))):
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
    if hurt(w) and is_food(e.code):
        steps = max(steps, FOOD_STEPS)
    return steps


def extra_steps(w: WorldModel, here: Pos, find: Pos, route: list[Pos], limit: int, avoid: Collection[str] = ()) -> int | None:
    """Steps going by ``find`` now adds to walking ``route`` from ``here``,
    rejoining it at its best cell; None when that is more than ``limit``, or
    when the walk to ``find`` is longer than the straight line plus ``limit``.

    ``route`` must start at the step after ``here`` (``route_ahead``): cell i
    is i + 1 steps away. Distances are walks over known ground (walkable or a
    door, never a tile in ``avoid``; the route's own cells count as open),
    searched from ``find`` no further than the straight line to ``here``
    plus ``limit``. A find near in a straight line but far on foot (beside a
    later stretch of a route that winds round a wall) is no short insert from
    here: it is priced again as the walk comes by it. A find beside the route
    within that search costs at most 2."""
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
        return None  # not a short insert from here: priced again as the walk comes by
    rejoin = [dist[p] - (i + 1) for i, p in enumerate(route) if p in dist]
    if not rejoin:
        return None
    extra = dist[here] + min(rejoin)
    return extra if extra <= limit else None


def detour_find(w: WorldModel, ctx: PlayContext, known: Danger | None = None) -> Entity | None:
    """The valuable worth a detour off the walk under way, on ground Gather
    may work (``known``, this decision's ``Danger``): fewest extra steps, then
    id. Faded ground a find lies in counts its steps (``Danger.price``)
    against the find's allowance, as it does for Gather's picks."""
    m = ctx.memory
    here = w.pos
    if here is None or m.goal in ("", GOAL):
        return None
    route = route_ahead(w, m)  # the queued steps too: priced from where we stand
    if not route:
        return None
    items = knowledge_items(ctx.knowledge)
    known = known or danger(w, ctx.policy, chosen_fight(ctx))
    finds = [e for e in w.entities if valuable(w, e, items)]
    best: tuple[int, int, Entity] | None = None
    for e in finds:
        if e.id in m.detour_skipped or chebyshev(e.pos, here) <= 1 or not gather_ground(w, e.pos, ctx.policy, known):
            continue  # in reach is Pickup's
        if not risk_allowed(w, ctx, e.pos, known):
            continue
        price = known.price(e.pos)
        extra = extra_steps(w, here, e.pos, route, allowance(w, e, finds, items) - price, ctx.policy.avoid_blocks)
        if extra is None:
            continue
        extra += price
        if best is None or (extra, e.id) < best[:2]:
            best = (extra, e.id, e)
    return best[2] if best is not None else None


def risk_allowed(w: WorldModel, ctx: PlayContext, at: Pos, known: Danger) -> bool:
    """Whether health covers the risk of a find at ``at``.

    With no known hostile within ``RISK_RADIUS`` of it (``hostiles_within``),
    or an op that chose to fight (``known.fight``), there is none. Otherwise
    one hit from each of them (the threat table's size for its type) must
    leave health above Retreat's floor against them
    (``survival.health_floor``). Free-play run 8: a detour at 4/10 to a pile
    2 cells from a pack ended at 1/10.
    """
    if known.fight:
        return True
    near = hostiles_within(w, ctx.policy, at, RISK_RADIUS)
    if not near:
        return True
    if w.health is None:
        return False
    volley = sum(w.threat.damage_per_hit(type_key_for_entity(e)) for e in near)
    return w.health - volley > health_floor(w, ctx.params, near)


def chosen_fight(ctx: PlayContext) -> bool:
    """The top op is a ``gather_gems`` the planner chose to fight for (``fight``)."""
    op = top_op(ctx)
    return op is not None and op["op"] == "gather_gems" and bool(op.get("fight"))


def _still_on(w: WorldModel, d: Detour, ctx: PlayContext, known: Danger) -> bool:
    """The find is still there, on ground Gather may work, its risk still
    covered by health, the path being walked to it is clear of known reach,
    and the detour has time left."""
    m = ctx.memory
    there = any(e.kind == "supply" and e.id == d.supply_id for e in w.entities)
    if not there or w.tick - d.since >= DETOUR_TICKS or not gather_ground(w, d.pos, ctx.policy, known):
        return False
    if not risk_allowed(w, ctx, d.pos, known):
        return False
    return m.goal != GOAL or route_clear(w, ctx.policy, m.path, known)


def _end(m: Memory, d: Detour) -> None:
    """The find is taken, gone or given up: the walk it left resumes toward its target."""
    if m.goal == GOAL:
        m.path, m.goal = [], ""
    m.detour = None


def _step_toward(w: WorldModel, ctx: PlayContext, at: Pos, known: Danger) -> Pos | None:
    """A bounded walk to ``at`` (``bounded_step``) that goes round every known hostile's reach, remembered ones too."""
    m, policy = ctx.memory, ctx.policy
    nav_avoid, nav_costly = navigation_avoid_costly(m.nav, ctx.knowledge, w.map_id, w.tick)
    hazards = {p for p, b in w.view.tiles.items() if b in policy.avoid_blocks}
    avoid, costly = nav_avoid | hazards, nav_costly | hazards | reach_cells(w, policy, known)

    def params():
        return grid_params(policy, avoid, costly)

    def plan() -> list[Pos] | None:
        return cost_path(w, at, params(), nav=nav_search(m, w, GOAL, at))

    return bounded_step(m, w, GOAL, at, avoid, plan, params=params)
