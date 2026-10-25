"""Expected health after an action: the one model of danger (docs/v3/SPEC.md **Controller**).

Every action is played forward for ``HORIZON_TICKS``, about the time an
engagement takes to resolve, from how the game works (docs/GAME_NOTES.md
How hostiles move, Use):

- We follow the action: a walk steps along its path at our movement speed,
  anything else stands still.
- Each threat chases at our pace, stepping to the neighbouring cell that
  brings it closest, standing still when none does (a wall or a fence stops
  it), and swings whenever it is within reach and we are not on safe ground.
- A swing at a threat repeats on our weapon's cooldown until it dies; a dead
  threat swings no more.
- A drink gives its health back at once.

Walking on, stepping away, fighting and drinking are then priced on one
scale, with no distance thresholds: a chaser keeps hitting a walk that does
not reach safe ground, and a fight we win stops the hits sooner.
"""

from __future__ import annotations

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

from adk3.core.interfaces import Action

from .state import State, threat_of

TICK_HZ = 10  # the scale the combat estimates are in (survival.HOSTILE_ATTACK_INTERVAL_TICKS)
HORIZON_TICKS = 100  # ten seconds: long enough for a fight with a starting weapon to end
HOSTILE_REACH = 1


def health_after(s: State, a: Action) -> float:
    """Our expected health once ``a`` has played out over the horizon."""
    threats = [e for e in s.npcs() if threat_of(s, e) > 0
               and chebyshev(e.pos, s.pos) <= HORIZON_TICKS // step_ticks(s) + HOSTILE_REACH]
    health = float(s.health) + a.data.get("heal", 0)
    if not threats:
        return health
    path: list[Pos] = list(a.data.get("path") or [])[:len(a.data.get("intents") or []) or None]
    if a.kind == "walk":
        path = list(a.data.get("path") or [])
    elif a.kind in ("flee",):
        path = list(a.data.get("path") or [a.data["at"]])
    else:
        path = []
    period = step_ticks(s)
    target = a.data.get("npc") if a.kind == "attack" else None
    kill_at = kill_ticks(s, target) if target is not None else None
    armor = worn_defense(s.w)
    hits = {e.id: threat_of(s, e) * hostile_swing_damage(s.w.threat.damage_per_hit(type_key_for_entity(e)), s.w.threat,
                                                         type_key_for_entity(e), armor) for e in threats}
    where = {e.id: e.pos for e in threats}
    here = s.pos
    for t in range(1, HORIZON_TICKS + 1):
        if path and t % period == 0:
            here = path.pop(0)
        if t % period == 0:
            for e in threats:
                where[e.id] = chase(s, where[e.id], here)
        if here in s.w.view.safe:
            continue
        for e in threats:
            if kill_at is not None and e.id == target.id and t >= kill_at:
                continue
            if chebyshev(where[e.id], here) <= HOSTILE_REACH:
                health -= hits[e.id] / HOSTILE_ATTACK_INTERVAL_TICKS
    return health


def step_ticks(s: State) -> int:
    return ticks_per_step(tick_rate_hz=TICK_HZ, movement_speed_milli=max(1, s.w.movement_speed))


def kill_ticks(s: State, e: Entity) -> float:
    """Ticks our weapon takes to kill ``e``, at its expected swing on its cooldown."""
    per_swing = swing_damage(s.armed)
    if per_swing <= 0:
        return float("inf")
    return health_left(e, s.w.npc_damage) / per_swing * attack_interval_ticks(s.armed)


def chase(s: State, at: Pos, us: Pos) -> Pos:
    """A chaser's step: the neighbouring cell nearest us that it can stand on, or stay."""
    if chebyshev(at, us) <= HOSTILE_REACH:
        return at
    best, best_d = at, chebyshev(at, us)
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            p = (at[0] + dx, at[1] + dy)
            if p == us or (p in s.w.view.tiles and not s.w.walkable(p)):
                continue
            d = chebyshev(p, us)
            if d < best_d:
                best, best_d = p, d
    return best
