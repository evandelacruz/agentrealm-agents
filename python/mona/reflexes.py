"""Reflexes: a static priority list, checked every tick (docs/v4/SPEC.md **Reflexes**).

The first that applies acts. Each runs to its end because its condition holds
until then (the berry eaten, the enemy dead); none changes the destination.
"""

from __future__ import annotations

from collections.abc import Callable

from agentrealm_adk import supplies
from agentrealm_adk.world import Entity, Pos, chebyshev

from adk3.realm import actions, travel
from adk3.realm.actions import Action
from adk3.realm.context import Ctx
from adk3.realm.memory import place_key

from . import route

FIGHT_WITHIN = 10  # blocks: a hostile this near is fought (better to attack than defend)
TAKE_WITHIN = 25  # blocks: food, gems, gear, chests, signs and people this near are gone for
READ_REACH = 5  # a sign is read from this near (in sight in any light)
SAY_REACH = 20  # a Say reaches an NPC 25 blocks off (Manual §11 Speech); a little inside it
DRINK_AT = 3  # health: at or below, with a hostile near, a carried potion is drunk (the get health mark)
STRIKE_NEAR = 3  # blocks: a hostile this near can strike within a step or two
ROUTED = 6  # the nearest this many in a straight line are routed to find the nearest by the walk
CONTACT = 2  # blocks: turned from a fight, nothing this near a hostile is gone for (it would hold her in the hits)


def on_fire(ctx: Ctx, turned: bool) -> Action | None:
    return route.off_hazard(ctx)


def ready(ctx: Ctx, action: Action | None) -> Action | None:
    """The standing rule, always armed with her best weapon and wearing her armor, applied to what was decided:
    when the action moves or deals and needs nothing in hand, the weapon goes back first. An action that arms or
    uses something (a swing, a break, a drink) or waits on one keeps the slot until its use lands."""
    if action is not None and not isinstance(action, (actions.Walk, actions.Take, actions.Withdraw, actions.Read, actions.Say)):
        return action
    s = ctx.state
    best = s.best_weapon()
    if best is not None and best.where != "armed":
        return actions.Equip(best.id, "Arm", best.code)
    armor = s.unworn()
    return actions.Equip(armor.id, "Wear", armor.code) if armor is not None else action


def drink(ctx: Ctx, turned: bool = False) -> Action | None:
    """A potion only against death close at hand: at 3 or below with a hostile near enough to strike next. Any other
    time food heals her (berries first, always); the potions are the kit's."""
    s = ctx.state
    if s.health > DRINK_AT or not any(chebyshev(e.pos, s.pos) <= STRIKE_NEAR for e in s.threats()):
        return None
    potion = min((c for c in s.carried if supplies.is_potion(c.code)), key=lambda c: supplies.heal_of(c.code), default=None)
    return actions.Drink(potion.id, potion.where == "armed", potion.code) if potion is not None else None


def fight(ctx: Ctx, turned: bool = False) -> Action | None:
    if turned:
        return None
    s = ctx.state
    foes = [e for e in s.threats() if chebyshev(e.pos, s.pos) <= FIGHT_WITHIN]
    if not foes:
        return None
    return route.strike(ctx, min(foes, key=lambda e: chebyshev(e.pos, s.pos)))


def take(ctx: Ctx, turned: bool = False) -> Action | None:
    """The nearest thing worth taking within 25 blocks: gems, a heart, free gear, a chest not yet looked into, and,
    when hurt, food or a bush to cut for its berry (food is eaten on pickup, so at full health it is left lying)."""
    s, mem = ctx.state, ctx.memory
    hurt = s.health < s.max_health
    found: list[tuple[int, str, object]] = []
    for e in s.supplies():
        if e.gem_price or s.world.is_our_drop(e.id) or not _near(s, e.pos, turned) or place_key(s.map_id, e.pos) in mem.no_effect:
            continue
        food = supplies.eaten_on_pickup(e.code) and supplies.heal_of(e.code) > 0
        if hurt or not food:
            found.append((chebyshev(e.pos, s.pos), "supply", e))
    for e in s.chests():
        key = place_key(s.map_id, e.pos)
        if _near(s, e.pos, turned) and key not in mem.no_effect and (key not in mem.chests_seen or s.world.chest_contents.get(e.id)):
            found.append((chebyshev(e.pos, s.pos), "chest", e))
    weapon = travel.weapon_for(s, "bush") if hurt else None
    if weapon is not None:
        found += [(chebyshev(p, s.pos), "bush", p) for p, b in s.world.view.tiles.items()
                  if b == "bush" and _near(s, p, turned) and place_key(s.map_id, p) not in mem.no_effect]
    kind, what = _nearest_by_route(ctx, found)
    if kind is None:
        return None
    if kind == "supply":
        return _take(ctx, what)
    if kind == "chest":
        if chebyshev(what.pos, s.pos) > 1:
            return route.beside(ctx, what.pos, 1, "to a chest")
        return actions.Withdraw(what.id)
    if chebyshev(what, s.pos) > 1:
        return route.beside(ctx, what, 1, "to a bush for its berry")
    if not s.attack_ready:
        return actions.Stop("waiting to swing")
    armed = any(c.id == weapon and c.where == "armed" for c in s.carried)
    return actions.UseOn(what[0], what[1], None if armed else weapon, "cut a bush for its berry")


