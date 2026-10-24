"""One value for every walk target, in gems, and one price for a step (A170).

Every place a walk may go for something has one value, in gems:

- gems lying free: their count (``gem_count``: a gem one, a gem cache the
  number it names; both are eaten into the counter on pickup);
- food, or any heal eaten on pickup: the health it would actually give back,
  ``min(heal, max_health - health)``, at the gems a health point costs as a
  potion (``gems_per_health``). At full health it is worth nothing, so food
  is passed by then by its price, not by a test of its own;
- a bush (``supplies.BLOCK_FOOD``): its berry's value times the chance a cut
  drops one (``berry_chance``, learned from the character's own cuts like
  ``gem_yield``, ``DropWatch``, with ``BUSH_PRIOR`` until then);
- a life (``supplies.is_life``): ``LIFE_GEMS``;
- any other supply, lying free or in a chest: its price (``prices``): as
  seen for sale in this world (``chest_sources.known_prices``), else the
  Supplies reference's for this world (``supplies.list_price``), and only
  what fits the pack;
- a chest, in view or remembered (her own death chests, kept across runs,
  A122): what it holds that fits the pack (``chest_value``): her own death
  chest by its contents known from the death (A167), any chest by what was
  seen in it in reach (``WorldModel.chest_contents``; a storage chest seen
  empty is worth nothing). A chest whose contents were never seen is a
  dropped chest's expected worth (``expected_chest_gems``: the running
  average of what the non-empty chests opened held, kept in the knowledge
  base, ``learn_opened_chests``, from ``CHEST_PRIOR_GEMS``): a dropped chest
  is never empty, since the game removes it once emptied.

A step on clear ground is nearly free: ``STEP_GEMS``. Only a monster or
another obstacle makes one dearer, by the surcharges the cost grid already
prices in step units (``navigation.planner``: a hostile's priced ground, a
hazard or costly cell, a block to break, fog), converted at that same rate
(``walk_gems``). A target's net is its value less the gems of its walk
(``net``); the highest net is chosen, distance breaking ties. A side find on
any walk is taken when its value beats the steps it adds (``extra_steps``)
at that rate (``detour_gain``). Urgency the cost grid does not see (an
attack under way, a boss clock running, hostiles the margin cannot pay for)
is the caller's, and ``URGENT`` declines every find. Lorraine's gather and
walks read all of this; Gary's Detour reads only ``extra_steps`` so far (A170
is partial: his allowance, Pickup and Gather do not read the value yet).
"""

from __future__ import annotations

import math
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .chest_sources import known_prices
from .gem_yield import GEM_CACHE_PREFIX, GEM_WINDOW_TICKS, PRIOR_CUTS, REGROW_TICKS
from .loot import GEM_SUPPLY_CODES
from .supplies import BLOCK_FOOD, eaten_on_pickup, heal_of, is_life, is_potion, list_price, sold_codes
from .world import DOORS, NEIGHBOURS, Pos, WorldModel, chebyshev

if TYPE_CHECKING:
    from .knowledge_base import KnowledgeBase

# A life in gems: twice a gem pile, the weight Detour gave one before A170
# (a life's 16 steps to a gem pile's 8). No world prices a life.
LIFE_GEMS = 2.0
# A health point in gems with no potion priced, seen or in the reference:
# a small potion's 10 gems for 10 health.
PRIOR_GEMS_PER_HEALTH = 1.0
# A step on clear ground, in gems: nearly free. Every surcharge the cost
# grid prices (a hostile's ground, a hazard, a break, fog) is in steps and
# converted at this rate (``walk_gems``).
STEP_GEMS = 0.1
# The step price under urgency that declines every find.
URGENT = math.inf
# What a chest whose contents were never seen is worth until opened chests
# measure it, weighted as this many chests: a dropped chest is never empty,
# and a death drops what was carried, a few items.
CHEST_PRIOR_GEMS = 5.0
CHEST_PRIOR_WEIGHT = 3
# The chance a bush's cut drops a berry until cuts measure it, weighted as
# ``gem_yield.PRIOR_CUTS`` cuts. The reference gives no rate (Manual §16),
# so it is grass's farther rate.
BUSH_PRIOR = 0.25
# Knowledge base keys (``kb.extra``): opened chests' worth and bush drops.
CHESTS_KEY, BUSH_KEY = "opened_chests", "bush_drops"
# The opened chest ids kept, so a chest is counted once.
CHEST_IDS_KEPT = 500


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


def expected_chest_gems(w: WorldModel, kb: KnowledgeBase | None) -> float:
    """A chest whose contents were never seen: what the non-empty chests
    opened held on average (``learn_opened_chests``), pulled toward
    ``CHEST_PRIOR_GEMS``; nothing with no room in the pack."""
    if room(w) == 0:
        return 0.0
    row = _row(kb, CHESTS_KEY)
    n, gems = int(row.get("count", 0)), float(row.get("gems", 0.0))
    return (gems + CHEST_PRIOR_GEMS * CHEST_PRIOR_WEIGHT) / (n + CHEST_PRIOR_WEIGHT)


