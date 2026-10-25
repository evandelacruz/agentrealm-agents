"""The danger model: our expected health after a course of action (docs/v3/SPEC.md **Engines**).

A course of action (stand, walk a path, fight one hostile, drink first) is
played forward tick by tick for ``HORIZON_TICKS``, by the Manual's rules
(§11 Hostiles, Combat):

- We step along our path at our movement speed, then stand.
- Each threat chases at our pace (no hostile's speed is published): it
  steps to the neighbouring block nearest us and stands still when none is
  nearer, so a wall or a river stops it. It swings whenever we are in its
  reach and not on safe ground, at its measured hit and swing interval.
- We swing at our target whenever it is in our weapon's reach and we are
  not on safe ground, at our weapon's expected hit and cooldown, until its
  health runs out; a dead threat swings no more.
- A drink gives its health back at once.

Every option is priced on one scale, our health at the end, so walking on
under attack, backing off, fighting and drinking compare directly.
"""

from __future__ import annotations

from dataclasses import dataclass

from agentrealm_adk.executor import ticks_per_step
from agentrealm_adk.survival import (
    HOSTILE_ATTACK_INTERVAL_TICKS,
    attack_interval_ticks,
    health_left,
    hostile_swing_damage,
    swing_damage,
    worn_defense,
)
from agentrealm_adk.threat import type_key_for_entity
from agentrealm_adk.world import Entity, Pos, chebyshev

from .state import State

SCALE_HZ = 10  # the combat estimates' tick scale (agentrealm_adk.survival)
HORIZON_TICKS = 100  # ten seconds: long enough for a fight with a starting weapon to end
HOSTILE_REACH = 1


@dataclass(frozen=True)
class Course:
    """One way to spend the next few seconds."""

    path: tuple[Pos, ...] = ()  # cells we step into, in order, then stand
    target: int | None = None  # the NPC we swing at whenever it is in reach
    heal: int = 0  # health a drink gives back at once


@dataclass(frozen=True)
class Result:
    health: float  # our expected health at the end
    killed: bool  # the target died in time


@dataclass
class _Threat:
    entity: Entity
    pos: Pos
    hit: float  # expected damage of one of its swings at us, through our armor
    health: float


def threats_near(s: State) -> list[Entity]:
    """The threats that could reach us within the horizon at our pace."""
    reach = HORIZON_TICKS // step_ticks(s) + HOSTILE_REACH
    return [e for e in s.threats() if chebyshev(e.pos, s.pos) <= reach]


def play_out(s: State, course: Course) -> Result:
    armor = worn_defense(s.world)
    threats = [
        _Threat(e, e.pos, hostile_swing_damage(s.world.threat.damage_per_hit(type_key_for_entity(e)), s.world.threat,
                                               type_key_for_entity(e), armor),
                float(health_left(e, s.world.npc_damage)))
        for e in threats_near(s)
    ]
    health = min(float(s.max_health), s.health + course.heal)
    here, path = s.pos, list(course.path)
    period = step_ticks(s)
    our_rate = swing_damage(s.armed) / attack_interval_ticks(s.armed)
    our_reach = s.world.attack_range or 1
    safe = s.world.view.safe
    for t in range(1, HORIZON_TICKS + 1):
        if t % period == 0:
            if path:
                here = path.pop(0)
            for th in threats:
                if th.health > 0:
                    th.pos = _chase(s, th.pos, here)
        if here in safe:
            continue
        for th in threats:
            if th.health <= 0:
                continue
            if chebyshev(th.pos, here) <= HOSTILE_REACH:
                health -= th.hit / HOSTILE_ATTACK_INTERVAL_TICKS
            if th.entity.id == course.target and chebyshev(th.pos, here) <= our_reach:
                th.health -= our_rate
    killed = any(th.entity.id == course.target and th.health <= 0 for th in threats)
    return Result(health, killed)


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
