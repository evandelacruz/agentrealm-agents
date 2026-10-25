"""Lou's methods: the procedures an approach is carried out by (docs/v3/SPEC.md **The executive and the methods**).

Each says what it wants now (an order the reactions carry out), why it
cannot go on when it cannot, and a progress number that rises as it nears
done. None decides when the goal is met (that is the goal's test) or what
to do next (that is the strategist's).
"""

from __future__ import annotations

import time

from agentrealm_adk import supplies
from agentrealm_adk.knowledge_maps import door_warp_known, iter_doors
from agentrealm_adk.world import Pos, chebyshev

from adk3.realm import actions, travel
from adk3.realm.context import Ctx
from adk3.realm.memory import Death, Memory, Sighting, place_key
from adk3.realm.state import State

from .orders import Do, Explore, Fight, GoTo, Order, Work

FIELD_SEARCH = 12  # blocks around the field's center (or us) that count as the field


class Travel:
    """Walk to a cell, on this map or another; through its door when ``door``."""

    name = "travel"

    def __init__(self, map_id: int, cell: Pos, why: str, door: bool = False):
        self.map_id, self.cell, self.why, self.door = map_id, cell, why, door

    def order(self, ctx: Ctx) -> Order:
        return GoTo(self.map_id, self.cell, self.why, self.door)

    def failed(self, ctx: Ctx) -> str | None:
        if not self._there(ctx) and travel.to_place(ctx, self.map_id, self.cell, door=self.door) is None:
            return f"no known way to {self.map_id}:{self.cell[0]},{self.cell[1]}"
        return None

    def _there(self, ctx: Ctx) -> bool:
        s = ctx.state
        return s.map_id == self.map_id and chebyshev(s.pos, self.cell) <= 1

    def progress(self, ctx: Ctx) -> float:
        s = ctx.state
        if s.map_id != self.map_id:
            return -10_000.0 + len(s.world.view.tiles) / 100
        return -float(chebyshev(s.pos, self.cell))


class Seek:
    """Explore toward ``toward`` (unseen ground nearest it), until ``found``."""

    name = "explore"

    def __init__(self, why: str, found, toward: Pos | None = None):
        self.why, self.found, self.toward = why, found, toward

    def order(self, ctx: Ctx) -> Order:
        return Explore(self.why, self.toward)

    def failed(self, ctx: Ctx) -> str | None:
        return None if travel.to_unseen(ctx, self.toward) is not None else "no unseen ground left in reach on this map"

    def progress(self, ctx: Ctx) -> float:
        return float(len(ctx.state.world.view.tiles))


class Buy:
    """Buy ``code`` where it was seen for sale: walk up to the shelf and take it (Manual §11 Shops)."""

    name = "buy"

    def __init__(self, code: str, seen_at: Sighting):
        self.code, self.seen = code, seen_at

    def stock(self, ctx: Ctx):
        return next((e for e in ctx.state.supplies() if e.code == self.code and e.pos == (self.seen.x, self.seen.y)), None)

    def order(self, ctx: Ctx) -> Order:
        item = self.stock(ctx)
        if item is not None:
            return Do(actions.Take(item.id, self.code), item.pos)
        return GoTo(self.seen.map_id, (self.seen.x, self.seen.y), f"to the {self.code} for sale")

    def failed(self, ctx: Ctx) -> str | None:
        s = ctx.state
        if s.gems < self.seen.price:
            return f"{self.code} costs {self.seen.price} gems; we hold {s.gems}"
        if s.map_id == self.seen.map_id and chebyshev(s.pos, (self.seen.x, self.seen.y)) <= 3 and self.stock(ctx) is None:
            return f"no {self.code} on its shelf now (stock returns 2 minutes after a sale)"
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

    def failed(self, ctx: Ctx) -> str | None:
        return None if ctx.state.has(self.code) else f"we do not carry {self.code}"

    def progress(self, ctx: Ctx) -> float:
        return 0.0


