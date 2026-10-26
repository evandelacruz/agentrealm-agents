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


def on_fire(ctx: Ctx) -> Action | None:
    return route.off_hazard(ctx)


def armed(ctx: Ctx) -> Action | None:
    """Always armed with her best weapon and wearing her armor: a potion or a tool is armed only for its use (one
    action), and the weapon goes back on the next tick."""
    s = ctx.state
    best = s.best_weapon()
    if best is not None and best.where != "armed":
        return actions.Equip(best.id, "Arm", best.code)
    armor = s.unworn()
    return actions.Equip(armor.id, "Wear", armor.code) if armor is not None else None


def eat(ctx: Ctx) -> Action | None:
    """Hurt: food or a heart near, else a bush to cut for its berry (food is eaten on pickup: none at full health)."""
    s = ctx.state
    if s.health >= s.max_health:
        return None
    food = [e for e in s.supplies() if not e.gem_price and supplies.eaten_on_pickup(e.code)
            and supplies.heal_of(e.code) > 0 and _near(s, e.pos)]
    if food:
        return _take(ctx, min(food, key=lambda e: chebyshev(e.pos, s.pos)))
    weapon = travel.weapon_for(s, "bush")
    if weapon is None:
        return None
    bushes = [p for p, b in s.world.view.tiles.items()
              if b == "bush" and _near(s, p) and place_key(s.map_id, p) not in ctx.memory.no_effect]
    if not bushes:
        return None
    bush = min(bushes, key=lambda p: chebyshev(p, s.pos))
    if chebyshev(bush, s.pos) > 1:
        return route.beside(ctx, bush, 1, "to a bush for its berry")
    if not s.attack_ready:
        return actions.Stop("waiting to swing")
    armed = any(c.id == weapon and c.where == "armed" for c in s.carried)
    return actions.UseOn(bush[0], bush[1], None if armed else weapon, "cut a bush for its berry")


def fight(ctx: Ctx) -> Action | None:
    s = ctx.state
    foes = [e for e in s.threats() if chebyshev(e.pos, s.pos) <= FIGHT_WITHIN]
    if not foes:
        return None
    return route.strike(ctx, min(foes, key=lambda e: chebyshev(e.pos, s.pos)))


def loot(ctx: Ctx) -> Action | None:
    """Gems, a heart, free gear and chests not yet looked into."""
    s, mem = ctx.state, ctx.memory
    things = [e for e in s.supplies() if not e.gem_price and not s.world.is_our_drop(e.id) and _near(s, e.pos)
              and not (supplies.eaten_on_pickup(e.code) and supplies.heal_of(e.code) > 0)  # food waits for hurt
              and place_key(s.map_id, e.pos) not in mem.no_effect]  # gems, hearts and free gear
    if things:
        return _take(ctx, min(things, key=lambda e: chebyshev(e.pos, s.pos)))
    chests = [e for e in s.chests() if _near(s, e.pos) and place_key(s.map_id, e.pos) not in mem.no_effect
              and (place_key(s.map_id, e.pos) not in mem.chests_seen or s.world.chest_contents.get(e.id))]
    if not chests:
        return None
    chest = min(chests, key=lambda e: chebyshev(e.pos, s.pos))
    if chebyshev(chest.pos, s.pos) > 1:
        return route.beside(ctx, chest.pos, 1, "to a chest")
    return actions.Withdraw(chest.id)


def learn(ctx: Ctx) -> Action | None:
    """A sign not read, or a person not heard."""
    s, mem = ctx.state, ctx.memory
    signs = [p for p in s.signs() if _near(s, p) and not mem.was_read(s.map_id, p)]
    if signs:
        p = min(signs, key=lambda q: chebyshev(q, s.pos))
        return actions.Read(*p) if chebyshev(p, s.pos) <= READ_REACH else route.beside(ctx, p, READ_REACH, "to a sign")
    if not s.speech_ready:
        return None
    people = [e for e in s.npcs() if not s.is_threat(e) and _near(s, e.pos) and not mem.heard(s.map_id, e.pos)]
    if not people:
        return None
    e = min(people, key=lambda q: chebyshev(q.pos, s.pos))
    return actions.Say(e.id, e.code) if chebyshev(e.pos, s.pos) <= SAY_REACH else route.beside(ctx, e.pos, SAY_REACH, f"to {e.code}")


REFLEXES: list[tuple[str, Callable[[Ctx], Action | None]]] = [
    ("off the fire", on_fire),
    ("armed", armed),  # a standing rule: never walk or fight empty-handed
    ("fight", fight),  # a hostile on her is fought, hurt or not: only get health (3 and below) turns her from it
    ("eat", eat),
    ("loot", loot),
    ("learn", learn),
]


def first(ctx: Ctx, fight: bool = True) -> tuple[str, Action] | None:
    """The first reflex that applies and can act, with its action; ``fight`` False while get health runs, the one
    thing that turns her from a fight."""
    for name, reflex in REFLEXES:
        if name == "fight" and not fight:
            continue
        action = reflex(ctx)
        if action is not None:
            return name, action
    return None


def _near(s, p: Pos) -> bool:
    return chebyshev(p, s.pos) <= TAKE_WITHIN


def _take(ctx: Ctx, e: Entity) -> Action | None:
    if chebyshev(e.pos, ctx.state.pos) <= 1:
        return actions.Take(e.id, e.code)
    return route.beside(ctx, e.pos, 1, f"to the {e.code}")

