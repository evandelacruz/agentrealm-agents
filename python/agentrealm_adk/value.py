"""One value for every walk target, in gems, and one price for a step (A170).

Every place a walk may go for something has one value, in gems:

- gems lying free: their count (``gem_count``: a gem one, a gem cache the
  number it names; both are eaten into the counter on pickup);
- food, or any heal eaten on pickup: the health it would actually give back,
  ``min(heal, max_health - health)``, at the gems a health point costs as a
  potion (``gems_per_health``). At full health it is worth nothing, so food
  is passed by then by its price, not by a test of its own;
- a life (``supplies.is_life``): ``LIFE_GEMS``;
- any other supply, lying free or in a chest: its price (``prices``): as
  seen for sale in this world (``chest_sources.known_prices``), else the
  Supplies reference's for this world (``supplies.list_price``), and only
  what fits the pack;
- a chest, in view or remembered (her own death chests, kept across runs,
  A122): the sum of what it holds that fits the pack (``chest_value``), as
  seen in reach (``WorldModel.chest_contents``) or expected from the death
  (A167). A chest never seen in reach is no dropped chest of ours, and a
  dropped chest is never empty (the game removes it once emptied), so it is
  worth at least one item at the cheapest price known (``unseen_chest_gems``).

A step costs what the action could have earned instead: the gems a cut
drops on this map (``gem_yield.expected_yield``), the price A105 gives every
step Gather walks. Urgency (a boss clock running, hostiles that can reach the
character with the margin short, an attack under way) raises it; the caller
says how much (``step_gems``'s ``urgency``), so a walk under threat declines
what it would otherwise take, with no test of its own.

A target is chosen by its value per step (``per_step``): its value over the
steps to it plus the one action that takes it. A side find on any walk is
taken when its value beats the steps it adds (``extra_steps``) at the step
price (``detour_gain``). Gary's Detour and Lorraine's walks both read this.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .chest_sources import known_prices
from .gem_yield import GEM_CACHE_PREFIX, expected_yield, regions
from .loot import GEM_SUPPLY_CODES
from .supplies import eaten_on_pickup, heal_of, is_life, is_potion, list_price, sold_codes
from .world import DOORS, NEIGHBOURS, Pos, WorldModel, chebyshev

if TYPE_CHECKING:
    from .knowledge_base import KnowledgeBase

# A life in gems: twice a gem pile, the weight Detour gave one before A170
# (a life's 16 steps to a gem pile's 8). No world prices a life.
LIFE_GEMS = 2.0
# A health point in gems with no potion priced, seen or in the reference:
# a small potion's 10 gems for 10 health.
PRIOR_GEMS_PER_HEALTH = 1.0
# The step price under urgency that declines every find.
URGENT = math.inf


@dataclass(frozen=True)
class Find:
    """One walk target and its value: a supply lying free (``kind``
    ``supply``, ``id`` its supply id) or a chest (``chest``, its chest id),
    on ``map_id`` at ``pos``."""

    kind: str
    id: int
    pos: Pos
    map_id: int | None
    code: str
    gems: float


def prices(w: WorldModel, kb: KnowledgeBase | None) -> dict[str, int]:
    """Each item's price: as seen for sale in this world
    (``chest_sources.known_prices``), else the Supplies reference's for this
    world (``supplies.list_price``), for every item a shop sells somewhere."""
    world = kb.world_code if kb is not None else None
    listed = {c: p for c in sold_codes() if (p := list_price(c, world)) is not None}
    return {**listed, **known_prices(w, kb)}


def gems_per_health(w: WorldModel, kb: KnowledgeBase | None, seen: Mapping[str, int] | None = None) -> float:
    """What a health point costs as a potion: the cheapest potion's price
    (``prices``) over its heal; ``PRIOR_GEMS_PER_HEALTH`` with none priced."""
    seen = prices(w, kb) if seen is None else seen
    rates = [p / heal_of(c) for c, p in seen.items() if is_potion(c) and heal_of(c) and p > 0]
    return min(rates) if rates else PRIOR_GEMS_PER_HEALTH


def gem_count(code: str | None) -> int:
    """The gems a supply puts on the counter: a gem one, a gem cache the
    number its code names (``gem_cache_5``, Manual Gem caches), anything else none."""
    if code in GEM_SUPPLY_CODES:
        return 1
    if code and code.startswith(GEM_CACHE_PREFIX):
        n = code.removeprefix(GEM_CACHE_PREFIX).strip("_")
        return int(n) if n.isdigit() else 0
    return 0


def health_restored(w: WorldModel, code: str | None) -> int:
    """The health eating ``code`` now gives back: its heal, up to what is lost."""
    if w.health is None or w.max_health is None:
        return 0
    return min(heal_of(code), max(0, w.max_health - w.health))


def supply_value(code: str | None, w: WorldModel, kb: KnowledgeBase | None, seen: Mapping[str, int] | None = None) -> float:
    """One supply's value in gems, wherever it lies (the module's table)."""
    if not code:
        return 0.0
    if gem_count(code):
        return float(gem_count(code))
    if is_life(code):
        return LIFE_GEMS
    seen = prices(w, kb) if seen is None else seen
    if eaten_on_pickup(code):
        return health_restored(w, code) * gems_per_health(w, kb, seen)
    return float(seen.get(code, 0))


def room(w: WorldModel) -> int:
    """Slots free in the pack; what is eaten on pickup needs none."""
    return max(0, w.carry_capacity - w.slots_carried())


def chest_value(codes: Iterable[str], w: WorldModel, kb: KnowledgeBase | None, seen: Mapping[str, int] | None = None) -> float:
    """What a chest holding ``codes`` is worth: the most valuable of them
    that fit the pack (``room``), and all that is eaten on pickup."""
    seen = prices(w, kb) if seen is None else seen
    eaten, kept = 0.0, []
    for code in codes:
        v = supply_value(code, w, kb, seen)
        if eaten_on_pickup(code):
            eaten += v
        else:
            kept.append(v)
    return eaten + sum(sorted(kept, reverse=True)[: room(w)])


def unseen_chest_gems(w: WorldModel, seen: Mapping[str, int]) -> float:
    """A chest never seen in reach: at least one supply, at the cheapest price known."""
    if room(w) == 0:
        return 0.0
    known = [p for p in seen.values() if p > 0]
    return float(min(known)) if known else 1.0


def finds(w: WorldModel, kb: KnowledgeBase | None) -> list[Find]:
    """Every walk target in sight or remembered on this map worth anything:
    free supplies, chests in view, and her death chests on this map."""
    seen = prices(w, kb)
    out: list[Find] = []
    in_view: set[int] = set()
    for e in w.entities:
        if e.kind == "supply" and e.gem_price is None:
            if not eaten_on_pickup(e.code) and not gem_count(e.code) and room(w) == 0:
                continue
            v = supply_value(e.code, w, kb, seen)
            if v > 0:
                out.append(Find("supply", e.id, e.pos, w.map_id, e.code, v))
        elif e.kind == "chest":
            in_view.add(e.id)
            v = _chest_gems(e.id, w, kb, seen)
            if v > 0:
                out.append(Find("chest", e.id, e.pos, w.map_id, "chest", v))
    for chest_id, c in w.death_chests.items():
        if chest_id in in_view or c.map_id != w.map_id:
            continue
        v = _chest_gems(chest_id, w, kb, seen)
        if v > 0:
            out.append(Find("chest", chest_id, c.pos, c.map_id, "chest", v))
    return out


def _chest_gems(chest_id: int, w: WorldModel, kb: KnowledgeBase | None, seen: Mapping[str, int]) -> float:
    contents = w.chest_contents.get(chest_id)
    if contents is not None:
        return chest_value([s.code for s in contents], w, kb, seen)
    chest = w.death_chests.get(chest_id)
    if chest is not None and chest.contents is not None:
        return chest_value(chest.contents, w, kb, seen)
    return unseen_chest_gems(w, seen)


def step_gems(w: WorldModel, kb: KnowledgeBase | None, urgency: float = 1.0) -> float:
    """What a step costs in gems: the gems a cut drops on this map (A105:
    a step is an action like a cut), times ``urgency`` (1 at ease; more,
    up to ``URGENT``, under threat)."""
    base = expected_yield(regions(kb, w.map_id))
    return URGENT if urgency == URGENT else base * urgency


def per_step(gems: float, steps: int) -> float:
    """A target's value per step: over the steps to it and the action that takes it."""
    return gems / (steps + 1)


