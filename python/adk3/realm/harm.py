"""The danger model: what a course of action is likely to cost and win (docs/v3/SPEC.md **Engines**).

A course (stand, walk a path, fight one hostile, drink first) is played
forward ``RUNS`` times for ``HORIZON_TICKS``, with the dice rolled, by the
Manual's rules (§11 Hostiles, Combat):

- We step along our path at our movement speed, then stand.
- Each threat chases at our pace (no hostile's speed is published): it
  steps to the neighbouring block nearest us and stands still when none is
  nearer, so a wall or a river stops it. Whenever we are in its reach and
  not on safe ground it swings, on its cooldown: it hits on the published
  roll at its attack power against our armor, for 1 up to its attack power,
  less our armor.
- We swing at our target whenever it is in our weapon's reach and we are
  not on safe ground, on our weapon's cooldown: a hit on 65% of rolls at
  attack power 2, for 1 up to 2 plus the weapon's damage. A threat whose
  health runs out swings no more.
- A drink gives its health back at once.

- A hostile that keeps to its zone (a zone spawn never leaves it, Manual
  §11 Hostiles) steps only onto the ground its type has been seen keeping
  to, once we have seen it stop at a border (``Memory.territory``).
- Once the target is dead we go on along ``then``: the way the skill was
  going, so a fight is credited with the progress it frees.

The runs give the chance we die, the chance the target dies, the health we
can expect to lose and the steps of progress made. What those are worth is
the engine's call.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from agentrealm_adk.executor import ticks_per_step
from agentrealm_adk.supplies import weapon_damage
from agentrealm_adk.survival import (
    BASE_ATTACK_POWER,
    HOSTILE_ATTACK_INTERVAL_TICKS,
    attack_interval_ticks,
    fighting_weapon,
    health_left,
    hit_chance,
    worn_defense,
)
from agentrealm_adk.threat import type_key_for_entity
from agentrealm_adk.world import Entity, Pos, chebyshev

from .state import State

SCALE_HZ = 10  # the tick scale the combat estimates are in (agentrealm_adk.survival)
HORIZON_TICKS = 100  # ten seconds: long enough for a fight with a starting weapon to end
RUNS = 120  # enough that two options' odds differ by more than their noise
HOSTILE_REACH = 1


@dataclass(frozen=True)
class Course:
    """One way to spend the next few seconds."""

    path: tuple[Pos, ...] = ()  # cells we step into, in order, then stand
    target: int | None = None  # the NPC we swing at whenever it is in reach
    heal: int = 0  # health a drink gives back at once
    then: tuple[Pos, ...] = ()  # once the target is dead, the way on
    bar: float = 0.0  # health at which the fight is broken off: the target is left and ``away`` walked
    away: tuple[Pos, ...] = ()  # the way off when the fight is broken off


@dataclass(frozen=True)
class Odds:
    death: float  # the chance we die within the horizon
    kill: float  # the chance the target dies within the horizon
    health_lost: float  # expected, counting a death as all our health
    hits_taken: float  # expected swings that landed on us
    steps: float  # expected steps taken along the way on


@dataclass(frozen=True)
class _Foe:
    entity: Entity
    attack: int  # its attack power: the largest hit we have seen from its type, else the world's base
    hit: float  # its chance to hit us through our armor
    health: float
    ground: frozenset[str] | None  # the block types it keeps to, when it has shown a border


def threats_near(s: State) -> list[Entity]:
    """The threats that could reach us within the horizon at our pace."""
    reach = HORIZON_TICKS // step_ticks(s) + HOSTILE_REACH
    return [e for e in s.threats() if chebyshev(e.pos, s.pos) <= reach]


def odds(s: State, course: Course, runs: int = RUNS, seed: int | None = None,
         territory: dict[str, list[str]] | None = None) -> Odds:
    """``seed`` fixes the dice, so options weighed in one engagement are compared on the same rolls."""
    armor = worn_defense(s.world)
    foes = []
    for e in threats_near(s):
        key = type_key_for_entity(e)
        attack = max(1, s.world.threat.damage_per_hit(key))
        chance = s.world.threat.hit_rate(key, hit_chance(attack_power=attack, defense=armor))
        ground = frozenset((territory or {}).get(e.code, ())) or None
        foes.append(_Foe(e, attack, chance, float(health_left(e, s.world.npc_damage)), ground))
    deaths = kills = 0
    lost = hits = steps = 0.0
    start = min(float(s.max_health), s.health + course.heal)
    for i in range(runs):
        rng = random.Random((s.tick if seed is None else seed) * 1000 + i)  # run i rolls the same dice for every option
        health, killed, landed, walked = _run(s, course, foes, armor, start, rng)
        deaths += health <= 0
        kills += killed
        lost += start - max(0.0, health)
        hits += landed
        steps += walked
    return Odds(deaths / runs, kills / runs, lost / runs, hits / runs, steps / runs)


def _run(s: State, course: Course, foes: list[_Foe], armor: int, health: float, rng: random.Random):
    here, path, then = s.pos, list(course.path), list(course.then)
    where = {f.entity.id: f.entity.pos for f in foes}
    alive = {f.entity.id: f.health for f in foes}
    period = step_ticks(s)
    weapon = fighting_weapon(s.armed)
    our_every = attack_interval_ticks(s.armed)
    our_top = max(1, BASE_ATTACK_POWER + (weapon_damage(weapon) or 0))
    our_hit = hit_chance()
    our_reach = s.world.attack_range or 1
    safe = s.world.view.safe
    landed = walked = 0
    target = course.target
    for t in range(1, HORIZON_TICKS + 1):
        if target is not None and alive.get(target, 0) > 0 and health <= course.bar:
            target, path, then = None, list(course.away), []  # the bar: break off
        if t % period == 0:
            if target is not None and alive.get(target, 0) <= 0 and then:
                path, then = then, []  # the fight is won: go on
            if path:
                here = path.pop(0)
                walked += 1
            for f in foes:
                if alive[f.entity.id] > 0:
                    where[f.entity.id] = _chase(s, where[f.entity.id], here, f.ground)
        if here in safe:
            continue
        for f in foes:
            fid = f.entity.id
            if alive[fid] <= 0:
                continue
            near = chebyshev(where[fid], here)
            if near <= HOSTILE_REACH and t % HOSTILE_ATTACK_INTERVAL_TICKS == fid % HOSTILE_ATTACK_INTERVAL_TICKS:
                if rng.random() < f.hit:
                    health -= max(0, rng.randint(1, f.attack) - armor)
                    landed += 1
                    if health <= 0:
                        return health, False, landed, walked
            if fid == target and near <= our_reach and t % our_every == 0 and rng.random() < our_hit:
                alive[fid] -= rng.randint(1, our_top)
    killed = course.target is not None and alive.get(course.target, 1) <= 0
    walked = walked if target is None or killed else 0  # steps on count only once the fight is over
    return health, killed, landed, walked


def step_ticks(s: State) -> int:
    return ticks_per_step(tick_rate_hz=SCALE_HZ, movement_speed_milli=max(1, s.world.movement_speed))


def _chase(s: State, at: Pos, us: Pos, ground: frozenset[str] | None = None) -> Pos:
    """A chaser's step: the neighbouring block nearest us that it can stand on, or stay (Manual §11 Hostiles).
    One that keeps to its zone steps only onto the ground it keeps to."""
    if chebyshev(at, us) <= HOSTILE_REACH:
        return at
    best, best_d = at, chebyshev(at, us)
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            p = (at[0] + dx, at[1] + dy)
            block = s.world.view.tiles.get(p)
            if p == us or (block is not None and not s.world.walkable(p)) or (ground and block not in ground):
                continue
            d = chebyshev(p, us)
            if d < best_d:
                best, best_d = p, d
    return best
