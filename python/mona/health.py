"""The subgoal: get health, the only one (docs/v4/SPEC.md **The subgoal**).

Enters at 3 health or below; exits at 10 or above, and nothing else ends it.
While it runs it owns the destination: at 3 or below (a carried potion is
drunk by reflex, ``reflexes.drink``), buy one when a seller is known and she
can pay; else, and always above 3, go to the nearest food she knows: a bush
seen on this map, else town's berries; else look around nearby.
A potion heals 10, so above 3 most of it would be spent for nothing.
Reflexes keep running under it, so the food it walks to is eaten by them.
"""

from __future__ import annotations

from agentrealm_adk import supplies
from agentrealm_adk.world import chebyshev

from adk3.realm import actions, travel, value
from adk3.realm.actions import Action
from adk3.realm.context import Ctx
from adk3.realm.memory import place_key
from adk3.realm.state import State

from . import route

ENTER_AT = 3
EXIT_AT = 10
ARRIVED = 3  # blocks: this near the destination counts as there


class GetHealth:
    def __init__(self):
        self.active = False
        self.tried: set[str] = set()  # destinations that gave nothing, this time

    def update(self, s: State) -> str | None:
        """Enter or exit on the marks; returns what changed, for the log."""
        if not self.active and s.health <= ENTER_AT:
            self.active, self.tried = True, set()
            return f"get health: in at {s.health}/{s.max_health}"
        if self.active and s.health >= min(EXIT_AT, s.max_health):
            self.active = False
            return f"get health: out at {s.health}/{s.max_health}"
        return None

    def clear(self) -> None:
        self.active, self.tried = False, set()

    def act(self, ctx: Ctx) -> Action | None:
        s = ctx.state
        low = s.health <= ENTER_AT  # a potion only in danger (drunk by reflex, ``reflexes.drink``): above it, food
        shelf = self._potion_shelf(ctx) if low else None
        if shelf is not None and "shop" not in self.tried:
            item = next((e for e in s.supplies() if e.code == shelf.code and e.pos == (shelf.x, shelf.y)), None)
            if item is not None and chebyshev(item.pos, s.pos) <= 1:
                return actions.Take(item.id, item.code)
            near = s.map_id == shelf.map_id and chebyshev((shelf.x, shelf.y), s.pos) <= ARRIVED
            if near and item is None:
                self.tried.add("shop")  # sold out now
            else:
                go = route.to(ctx, shelf.map_id, (shelf.x, shelf.y), "to buy a potion")
                if go is not None:
                    return go
                self.tried.add("shop")
        bush = self._nearest_bush(ctx)
        town_far = (ctx.town is None or ctx.town[0] != s.map_id or "town" in self.tried
                    or bush is not None and chebyshev(bush, s.pos) < chebyshev(ctx.town[1], s.pos))
        if bush is not None and town_far:  # the nearer of a bush seen and town
            go = route.beside(ctx, bush, 1, "to a bush for its berry")
            if go:
                return go
        if ctx.town is not None and "town" not in self.tried:
            map_id, cell = ctx.town
            if s.map_id == map_id and chebyshev(cell, s.pos) <= ARRIVED:
                self.tried.add("town")  # there, and no food in reach: look further
            else:
                go = route.to(ctx, map_id, cell, "to town for its berries")
                if go is not None:
                    return go
                self.tried.add("town")
        return route.explore(ctx, ctx.town[1] if ctx.town else None, "look for food")

    def _nearest_bush(self, ctx: Ctx):
        """The nearest bush seen on this map that a carried weapon cuts (the take reflex cuts it once near)."""
        s = ctx.state
        if travel.weapon_for(s, "bush") is None:
            return None
        bushes = [p for p, b in s.world.view.tiles.items()
                  if b == "bush" and place_key(s.map_id, p) not in ctx.memory.no_effect]
        return min(bushes, key=lambda p: chebyshev(p, s.pos), default=None)

    def _potion_shelf(self, ctx: Ctx):
        s = ctx.state
        shelves = [x for x in ctx.memory.sellers("small_potion") if s.gems >= value.item("small_potion")]
        return min(shelves, key=lambda x: (x.map_id != s.map_id, chebyshev((x.x, x.y), s.pos)), default=None)