def learn_opened_chests(w: WorldModel, kb: KnowledgeBase | None) -> None:
    """Count each chest seen in reach holding something, once, toward
    ``expected_chest_gems``: its contents each by ``supply_value``, fit or
    not (gems by their count, food by the health it gives back now). Our
    own death chests are known, not expected, and an empty one is a storage
    chest (a dropped chest is never empty), so neither counts."""
    if kb is None:
        return
    opened = {cid: c for cid, c in w.chest_contents.items() if c and cid not in w.death_chests}
    if not opened or all(cid in _row(kb, CHESTS_KEY).get("ids", []) for cid in opened):
        return
    seen = prices(w, kb)  # outside the lock: it reads the knowledge base
    with kb.lock:
        row = dict(_row(kb, CHESTS_KEY))
        ids = list(row.get("ids", []))
        for chest_id, contents in opened.items():
            if chest_id in ids:
                continue
            worth = sum(supply_value(s.code, w, kb, seen) for s in contents)
            row["count"], row["gems"] = int(row.get("count", 0)) + 1, float(row.get("gems", 0.0)) + worth
            ids.append(chest_id)
        row["ids"] = ids[-CHEST_IDS_KEPT:]
        kb.extra[CHESTS_KEY] = row


def _row(kb: KnowledgeBase | None, key: str) -> dict:
    row = kb.extra.get(key) if kb is not None else None
    return row if isinstance(row, dict) else {}


def berry_chance(kb: KnowledgeBase | None) -> float:
    """The chance a bush's cut drops its berry: the cuts measured
    (``DropWatch``) pulled toward ``BUSH_PRIOR`` as ``PRIOR_CUTS`` cuts."""
    row = _row(kb, BUSH_KEY)
    return (int(row.get("berries", 0)) + BUSH_PRIOR * PRIOR_CUTS) / (int(row.get("cuts", 0)) + PRIOR_CUTS)


def bush_value(block: str | None, w: WorldModel, kb: KnowledgeBase | None, seen: Mapping[str, int] | None = None) -> float:
    """A block that drops food when cut (``supplies.BLOCK_FOOD``): its food's
    value now times the chance a cut drops it; nothing for another block."""
    food = BLOCK_FOOD.get(block or "")
    return berry_chance(kb) * supply_value(food, w, kb, seen) if food else 0.0


@dataclass
class DropWatch:
    """The bush cuts that landed and are not yet judged (A170): each is
    noted at the tick its ``Use`` result came back (``note_cut``), filed a
    cut, with a berry when one lies on its cell within ``GEM_WINDOW_TICKS``
    of it (a break drops at most one supply, on that block), and a cell the
    cut left standing is passed over for ``REGROW_TICKS`` (``unbroken``)."""

    pending: list[tuple[int | None, Pos, int]] = field(default_factory=list)
    berry: set[tuple[int | None, Pos]] = field(default_factory=set)
    standing: dict[tuple[int | None, Pos], int] = field(default_factory=dict)

    def note_cut(self, w: WorldModel, cell: Pos, tick: int) -> None:
        """A ``Use`` on the bush at ``cell`` landed at ``tick``."""
        if not any(m == w.map_id and p == cell for m, p, _ in self.pending):
            self.pending.append((w.map_id, cell, tick))

    def unbroken(self, w: WorldModel, cell: Pos) -> bool:
        return w.tick < self.standing.get((w.map_id, cell), -1)

    def update(self, w: WorldModel, kb: KnowledgeBase | None) -> None:
        keep = []
        for map_id, cell, tick in self.pending:
            here = map_id == w.map_id
            if here and any(e.kind == "supply" and e.pos == cell and e.code in BLOCK_FOOD.values() for e in w.entities):
                self.berry.add((map_id, cell))
            if w.tick <= tick + GEM_WINDOW_TICKS:
                keep.append((map_id, cell, tick))
                continue
            if here and w.view.tiles.get(cell) in BLOCK_FOOD:
                self.standing[(map_id, cell)] = w.tick + REGROW_TICKS  # no break: nothing to file
            elif here:
                _file_bush_cut(kb, (map_id, cell) in self.berry)
            self.berry.discard((map_id, cell))
        self.pending = keep


def _file_bush_cut(kb: KnowledgeBase | None, berry: bool) -> None:
    if kb is None:
        return
    with kb.lock:
        row = dict(_row(kb, BUSH_KEY))
        row["cuts"] = int(row.get("cuts", 0)) + 1
        row["berries"] = int(row.get("berries", 0)) + (1 if berry else 0)
        kb.extra[BUSH_KEY] = row


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
    return expected_chest_gems(w, kb)


def step_gems(urgent: bool = False) -> float:
    """What a step on clear ground costs in gems: ``STEP_GEMS``, or
    ``URGENT`` when the caller's urgency declines every find."""
    return URGENT if urgent else STEP_GEMS


def walk_gems(cost: float, step: float = STEP_GEMS) -> float:
    """A walk's cost grid price (steps, surcharges in steps) in gems; none costs nothing."""
    return cost * step if cost else 0.0


def net(gems: float, cost: float, step: float = STEP_GEMS) -> float:
    """A target's net: its value less the gems of the walk to it."""
    return gems - walk_gems(cost, step)


def detour_gain(gems: float, extra: float, step: float = STEP_GEMS) -> float:
    """What going by a find gains: its value less the steps it adds at the step price."""
    return net(gems, extra, step)


def allowance(gems: float, step: float = STEP_GEMS) -> int:
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
