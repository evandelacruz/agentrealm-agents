"""Lou's skills: procedures that satisfy one kind of need (docs/v3/SPEC.md **Executive and skills**).

Each says what it wants now (an order the engines carry out), when it is
done, when it cannot go on and why, and a progress number that rises as it
nears done. A skill never acts itself and never looks past its own need.
"""

from __future__ import annotations

from agentrealm_adk import supplies
from agentrealm_adk.knowledge_maps import door_warp_known, iter_doors
from agentrealm_adk.world import Pos, chebyshev

from adk3.realm import actions, travel
from adk3.realm.context import Ctx
from adk3.realm.memory import Death, Sighting, place_key

from .orders import Do, Explore, Fight, GoTo, Order, Work

FIELD_SEARCH = 12  # blocks: grass this near is worked before walking off for more


class Travel:
    """Walk to a cell, on this map or another; through its door when ``door``."""

    name = "travel"

    def __init__(self, map_id: int, cell: Pos, why: str, door: bool = False):
        self.map_id, self.cell, self.why, self.door = map_id, cell, why, door
        self.start_map: int | None = None

    def order(self, ctx: Ctx) -> Order:
        return GoTo(self.map_id, self.cell, self.why, self.door)

    def done(self, ctx: Ctx) -> bool:
        s = ctx.state
        if self.start_map is None:
            self.start_map = s.map_id
        if self.door:
            return s.map_id != self.map_id
        return s.map_id == self.map_id and chebyshev(s.pos, self.cell) <= (0 if s.world.walkable(self.cell) else 1)

    def failed(self, ctx: Ctx) -> str | None:
        if travel.to_place(ctx, self.map_id, self.cell, door=self.door) is None:
            return f"no known way to {self.map_id}:{self.cell[0]},{self.cell[1]}"
        return None

    def progress(self, ctx: Ctx) -> float:
        s = ctx.state
        return -float(chebyshev(s.pos, self.cell)) if s.map_id == self.map_id else -10_000.0 + len(s.world.view.tiles) / 100


class Seek:
    """Explore toward ``toward`` until ``found`` says what we look for is known."""

    name = "explore"

    def __init__(self, why: str, found, toward: Pos | None = None):
        self.why, self.found, self.toward = why, found, toward

    def order(self, ctx: Ctx) -> Order:
        return Explore(self.why, self.toward)

    def done(self, ctx: Ctx) -> bool:
        return self.found(ctx)

    def failed(self, ctx: Ctx) -> str | None:
        return None if travel.to_unseen(ctx, self.toward) is not None else "no unseen ground left in reach on this map"

    def progress(self, ctx: Ctx) -> float:
        return float(len(ctx.state.world.view.tiles))


class Buy:
    """Buy ``code`` where it was seen for sale: walk up to it and take it (Manual §11 Shops)."""

    name = "buy"

    def __init__(self, code: str, seen: Sighting):
        self.code, self.seen = code, seen
        self.had: int | None = None

    def count(self, ctx: Ctx) -> int:
        return sum(1 for c in ctx.state.carried if c.code == self.code)

    def stock(self, ctx: Ctx):
        return next((e for e in ctx.state.supplies() if e.code == self.code and e.pos == (self.seen.x, self.seen.y)), None)

    def order(self, ctx: Ctx) -> Order:
        item = self.stock(ctx)
        if item is not None:
            return Do(actions.Take(item.id, self.code), item.pos)
        return GoTo(self.seen.map_id, (self.seen.x, self.seen.y), f"to the {self.code} for sale")

    def done(self, ctx: Ctx) -> bool:
        if self.had is None:
            self.had = self.count(ctx)
        return self.count(ctx) > self.had

    def failed(self, ctx: Ctx) -> str | None:
        s = ctx.state
        if s.gems < self.seen.price:
            return f"{self.code} costs {self.seen.price} gems; we hold {s.gems}"
        here = (self.seen.x, self.seen.y)
        if s.map_id == self.seen.map_id and chebyshev(s.pos, here) <= 3 and self.stock(ctx) is None:
            return f"no {self.code} on its shelf now (sold out; stock returns 2 minutes after a sale)"
        return None

    def progress(self, ctx: Ctx) -> float:
        s = ctx.state
        return -float(chebyshev(s.pos, (self.seen.x, self.seen.y))) if s.map_id == self.seen.map_id else -10_000.0


class Equip:
    """Arm a carried weapon or tool, or wear carried armor or an accessory (Manual §11 Supplies and inventory)."""

    name = "equip"

    def __init__(self, code: str):
        self.code = code

    def order(self, ctx: Ctx) -> Order | None:
        item = ctx.state.held(self.code)
        if item is None:
            return None
        verb = "Wear" if supplies.worn_slot(self.code) else "Arm"
        return Do(actions.Equip(item.id, verb, self.code), ctx.state.pos, reach=0)

    def done(self, ctx: Ctx) -> bool:
        return any(c.code == self.code and c.where in ("armed", "worn") for c in ctx.state.carried)

    def failed(self, ctx: Ctx) -> str | None:
        return None if ctx.state.has(self.code) else f"we do not carry {self.code}"

    def progress(self, ctx: Ctx) -> float:
        return 0.0