class EarnGems:
    """Field work: cut grass in the field the strategist named (around ``field``), or near us, for its gems
    (Manual §16 How to make money). Grass a known hostile would reach first is not worked: it holds that ground."""

    name = "earn gems"

    def __init__(self, field: Pos | None = None):
        self.field = field
        self.gems_at: tuple[float, int] | None = None  # (when, gems) of the last rise

    def cells(self, ctx: Ctx) -> list[Pos]:
        s, mem = ctx.state, ctx.memory
        center = self.field or s.pos
        safe = s.world.view.safe
        out = [p for p, block in s.world.view.tiles.items()
               if block == "grass" and p not in safe and chebyshev(p, center) <= FIELD_SEARCH
               and place_key(s.map_id, p) not in mem.no_effect and not s.ground_held(p)]

        def worth(p: Pos) -> tuple:
            rate = mem.gems_per_break(s.map_id, p)
            return (-(rate if rate is not None else 0.2), chebyshev(p, s.pos))

        return sorted(out, key=worth)[:30]

    def order(self, ctx: Ctx) -> Order:
        cells = self.cells(ctx)
        if cells:
            return Work(tuple(cells), "cut grass for gems")
        if self.field is not None:
            return GoTo(ctx.state.map_id, self.field, "to the field")
        return Explore("look for grass no hostile holds")

    def failed(self, ctx: Ctx) -> str | None:
        s = ctx.state
        if "cut" not in travel.weapon_breaks(s):
            return "nothing we carry cuts grass"
        if self.field is not None and chebyshev(s.pos, self.field) <= FIELD_SEARCH and not self.cells(ctx):
            holders = sorted({e.code for e in s.known_threats() if chebyshev(e.pos, self.field) <= FIELD_SEARCH + 6})
            return f"the field around {self.field[0]},{self.field[1]} is " + (
                f"held by {', '.join(holders)}" if holders else "out of cuttable grass")
        return None

    def minutes_since_gem(self, ctx: Ctx) -> float:
        now, gems = time.time(), ctx.state.gems
        if self.gems_at is None or gems > self.gems_at[1]:
            self.gems_at = (now, gems)
        return (now - self.gems_at[0]) / 60

    def progress(self, ctx: Ctx) -> float:
        return ctx.state.gems + sum(y.breaks for y in ctx.memory.yields.values()) / 10.0


class Forage:
    """Health back from food (Manual §11 Food and potions: health never comes back on its own, and safe ground
    heals nothing): eat what lies in view, else cut bushes, which drop berries (§16), else look toward town."""

    name = "forage"

    def order(self, ctx: Ctx) -> Order:
        s, mem = ctx.state, ctx.memory
        food = [e for e in s.supplies() if not e.gem_price and supplies.eaten_on_pickup(e.code) and supplies.heal_of(e.code) > 0]
        if food:
            e = min(food, key=lambda e: chebyshev(e.pos, s.pos))
            return Do(actions.Take(e.id, e.code), e.pos)
        if travel.weapon_for(s, "bush") is not None:
            bushes = sorted((p for p, b in s.world.view.tiles.items() if b == "bush"
                             and place_key(s.map_id, p) not in mem.no_effect and chebyshev(p, s.pos) <= FIELD_SEARCH),
                            key=lambda p: chebyshev(p, s.pos))
            if bushes:
                return Work(tuple(bushes[:20]), "cut a bush for berries")
        toward = ctx.town[1] if ctx.town is not None and ctx.town[0] == s.map_id else None
        return Explore("look for food", toward)

    def failed(self, ctx: Ctx) -> str | None:
        return None

    def progress(self, ctx: Ctx) -> float:
        return ctx.state.health + sum(y.breaks for y in ctx.memory.yields.values()) / 10.0


class Recover:
    """Go back for our death chest and take everything from it (Manual §11 Death)."""

    name = "recover"

    def __init__(self, death: Death):
        self.death = death

    def place(self) -> tuple[int, Pos]:
        d = self.death
        return d.chest_map or d.map_id, (d.chest_x if d.chest_x is not None else d.x, d.chest_y if d.chest_y is not None else d.y)

    def chest(self, ctx: Ctx):
        return next((e for e in ctx.state.chests() if e.id == self.death.chest_id), None)

    def order(self, ctx: Ctx) -> Order:
        chest = self.chest(ctx)
        if chest is not None:
            return Do(actions.Withdraw(chest.id), chest.pos)
        map_id, cell = self.place()
        return GoTo(map_id, cell, "back to our death chest")

    def failed(self, ctx: Ctx) -> str | None:
        map_id, cell = self.place()
        s = ctx.state
        if s.map_id == map_id and chebyshev(s.pos, cell) <= 3 and self.chest(ctx) is None:
            self.death.chest_id = None
            return "the chest is gone: emptied, by us or someone else"
        return None

    def progress(self, ctx: Ctx) -> float:
        map_id, cell = self.place()
        return -float(chebyshev(ctx.state.pos, cell)) if ctx.state.map_id == map_id else -10_000.0


