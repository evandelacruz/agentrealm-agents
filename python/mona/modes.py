"""The four modes, each its own flow (docs/v4/SPEC.md **The modes**).

A flow keeps one destination at a time and changes it only when the mode
starts, when it arrives and the work there is done, or when the destination
fails (crossed off for the rest of the mode). It may ask the strategist for a
pick from a fixed list (``question``), or tell it the mode is blocked
(``blocked``) or that a learn step is done (``learned``); it never decides
the mode.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from agentrealm_adk import supplies
from agentrealm_adk.knowledge_maps import door_warp_known, iter_doors
from agentrealm_adk.world import Pos, chebyshev

from adk3.realm import actions, travel
from adk3.realm.actions import Action
from adk3.realm.context import Ctx
from adk3.realm.memory import Entrance, place_key

from . import route
from .kit import Kit

ARRIVED = 3  # blocks: this near a destination counts as there
FIELD = 12  # blocks around a field's center that count as the field
INNER, OUTER = 45, 110  # blocks from town: the inner and outer bands (Manual §16: ring 1 near town, farther out pays more)
IRON_DAMAGE = 8  # a weapon this strong is iron or better: the outer band is open to her
TOWN_LOOK_SECONDS = 150  # learn: how long to walk town for signs and helpers
TOWN_RADIUS = 30  # blocks from the town's center that are town (a fenced 60 by 60, Manual changelog 0.9)
RESTOCK_WAIT_SECONDS = 150  # a shelf found sold out restocks 2 minutes after a sale (Manual §11 Shops)
SEARCH_REACH = 160  # blocks from town a search for a seller goes, each way
DIRECTIONS = {"north": (0, -1), "east": (1, 0), "south": (0, 1), "west": (-1, 0),
              "north-east": (1, -1), "south-east": (1, 1), "south-west": (-1, 1), "north-west": (-1, -1)}


@dataclass
class Question:
    """A pick the strategist is asked for: one of ``options``."""

    about: str
    options: list[str]


class Flow:
    name = ""

    def __init__(self, kit: Kit | None):
        self.kit = kit
        self.question: Question | None = None  # waiting on the strategist's pick
        self.blocked: str = ""  # why the mode cannot go on: the strategist revises the kit
        self.learned: str = ""  # a learn step just finished: the strategist revises the kit

    def act(self, ctx: Ctx) -> Action | None:
        raise NotImplementedError

    def picked(self, index: int, ctx: Ctx) -> None:
        self.question = None

    def status(self) -> str:
        return ""


# 1. Learn what level N needs.

class Learn(Flow):
    """Steps in fixed order, each once: walk town (signs and helpers come by reflex), scout level N's entrance,
    attempt the level. After each, the strategist revises the kit and says whether it is trusted."""

    name = "learn"
    STEPS = ("town", "entrance", "attempt")

    def __init__(self, kit: Kit | None, done: list[str]):
        super().__init__(kit)
        self.done = list(done)
        self.reported = False  # the finished step has been put to the strategist
        self.started = time.time()
        self.was_inside = False

    def step(self) -> str | None:
        return next((s for s in self.STEPS if s not in self.done), None)

    def finish(self, why: str) -> None:
        self.done.append(self.step())
        self.learned, self.started = why, time.time()

    def act(self, ctx: Ctx) -> Action | None:
        s = ctx.state
        step = self.step()
        if self.learned or step is None:
            return actions.Stop("waiting on the strategist")
        if step == "town":
            if time.time() - self.started >= TOWN_LOOK_SECONDS:
                self.finish("walked town")
                return None
            go = _look_around_town(ctx)
            if go is None:
                self.finish("walked town")
            return go
        entrance = _entrance(ctx, self.kit)
        if entrance is None:
            self.finish("no entrance known")
            return None
        if step == "entrance":
            if entrance.reached or entrance.refused:
                self.finish(f"scouted the entrance at {entrance.x},{entrance.y}: "
                            + (f"refused us: {entrance.refused}" if entrance.refused else f"{entrance.block or 'a door'}"))
                return None
            go = route.beside(ctx, (entrance.x, entrance.y), 1, "to scout the entrance")
            if go is None:
                self.finish(f"no route to the entrance at {entrance.x},{entrance.y}")
            return go
        # attempt
        if s.in_level:
            self.was_inside = True
            if s.boss() is not None:
                self.finish(f"saw level {s.level}'s boss: {s.boss().code}")
                return None
            return _find_boss(ctx)
        if self.was_inside:
            self.finish("left the level before seeing its boss")
            return None
        if entrance.refused:
            self.finish(f"the entrance refused us: {entrance.refused}")
            return None
        go = route.to(ctx, entrance.map_id, (entrance.x, entrance.y), "into the level", door=True)
        if go is None:
            self.finish(f"no route into the entrance at {entrance.x},{entrance.y}")
        return go

    def status(self) -> str:
        return f"step {self.step() or 'done'}"


# 2. Get money.

class Money(Flow):
    """Work a field until her gems cover the kit; the strategist picks the field from the ones her gear can
    handle. A field that fails (cut out, no route) is crossed off; when all are, the list resets."""

    name = "money"

    def __init__(self, kit: Kit):
        super().__init__(kit)
        self.crossed: set[str] = set()
        self.field: tuple[str, Pos] | None = None
        self.target: Pos | None = None  # the grass block being walked to
        self.options: list[tuple[str, Pos]] = []

    def fields(self, ctx: Ctx) -> list[tuple[str, Pos]]:
        town = ctx.town[1] if ctx.town else ctx.state.pos
        best = ctx.state.best_weapon()
        bands = [("inner", INNER)]
        if best is not None and (supplies.weapon_damage(best.code) or 0) >= IRON_DAMAGE:
            bands.append(("outer", OUTER))
        out = [(f"{band} band, {name} of town", (town[0] + dx * r, town[1] + dy * r))
               for band, r in bands for name, (dx, dy) in DIRECTIONS.items()]
        for key, y in ctx.memory.yields.items():
            if y.breaks >= 10 and y.gems:
                map_id, xy = key.split(":")
                rx, ry = (int(v) for v in xy.split(","))
                if int(map_id) == ctx.state.map_id:
                    center = (rx * 16 + 8, ry * 16 + 8)
                    if chebyshev(center, town) <= (OUTER if len(bands) > 1 else INNER + FIELD):
                        out.append((f"worked before around {center[0]},{center[1]}: {y.gems} gems in {y.breaks} cuts", center))
        out = [o for o in out if o[0] not in self.crossed]
        if not out:
            self.crossed.clear()
            return self.fields(ctx)
        return out

    def picked(self, index: int, ctx: Ctx) -> None:
        super().picked(index, ctx)
        if 0 <= index < len(self.options):
            self.field, self.target = self.options[index], None

    def act(self, ctx: Ctx) -> Action | None:
        if self.question is not None:
            return actions.Stop("waiting on the strategist")
        if self.field is None:
            self.options = self.fields(ctx)
            self.question = Question("which field to work for gems", [o[0] for o in self.options])
            return actions.Stop("waiting on the strategist")
        name, center = self.field
        s = ctx.state
        cells = _grass(ctx, center)
        if cells:
            if self.target not in cells:
                self.target = min(cells, key=lambda p: chebyshev(p, s.pos))
            if chebyshev(self.target, s.pos) <= 1:
                if not s.attack_ready:
                    return actions.Stop("waiting to swing")
                weapon = travel.weapon_for(s, "grass")
                armed = weapon is None or any(c.id == weapon and c.where == "armed" for c in s.carried)
                return actions.UseOn(self.target[0], self.target[1], None if armed else weapon, "cut grass for gems")
            go = route.beside(ctx, self.target, 1, "to grass")
            if go is not None:
                return go
            self.target = None
        if _seen(ctx, center) or route.to(ctx, s.map_id, center, "to the field") is None:
            self.crossed.add(name)  # cut out, or no route: another field
            self.field = None
            return None
        return route.to(ctx, s.map_id, center, "to the field")

    def status(self) -> str:
        return self.field[0] if self.field else "choosing a field"


# 3. Get gear.

class Gear(Flow):
    """The kit's missing pieces in order (weapon, armor, tools, potions): each from the death chest when it holds
    it, else a known seller; with none known, search each way out of town once. Bought, it is armed or worn
    when she fights (the router does it)."""

    name = "gear"

    def __init__(self, kit: Kit):
        super().__init__(kit)
        self.crossed: set[str] = set()  # sellers and searches that gave nothing
        self.waiting_since: float | None = None
        self.search: str | None = None

    def act(self, ctx: Ctx) -> Action | None:
        s, mem = ctx.state, ctx.memory
        missing = self.kit.missing(s)
        if not missing:
            return None
        piece = missing[0]
        ready = _ready(ctx)
        if ready is not None:
            return ready
        death = mem.last_death()
        if death is not None and death.chest_id is not None and piece in death.items and death.chest_map == s.map_id:
            chest = (death.chest_x, death.chest_y)
            if chebyshev(chest, s.pos) <= 1:
                return actions.Withdraw(death.chest_id)
            go = route.beside(ctx, chest, 1, "to our death chest")
            if go is not None:
                return go
        shelves = [x for x in mem.sellers(piece) if place_key(x.map_id, (x.x, x.y)) not in self.crossed]
        if shelves:
            shelf = min(shelves, key=lambda x: (x.map_id != s.map_id, chebyshev((x.x, x.y), s.pos)))
            key = place_key(shelf.map_id, (shelf.x, shelf.y))
            item = next((e for e in s.supplies() if e.code == piece and e.pos == (shelf.x, shelf.y)), None)
            if item is not None:
                if item.gem_price and item.gem_price > s.gems:
                    self.blocked = f"{piece} costs {item.gem_price} gems here; we hold {s.gems}"
                    return None
                self.waiting_since = None
                if chebyshev(item.pos, s.pos) <= 1:
                    return actions.Take(item.id, piece)
                return route.beside(ctx, item.pos, 1, f"to the {piece} for sale")
            if s.map_id == shelf.map_id and chebyshev((shelf.x, shelf.y), s.pos) <= ARRIVED:
                self.waiting_since = self.waiting_since or time.time()
                if time.time() - self.waiting_since < RESTOCK_WAIT_SECONDS:
                    return actions.Stop(f"waiting for the {piece} to restock")
                self.crossed.add(key)
                self.waiting_since = None
                return None
            go = route.to(ctx, shelf.map_id, (shelf.x, shelf.y), f"to the {piece} for sale")
            if go is not None:
                return go
            self.crossed.add(key)
            return None
        return self._search(ctx, piece)

    def _search(self, ctx: Ctx, piece: str) -> Action | None:
        town = ctx.town[1] if ctx.town else ctx.state.pos
        ways = [w for w in ("north", "east", "south", "west") if f"search {w}" not in self.crossed]
        if not ways:
            self.blocked = f"no seller of {piece} found: searched every way out of town"
            return None
        way = self.search if self.search in ways else ways[0]
        self.search = way
        dx, dy = DIRECTIONS[way]
        far = (town[0] + dx * SEARCH_REACH, town[1] + dy * SEARCH_REACH)
        go = route.explore(ctx, far, f"look {way} for a seller of {piece}")
        if go is None or chebyshev(ctx.state.pos, far) <= ARRIVED + 10:
            self.crossed.add(f"search {way}")
            self.search = None
            return None
        return go

    def status(self) -> str:
        return f"searching {self.search}" if self.search else ""


# 4. Beat level N.

class Beat(Flow):
    """Route to the entrance, open it with what the kit carries, go in, find the boss through doors not yet
    taken, and fight to the death (the fight is a reflex). Leaving without the clear is a loss."""

    name = "beat"

    def __init__(self, kit: Kit):
        super().__init__(kit)
        self.was_inside = False

    def act(self, ctx: Ctx) -> Action | None:
        s = ctx.state
        ready = _ready(ctx)
        if ready is not None:
            return ready
        if s.in_level:
            self.was_inside = True
            return _find_boss(ctx)
        if self.was_inside:
            self.blocked = f"left level {self.kit.level} without clearing it"
            self.was_inside = False
            return None
        entrance = _entrance(ctx, self.kit)
        if entrance is None:
            self.blocked = "the kit names no entrance we know"
            return None
        if entrance.refused:
            tool = next((c for c in s.carried if c.code in ("bomb",)), None)
            if tool is not None and entrance.refused != "door_locked":
                if chebyshev((entrance.x, entrance.y), s.pos) <= 1:
                    return actions.UseOn(entrance.x, entrance.y, tool.id if tool.where != "armed" else None, "open the entrance")
                return route.beside(ctx, (entrance.x, entrance.y), 1, "to the entrance")
            if tool is None and entrance.refused != "door_locked":
                self.blocked = f"the entrance at {entrance.x},{entrance.y} refused us: {entrance.refused}"
                return None
        go = route.to(ctx, entrance.map_id, (entrance.x, entrance.y), "to level N", door=True)
        if go is None:
            self.blocked = f"no route to the entrance at {entrance.x},{entrance.y}"
        return go


# Shared.

def _entrance(ctx: Ctx, kit: Kit | None) -> Entrance | None:
    mem = ctx.memory
    if kit is not None and kit.entrance and kit.entrance in mem.entrances:
        return mem.entrances[kit.entrance]
    s = ctx.state
    known = [e for e in mem.entrances.values() if e.map_id == s.map_id]
    return min(known, key=lambda e: chebyshev((e.x, e.y), s.pos), default=None)


def _find_boss(ctx: Ctx) -> Action | None:
    """Inside a level: through a door not taken yet, else to ground not seen (Manual §11 Bosses and levels)."""
    s = ctx.state
    taken = set()
    if ctx.kb is not None:
        taken = {(int(d["x"]), int(d["y"])) for d in iter_doors(ctx.kb, s.map_id) if door_warp_known(d)}
    untried = [p for p in s.doors() if p not in taken]
    if untried:
        door = min(untried, key=lambda p: chebyshev(p, s.pos))
        go = route.to(ctx, s.map_id, door, "through a door not taken yet", door=True)
        if go is not None:
            return go
    return route.explore(ctx, None, "look for the boss") or actions.Stop("nothing left to look at")


def _look_around_town(ctx: Ctx) -> Action | None:
    """Unseen ground inside town only (its signs and helpers are there); None when town is seen."""
    if ctx.town is None:
        return None
    s = ctx.state
    center = ctx.town[1]
    frontier = {p for p in s.world.frontier() if chebyshev(p, center) <= TOWN_RADIUS} - {s.pos}
    return route.along(ctx, travel.to_nearest(ctx, frontier) or (), "look around town") if frontier else None


def _grass(ctx: Ctx, center: Pos) -> list[Pos]:
    s, mem = ctx.state, ctx.memory
    safe = s.world.view.safe
    return [p for p, b in s.world.view.tiles.items() if b == "grass" and p not in safe
            and chebyshev(p, center) <= FIELD and place_key(s.map_id, p) not in mem.no_effect]


def _seen(ctx: Ctx, center: Pos) -> bool:
    x, y = center
    area = [(x + dx, y + dy) for dx in range(-FIELD, FIELD + 1) for dy in range(-FIELD, FIELD + 1)]
    tiles = ctx.state.world.view.tiles
    return sum(p in tiles for p in area) >= len(area) // 2


def _ready(ctx: Ctx) -> Action | None:
    """Arm the best weapon and wear carried armor before going on: one tick each, and only helps."""
    s = ctx.state
    best = s.best_weapon()
    if best is not None and best.where != "armed":
        return actions.Equip(best.id, "Arm", best.code)
    armor = s.unworn()
    if armor is not None:
        return actions.Equip(armor.id, "Wear", armor.code)
    return None