class EarnGems:
    """Field work until we hold ``target`` gems: cut grass outside town, best regions first (Manual §16 How to make money)."""

    name = "earn gems"

    def __init__(self, target: int):
        self.target = target

    def fields(self, ctx: Ctx) -> list[Pos]:
        s, mem = ctx.state, ctx.memory
        safe = s.world.view.safe
        out = [p for p, block in s.world.view.tiles.items()
               if block == "grass" and p not in safe and chebyshev(p, s.pos) <= FIELD_SEARCH
               and place_key(s.map_id, p) not in mem.no_effect]

        def worth(p: Pos) -> tuple:
            rate = mem.gems_per_break(s.map_id, p)
            return (-(rate if rate is not None else 0.2), chebyshev(p, s.pos))

        return sorted(out, key=worth)[:30]

    def order(self, ctx: Ctx) -> Order:
        cells = self.fields(ctx)
        if cells:
            return Work(tuple(cells), "cut grass for gems")
        return Explore("look for fields to cut", None)

    def done(self, ctx: Ctx) -> bool:
        return ctx.state.gems >= self.target

    def failed(self, ctx: Ctx) -> str | None:
        if "cut" not in travel.weapon_breaks(ctx.state):
            return "nothing we carry cuts grass"
        return None

    def progress(self, ctx: Ctx) -> float:
        breaks = sum(y.breaks for y in ctx.memory.yields.values())
        return ctx.state.gems + breaks / 10.0


class Recover:
    """Go back for our death chest and take everything from it (Manual §11 Death)."""

    name = "recover"

    def __init__(self, death: Death):
        self.death = death

    def place(self) -> tuple[int, Pos]:
        return self.death.chest_map or self.death.map_id, (self.death.chest_x or self.death.x, self.death.chest_y or self.death.y)

    def chest(self, ctx: Ctx):
        return next((e for e in ctx.state.chests() if e.id == self.death.chest_id), None)

    def order(self, ctx: Ctx) -> Order:
        chest = self.chest(ctx)
        if chest is not None:
            return Do(actions.Withdraw(chest.id), chest.pos)
        map_id, cell = self.place()
        return GoTo(map_id, cell, "back to our death chest")

    def done(self, ctx: Ctx) -> bool:
        map_id, cell = self.place()
        s = ctx.state
        gone = s.map_id == map_id and chebyshev(s.pos, cell) <= 3 and self.chest(ctx) is None
        if gone:
            self.death.chest_id = None  # emptied, by us or someone else
        return gone

    def failed(self, ctx: Ctx) -> str | None:
        return None

    def progress(self, ctx: Ctx) -> float:
        map_id, cell = self.place()
        return -float(chebyshev(ctx.state.pos, cell)) if ctx.state.map_id == map_id else -10_000.0


class BreakOpen:
    """Break the block at ``cell`` with ``code`` (a bomb on a hidden entrance, Manual §11 Breaking blocks)."""

    name = "break open"

    def __init__(self, map_id: int, cell: Pos, code: str):
        self.map_id, self.cell, self.code = map_id, cell, code
        self.was: str | None = None

    def order(self, ctx: Ctx) -> Order:
        s = ctx.state
        if s.map_id != self.map_id or chebyshev(s.pos, self.cell) > 1:
            return GoTo(self.map_id, self.cell, f"to the block to break with {self.code}")
        item = next((c for c in s.carried if c.code == self.code), None)
        arm = item.id if item is not None and item.where != "armed" else None
        return Do(actions.UseOn(self.cell[0], self.cell[1], arm, f"break with {self.code}"), self.cell)

    def done(self, ctx: Ctx) -> bool:
        block = ctx.state.world.view.tiles.get(self.cell) if ctx.state.map_id == self.map_id else None
        if self.was is None:
            self.was = block
        return block is not None and block != self.was

    def failed(self, ctx: Ctx) -> str | None:
        if place_key(self.map_id, self.cell) in ctx.memory.no_effect:
            return f"{self.code} did not break the block at {self.cell[0]},{self.cell[1]}"
        return None if ctx.state.has(self.code) else f"we do not carry {self.code}"

    def progress(self, ctx: Ctx) -> float:
        return -float(chebyshev(ctx.state.pos, self.cell))


class FindBoss:
    """Inside a level: look for its boss, through the doors not taken yet and the ground not seen (Manual §11 Bosses and levels)."""

    name = "find the boss"

    def __init__(self, level: int):
        self.level = level

    def order(self, ctx: Ctx) -> Order:
        s = ctx.state
        untried = [p for p in s.doors() if not _warp_known(ctx, p)]
        if untried:
            door = min(untried, key=lambda p: chebyshev(p, s.pos))
            return GoTo(s.map_id, door, "through a door not taken yet", door=True)
        return Explore(f"look for level {self.level}'s boss")

    def done(self, ctx: Ctx) -> bool:
        return ctx.state.boss() is not None

    def failed(self, ctx: Ctx) -> str | None:
        if not ctx.state.in_level:
            return "not inside the level"
        return None

    def progress(self, ctx: Ctx) -> float:
        level = ctx.memory.levels.get(self.level)
        return float(len(ctx.state.world.view.tiles)) + 1000 * (len(level.maps) if level is not None else 0)


class FightBoss:
    """Fight the boss in view to the end, on its clock; the boss engine leaves before the limit."""

    name = "fight the boss"

    def __init__(self, level: int):
        self.level = level

    def order(self, ctx: Ctx) -> Order | None:
        boss = ctx.state.boss()
        return Fight(boss.id, f"level {self.level}'s boss", boss=True) if boss is not None else None

    def done(self, ctx: Ctx) -> bool:
        return self.level in ctx.state.levels_cleared

    def failed(self, ctx: Ctx) -> str | None:
        return None if ctx.state.boss() is not None else "the boss is out of view"

    def progress(self, ctx: Ctx) -> float:
        boss = ctx.state.boss()
        return -float(boss.health or 0) if boss is not None else 0.0


def _warp_known(ctx: Ctx, door: Pos) -> bool:
    """We have been through ``door`` and know where it leads (the knowledge base's warps)."""
    if ctx.kb is None:
        return False
    return any((int(d["x"]), int(d["y"])) == door and door_warp_known(d) for d in iter_doors(ctx.kb, ctx.state.map_id))