def detour_gain(gems: float, extra: int, step: float) -> float:
    """What going by a find gains: its value less the steps it adds at the step price."""
    return gems - extra * step if extra else gems


def allowance(gems: float, step: float) -> int:
    """The most steps a find of ``gems`` is worth at ``step`` gems a step."""
    if step == URGENT:
        return -1
    return math.floor(gems / step) if step > 0 else math.floor(gems * 10**6)


def extra_steps(w: WorldModel, here: Pos, find: Pos, route: list[Pos], limit: int, avoid: Collection[str] = ()) -> int | None:
    """Steps going by ``find`` now adds to walking ``route`` from ``here``,
    rejoining it at its best cell; None when that is more than ``limit``, or
    when the walk to ``find`` is longer than the straight line plus ``limit``.

    ``route`` must start at the step after ``here`` (``pathing.route_ahead``):
    cell i is i + 1 steps away. Distances are walks over known ground
    (walkable or a door, never a tile in ``avoid``; the route's own cells
    count as open), searched from ``find`` no further than the straight line
    to ``here`` plus ``limit``. A find near in a straight line but far on
    foot (beside a later stretch of a route that winds round a wall) is no
    short insert from here: it is priced again as the walk comes by it. A
    find beside the route within that search costs at most 2."""
    if not route:
        return None
    # Straight lines never overestimate a walk: past the limit on them, no search.
    reach = chebyshev(here, find)
    if reach + min(chebyshev(find, p) - (i + 1) for i, p in enumerate(route)) > limit:
        return None
    on_route = set(route) | {here}

    def open_(p: Pos) -> bool:
        tile = w.view.tiles.get(p)
        return p in on_route or (tile not in avoid and (w.walkable(p) or tile in DOORS))

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
