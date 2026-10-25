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

The runs give the chance we die, the chance the target dies, and the
health we can expect to lose. What those are worth is the engine's call.
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
RUNS = 32
HOSTILE_REACH = 1


@dataclass(frozen=True)
class Course:
    """One way to spend the next few seconds."""

    path: tuple[Pos, ...] = ()  # cells we step into, in order, then stand
    target: int | None = None  # the NPC we swing at whenever it is in reach
    heal: int = 0  # health a drink gives back at once


@dataclass(frozen=True)
class Odds:
    death: float  # the chance we die within the horizon
    kill: float  # the chance the target dies within the horizon
    health_lost: float  # expected, counting a death as all our health
    hits_taken: float  # expected swings that landed on us


@dataclass(frozen=True)
class _Foe:
    entity: Entity
    attack: int  # its attack power: the largest hit we have seen from its type, else the world's base
    hit: float  # its chance to hit us through our armor
    health: float


def threats_near(s: State) -> list[Entity]:
    """The threats that could reach us within the horizon at our pace."""
    reach = HORIZON_TICKS // step_ticks(s) + HOSTILE_REACH
    return [e for e in s.threats() if chebyshev(e.pos, s.pos) <= reach]


def odds(s: State, course: Course, runs: int = RUNS) -> Odds:
    armor = worn_defense(s.world)
    foes = []
    for e in threats_near(s):
        key = type_key_for_entity(e)
        attack = max(1, s.world.threat.damage_per_hit(key))
        chance = s.world.threat.hit_rate(key, hit_chance(attack_power=attack, defense=armor))
        foes.append(_Foe(e, attack, chance, float(health_left(e, s.world.npc_damage))))
    rng = random.Random(s.tick)  # the same moment gives the same odds
    deaths = kills = 0
    lost = hits = 0.0
    start = min(float(s.max_health), s.health + course.heal)
    for _ in range(runs):
        health, killed, landed = _run(s, course, foes, armor, start, rng)
        deaths += health <= 0
        kills += killed
        lost += start - max(0.0, health)
        hits += landed
    return Odds(deaths / runs, kills / runs, lost / runs, hits / runs)


def _run(s: State, course: Course, foes: list[_Foe], armor: int, health: float, rng: random.Random):
    here, path = s.pos, list(course.path)
    where = {f.entity.id: f.entity.pos for f in foes}
    alive = {f.entity.id: f.health for f in foes}
    period = step_ticks(s)
    weapon = fighting_weapon(s.armed)
    our_every = attack_interval_ticks(s.armed)
    our_top = max(1, BASE_ATTACK_POWER + (weapon_damage(weapon) or 0))
    our_hit = hit_chance()
    our_reach = s.world.attack_range or 1
    safe = s.world.view.safe
    landed = 0
    for t in range(1, HORIZON_TICKS + 1):
        if t % period == 0:
            if path:
                here = path.pop(0)
            for f in foes:
                if alive[f.entity.id] > 0:
                    where[f.entity.id] = _chase(s, where[f.entity.id], here)
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
                        return health, False, landed
            if fid == course.target and near <= our_reach and t % our_every == 0 and rng.random() < our_hit:
                alive[fid] -= rng.randint(1, our_top)
    killed = course.target is not None and alive.get(course.target, 1) <= 0
    return health, killed, landed


def step_ticks(s: State) -> int:
    return ticks_per_step(tick_rate_hz=SCALE_HZ, movement_speed_milli=max(1, s.world.movement_speed))


def _chase(s: State, at: Pos, us: Pos) -> Pos:
    """A chaser's step: the neighbouring block nearest us that it can stand on, or stay (Manual §11 Hostiles)."""
    if chebyshev(at, us) <= HOSTILE_REACH:
        return at
    best, best_d = at, chebyshev(at, us)
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            p = (at[0] + dx, at[1] + dy)
            if p == us or (p in s.world.view.tiles and not s.world.walkable(p)):
                continue
            d = chebyshev(p, us)
            if d < best_d:
                best, best_d = p, d
    return best
