"""The router: every move and every attack goes through it (docs/v4/SPEC.md **The router**).

Travelling, it replans every decision on what is known (``adk3.realm.travel``:
breakable bushes and trees, known fire and lava, steps the game refused), so
a fence is routed around and the destination stays. Attacking, it arms the
best weapon, wears carried armor, and picks a block to strike from that is in
reach and off safe ground.
"""

from __future__ import annotations

from agentrealm_adk.world import Entity, Pos, chebyshev

from adk3.realm import actions, travel
from adk3.realm.actions import Action
from adk3.realm.context import Ctx


def along(ctx: Ctx, path, why: str, goal: object = None) -> Action | None:
    """Walk the path; a bush or tree in the way that a carried weapon breaks is broken first (Manual §11).

    With a ``goal``, the route already being walked to it is kept while it still holds (every step ahead
    passable on what is known), as a map app keeps a route: a replan never flips between two near-equal
    detours. A fresh plan is taken when the goal changes or the route breaks."""
    if goal is not None:
        path = _kept(ctx, goal, path)
    if not path:
        return None
    s = ctx.state
    nxt = path[0]
    block = s.world.view.tiles.get(nxt)
    if block is not None and not s.world.walkable(nxt) and nxt in travel.breakable(s, ctx.memory):
        if not s.attack_ready:
            return actions.Stop("waiting to swing")
        return actions.UseOn(nxt[0], nxt[1], _arm(ctx, block, nxt), f"break the {block} in the way")
    return actions.Walk(tuple(path), why)


def _kept(ctx: Ctx, goal: object, fresh) -> list[Pos]:
    s = ctx.state
    key = (s.map_id, goal)
    held = getattr(ctx.nav, "route_held", None)
    if fresh is not None and len(fresh) == 0:  # there
        ctx.nav.route_held = None
        return []
    if held is not None and held[0] == key and s.pos in held[1]:
        ahead = held[1][held[1].index(s.pos) + 1:]
        breaks = travel.breakable(s, ctx.memory)
        bad = travel.hazards(s) | s.world.for_sale()  # stepping onto a priced supply buys it
        if len(ahead) > 1 and all(p not in bad and (p not in s.world.view.tiles or s.world.walkable(p) or p in breaks)
                                  for p in ahead):
            ctx.nav.route_held = (key, [s.pos, *ahead])
            return ahead
    ctx.nav.route_held = (key, [s.pos, *fresh]) if fresh else None
    return list(fresh or ())


def steps(ctx: Ctx, path) -> int | None:
    """What a path costs to walk: a step a cell, a break what the router prices it at; None for no path."""
    if path is None:
        return None
    breaks = travel.breakable(ctx.state, ctx.memory)
    return sum(breaks[p] if p in breaks and not ctx.state.world.walkable(p) else 1 for p in path)


def to(ctx: Ctx, map_id: int, cell: Pos, why: str, door: bool = False) -> Action | None:
    """Toward a place, on any map, by known doors; None when no route is known."""
    return along(ctx, travel.to_place(ctx, map_id, cell, door=door) or (), why, ("to", map_id, cell))


def beside(ctx: Ctx, cell: Pos, reach: int, why: str) -> Action | None:
    return along(ctx, travel.to_beside(ctx, cell, reach) or (), why, ("beside", cell, reach))


def explore(ctx: Ctx, toward: Pos | None, why: str) -> Action | None:
    return along(ctx, travel.to_unseen(ctx, toward) or (), why)


