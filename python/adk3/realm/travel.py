"""Finding the way: paths over what is known, across maps through known door warps.

Paths are planned over the ADK's cost grid (``agentrealm_adk.navigation``):
known open ground is cheap, unseen ground is priced as probably open, and
cells near threats cost more. The walk is planned again every decision, so
an obstacle that comes into view is routed around on the next one.

A bush or a tree on the way is not a wall when we carry a weapon that
breaks it (Manual §11 Breaking blocks: the knife and every sword cut
bushes, every sword chops trees; the block becomes dirt and grows back in
about a minute). It is priced as passable at the cost of breaking it, and
the travel engine breaks it when the walk reaches it. Many bushes and trees
never break, and a block never says which, so one a strike did not change
is a wall from then on (``Memory.no_effect``).
"""

from __future__ import annotations

from agentrealm_adk import supplies
from agentrealm_adk.break_memory import BLOCK_OPENERS, capabilities_for_code
from agentrealm_adk.navigation import CostGridParams, nearest_target
from agentrealm_adk.navigation.door_graph import route_first_leg, way_off_map
from agentrealm_adk.navigation.rejection import navigation_avoid_costly
from agentrealm_adk.world import Pos, chebyshev

from .context import Ctx
from .memory import Memory, place_key
from .state import State

BREAK_STEPS = 4  # a break priced as this many steps: the swing's cooldown, and the chance it does not break


def grid(ctx: Ctx, *, door: bool = False) -> CostGridParams:
    """The cost grid's settings: cells rejections taught us to avoid, threats priced as threats,
    and the bushes and trees a weapon we carry may break priced as passable."""
    s = ctx.state
    avoid, costly = navigation_avoid_costly(ctx.nav.nav, ctx.kb, s.map_id, s.tick)
    breaks = {p: BREAK_STEPS for p in breakable(s, ctx.memory)}
    return CostGridParams(avoid=avoid, costly=costly, is_hostile=lambda w, e: s.is_threat(e), allow_goal_door=door,
                          break_costs=breaks)


def weapon_breaks(s: State) -> frozenset[str]:
    """What our weapons break without being used up (a tool is used up by a break, Manual §11)."""
    caps: set[str] = set()
    for c in s.carried:
        if supplies.is_weapon(c.code):
            caps |= capabilities_for_code(c.code)
    return frozenset(caps)


def breakable(s: State, memory: Memory) -> set[tuple[int, int]]:
    """Blocks in view that a weapon we carry may break, less those a strike has already left standing."""
    caps = weapon_breaks(s)
    if not caps:
        return set()
    x0, y0, width, height = s.world.perception_rect()
    tiles = s.world.view.tiles
    out = set()
    for y in range(y0, y0 + height):
        for x in range(x0, x0 + width):
            block = tiles.get((x, y))
            if block in BLOCK_OPENERS and BLOCK_OPENERS[block] & caps and place_key(s.map_id, (x, y)) not in memory.no_effect:
                out.add((x, y))
    return out


def weapon_for(s: State, block: str) -> int | None:
    """The id of a carried weapon that breaks ``block``: the armed one first, then the best weapon (so a break
    never swaps a better fighting weapon for a worse one); None when none does."""
    openers = BLOCK_OPENERS.get(block, frozenset())
    ranked = sorted((c for c in s.carried if supplies.is_weapon(c.code)),
                    key=lambda c: (c.where != "armed", c != s.best_weapon()))
    return next((c.id for c in ranked if capabilities_for_code(c.code) & openers), None)


def to_place(ctx: Ctx, map_id: int, cell: Pos, *, door: bool = False) -> list[Pos] | None:
    """The path on this map toward ``map_id:cell``: direct, through known warps, or off this map to look for one."""
    params = grid(ctx, door=door)
    path = route_first_leg(ctx.state.world, ctx.kb, map_id, cell, params)
    if path is None and map_id != ctx.state.map_id:
        path = way_off_map(ctx.state.world, ctx.kb, params)
    return path


def to_nearest(ctx: Ctx, cells: set[Pos], *, door: bool = False) -> list[Pos] | None:
    """The path to the cheapest of ``cells`` on this map; [] when standing on one; None when none is reachable.

    A shelf is never a place to stand: stepping onto a priced supply buys it (Manual §11 Shops)."""
    cells = set(cells) - ctx.state.world.for_sale()
    if not cells:
        return None
    found = nearest_target(ctx.state.world, set(cells), grid(ctx, door=door))
    return found[1] if found is not None else None


def to_beside(ctx: Ctx, cell: Pos, reach: int = 1) -> list[Pos] | None:
    """The path to the nearest cell we can stand on within ``reach`` of ``cell`` (to use, take or talk)."""
    s = ctx.state
    if chebyshev(s.pos, cell) <= reach:
        return []
    x, y = cell
    around = {(x + dx, y + dy) for dx in range(-reach, reach + 1) for dy in range(-reach, reach + 1)}
    standable = {p for p in around if p != cell and (p not in s.world.view.tiles or s.world.walkable(p))}
    return to_nearest(ctx, standable)


def to_strike(ctx: Ctx, cell: Pos, reach: int = 1) -> list[Pos] | None:
    """The path to the nearest cell within ``reach`` of ``cell`` that is off safe ground: no swing lands from
    safe ground (Manual §11 Combat). [] when we stand on one; None when there is none."""
    s = ctx.state
    safe = s.world.view.safe
    if chebyshev(s.pos, cell) <= reach and s.pos not in safe:
        return []
    x, y = cell
    around = {(x + dx, y + dy) for dx in range(-reach, reach + 1) for dy in range(-reach, reach + 1)}
    standable = {p for p in around if p != cell and p not in safe and (p not in s.world.view.tiles or s.world.walkable(p))}
    return to_nearest(ctx, standable)


def to_unseen(ctx: Ctx, toward: Pos | None = None, take: int = 40) -> list[Pos] | None:
    """The path to the edge of unseen ground, the part nearest ``toward`` when given."""
    s = ctx.state
    frontier = s.world.frontier() - {s.pos}
    if toward is not None and frontier:
        frontier = set(sorted(frontier, key=lambda p: chebyshev(p, toward))[:take])
    return to_nearest(ctx, frontier)


def to_safe(ctx: Ctx) -> list[Pos] | None:
    """The path to the nearest known safe ground (Manual §11 Combat: no damage lands there)."""
    s = ctx.state
    safe = s.world.view.safe
    if s.pos in safe:
        return []
    near = {p for p in safe if chebyshev(p, s.pos) <= 60}
    return to_nearest(ctx, near) if near else None