class BreakOpen:
    """Break the block at ``cell`` with ``code`` (a bomb on a hidden entrance, Manual §11 Breaking blocks)."""

    name = "break open"

    def __init__(self, map_id: int, cell: Pos, code: str):
        self.map_id, self.cell, self.code = map_id, cell, code

    def order(self, ctx: Ctx) -> Order:
        s = ctx.state
        if s.map_id != self.map_id or chebyshev(s.pos, self.cell) > 1:
            return GoTo(self.map_id, self.cell, f"to the block to break with {self.code}")
        item = next((c for c in s.carried if c.code == self.code), None)
        arm = item.id if item is not None and item.where != "armed" else None
        return Do(actions.UseOn(self.cell[0], self.cell[1], arm, f"break with {self.code}"), self.cell)

    def failed(self, ctx: Ctx) -> str | None:
        if place_key(self.map_id, self.cell) in ctx.memory.no_effect:
            return f"{self.code} did not break the block at {self.cell[0]},{self.cell[1]}"
        return None if ctx.state.has(self.code) else f"we do not carry {self.code}"

    def progress(self, ctx: Ctx) -> float:
        return -float(chebyshev(ctx.state.pos, self.cell))


class FindBoss:
    """Inside a level: look for its boss, through the doors not taken yet and the ground not seen (Manual §11 Bosses and levels)."""

    name = "find the boss"

    def order(self, ctx: Ctx) -> Order:
        s = ctx.state
        untried = [p for p in s.doors() if not _warp_known(ctx, p)]
        if untried:
            door = min(untried, key=lambda p: chebyshev(p, s.pos))
            return GoTo(s.map_id, door, "through a door not taken yet", door=True)
        return Explore("look for the boss")

    def failed(self, ctx: Ctx) -> str | None:
        return None if ctx.state.in_level else "not inside a level"

    def progress(self, ctx: Ctx) -> float:
        return float(len(ctx.state.world.view.tiles)) + 1000 * len(ctx.memory.levels)


class FightBoss:
    """Fight the boss in view to the end, on its clock; the boss reaction leaves before the limit."""

    name = "fight the boss"

    def order(self, ctx: Ctx) -> Order | None:
        boss = ctx.state.boss()
        return Fight(boss.id, "the boss", boss=True) if boss is not None else None

    def failed(self, ctx: Ctx) -> str | None:
        return None if ctx.state.boss() is not None else "the boss is out of view"

    def progress(self, ctx: Ctx) -> float:
        boss = ctx.state.boss()
        return -float(boss.health or 0) if boss is not None else 0.0


def seen(s: State, memory: Memory, what: str) -> bool:
    """``what`` is in view: ``door``, ``sign``, ``entrance``, ``npc:<type>`` or ``supply:<code>``."""
    kind, _, code = what.partition(":")
    if kind == "door":
        return bool(s.doors())
    if kind == "sign":
        return bool(s.signs())
    if kind == "entrance":
        return any(e.map_id == s.map_id and (e.x, e.y) in s.world.view.tiles for e in memory.entrances.values())
    if kind == "npc":
        return any(e.code == code for e in s.npcs())
    if kind == "supply":
        return any(e.code == code for e in s.supplies())
    return False


def _warp_known(ctx: Ctx, door: Pos) -> bool:
    """We have been through ``door`` and know where it leads (the knowledge base's warps)."""
    if ctx.kb is None:
        return False
    return any((int(d["x"]), int(d["y"])) == door and door_warp_known(d) for d in iter_doors(ctx.kb, ctx.state.map_id))