def strike(ctx: Ctx, foe: Entity) -> Action | None:
    """Fight ``foe``: the best weapon armed and carried armor worn first, then from a block in reach off safe
    ground (no swing lands from safe ground, Manual §11 Combat), on the weapon's cooldown."""
    s = ctx.state
    stand = travel.to_strike(ctx, foe.pos, s.world.attack_range or 1)
    if stand:  # on the way the armed slot is the router's (a break); the weapon comes out at the swing
        return along(ctx, stand, f"close on {foe.code}")
    if stand is None:
        return None
    best = s.best_weapon()
    if best is not None and best.where != "armed":
        return actions.Equip(best.id, "Arm", best.code)
    armor = s.unworn()
    if armor is not None:
        return actions.Equip(armor.id, "Wear", armor.code)
    return actions.Swing(foe.id, f"{foe.code} {foe.id}") if s.attack_ready else actions.Stop("between swings")


ESCAPE_STEPS = 8  # the escapes looked at lie this many steps off, at most
REACH = 1  # a hostile this near strikes
SAFE_LEAD = 99  # safe ground: no blow lands there, whoever is near


def chase(ctx: Ctx, foe: Pos, target: Pos, steps: int) -> Pos:
    """Where a chaser stands after ``steps`` steps toward ``target``: each step the open neighbour that brings it
    closest, and none when no step brings it closer (Manual §11 Hostiles: it stands still). So a tree, a wall or a
    bush between them stops it."""
    w = ctx.state.world
    at = foe
    for _ in range(steps):
        here = chebyshev(at, target)
        if here <= REACH:
            break
        steps_open = [p for p in w.open_neighbours(at) if w.walkable(p)]
        nxt = min(steps_open, key=lambda p: chebyshev(p, target), default=None)
        if nxt is None or chebyshev(nxt, target) >= here:
            break
        at = nxt
    return at


def reaches(ctx: Ctx, foes) -> bool:
    """Whether any of ``foes`` can come within striking reach of her, chasing as a chaser does."""
    s = ctx.state
    return any(chebyshev(chase(ctx, e.pos, s.pos, ESCAPE_STEPS * 2), s.pos) <= REACH for e in foes)


def escape(ctx: Ctx, foes) -> Action | None:
    """The way out of a fight she would lose: toward the spot a few steps off where her lead over the chasers is
    greatest, each chaser played out as it moves (straight at her, stopped by what stands between), safe ground
    best of all. None when no spot gains on where she stands."""
    s = ctx.state
    bad = travel.hazards(s) | s.world.for_sale()
    occupied = {e.pos for e in s.world.entities if e.kind in ("npc", "character")}
    came = {s.pos: None}
    frontier, dist = [s.pos], {s.pos: 0}
    while frontier:
        nxt = []
        for p in frontier:
            if dist[p] >= ESCAPE_STEPS:
                continue
            for q in s.world.open_neighbours(p):
                if q in came or q in bad or q in occupied or not s.world.walkable(q):
                    continue
                came[q], dist[q] = p, dist[p] + 1
                nxt.append(q)
        frontier = nxt

    def lead(p: Pos) -> int:
        if p in s.world.view.safe:
            return SAFE_LEAD
        return min(chebyshev(chase(ctx, e.pos, p, dist[p]), p) for e in foes)

    best = max(came, key=lambda p: (lead(p), -dist[p]))
    if lead(best) <= max(lead(s.pos), REACH):
        return None
    path = []
    while best != s.pos:
        path.append(best)
        best = came[best]
    return actions.Walk(tuple(reversed(path)), "escape")


def off_hazard(ctx: Ctx) -> Action | None:
    """Standing on fire or lava: the nearest block that is not."""
    s = ctx.state
    if s.pos not in travel.hazards(s):
        return None
    bad = travel.hazards(s)
    open_ = [p for p in s.world.open_neighbours(s.pos) if p not in bad]
    if not open_:
        return None
    return actions.Walk((min(open_, key=lambda p: chebyshev(p, s.pos)),), "off the burning ground")


def _arm(ctx: Ctx, block: str, pos: Pos) -> int | None:
    s = ctx.state
    breaker = travel.weapon_for(s, block, ctx.memory, pos)
    return None if breaker is None or any(c.id == breaker and c.where == "armed" for c in s.carried) else breaker