def _nearest_by_route(ctx: Ctx, found: list[tuple[int, str, object]]) -> tuple[str | None, object]:
    """The nearest by the walk to it, not as the crow flies: a berry three blocks off behind a row of trees is far.
    The few nearest in a straight line are routed (the walk is never shorter), each break costing what it takes."""
    best: tuple[int, str | None, object] = (TAKE_WITHIN + 1, None, None)
    for line, kind, what in sorted(found, key=lambda f: f[0])[:ROUTED]:
        if line >= best[0]:
            break
        pos = what if kind == "bush" else what.pos
        steps = route.steps(ctx, travel.to_beside(ctx, pos, 1))
        if steps is not None and steps < best[0]:
            best = (steps, kind, what)
    return best[1], best[2]


def learn(ctx: Ctx, turned: bool = False) -> Action | None:
    """A sign not read, or a person not heard."""
    s, mem = ctx.state, ctx.memory
    signs = [p for p in s.signs() if _near(s, p, turned) and not mem.was_read(s.map_id, p)]
    if signs:
        p = min(signs, key=lambda q: chebyshev(q, s.pos))
        return actions.Read(*p) if chebyshev(p, s.pos) <= READ_REACH else route.beside(ctx, p, READ_REACH, "to a sign")
    if not s.speech_ready:
        return None
    people = [e for e in s.npcs() if not s.is_threat(e) and _near(s, e.pos, turned) and not mem.heard(s.map_id, e.pos)]
    if not people:
        return None
    e = min(people, key=lambda q: chebyshev(q.pos, s.pos))
    return actions.Say(e.id, e.code) if chebyshev(e.pos, s.pos) <= SAY_REACH else route.beside(ctx, e.pos, SAY_REACH, f"to {e.code}")


REFLEXES: list[tuple[str, Callable[[Ctx, bool], Action | None]]] = [
    ("off the fire", on_fire),
    ("drink", drink),  # 3 or below with a potion: drunk before anything else
    ("fight", fight),  # a hostile on her is fought, hurt or not: only get health (3 and below) turns her from it
    ("take", take),  # the nearest of gems, gear, chests and, when hurt, food
    ("learn", learn),
]


PARKING = ("off the fire", "drink", "fight")  # stopping, she goes to safe ground: these still come first


def first(ctx: Ctx, turned: bool = False, only: tuple[str, ...] | None = None) -> tuple[str, Action] | None:
    """The first reflex that applies and can act, with its action. ``turned`` while get health runs, the one thing
    that turns her from a fight: she does not fight, and goes for nothing a hostile stands by, so she breaks contact."""
    for name, reflex in REFLEXES:
        if only is not None and name not in only:
            continue
        action = reflex(ctx, turned)
        if action is not None:
            return name, action
    return None


def _near(s, p: Pos, turned: bool = False) -> bool:
    if chebyshev(p, s.pos) > TAKE_WITHIN:
        return False
    return not turned or all(chebyshev(e.pos, p) > CONTACT for e in s.threats())


def _take(ctx: Ctx, e: Entity) -> Action | None:
    if chebyshev(e.pos, ctx.state.pos) <= 1:
        return actions.Take(e.id, e.code)
    return route.beside(ctx, e.pos, 1, f"to the {e.code}")

