"""Survival params, retreat threshold and win estimate (A9, PLAYABLE_AGENT_PLAN Health and lives)."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from .threat import ThreatTable, type_key_for_entity
from .world import Entity, Pos, WorldModel, chebyshev
from .zone_discovery import safe_tiles

if TYPE_CHECKING:
    from .config import Policy

# Stated assumptions until measured (GAME_NOTES.md Open questions, "Assumed
# until measured"). Only the win estimate reads them; A23 refines it.
HOSTILE_ATTACK_INTERVAL_TICKS = 15
OUR_ATTACK_INTERVAL_TICKS = 10
OUR_DAMAGE_PER_HIT = 1
UNKILLED_HOSTILE_HEALTH = 10
NEW_CHARACTER_HEALTH = 10
GROUP_JOIN_RADIUS = 2  # hostiles within this of the focus join the fight (PLAYABLE_AGENT_PLAN Fight)
# How long an Attacked or Damaged keeps us threatened with no hostile in range:
# two of a hostile's swings, so a pursuer stepping just out of range between
# hits does not hand the decision back to Explore (A9, A58 run 9).
THREAT_MEMORY_TICKS = 2 * HOSTILE_ATTACK_INTERVAL_TICKS


def effective_risk(risk: float, lives: int, lives_floor: int) -> float:
    """``risk × min(1, (lives − lives_floor) / lives_floor)``, floored at 0."""
    if lives_floor < 1:
        return 0.0
    headroom = lives - lives_floor
    if headroom <= 0:
        return 0.0
    return max(0.0, risk * min(1.0, headroom / lives_floor))


def effective_retreat_hits(retreat_hits: int, eff_risk: float) -> int:
    """``retreat_hits + round(1 − 2 × effective risk)``, never below 1 (halves round up)."""
    return max(1, int(retreat_hits) + math.floor(1 - 2 * eff_risk + 0.5))


def effective_fight_margin(fight_margin: float, eff_risk: float) -> float:
    return fight_margin * (1.5 - eff_risk)


def hostiles_in_range(w: WorldModel, policy: Policy) -> list[Entity]:
    if w.pos is None:
        return []
    here = w.pos
    return [
        e
        for e in w.entities
        if e.kind in policy.hostile and chebyshev(e.pos, here) <= policy.hostile_range
    ]


def recently_attacked(w: WorldModel) -> bool:
    """An Attacked or Damaged landed on us within ``THREAT_MEMORY_TICKS``."""
    return w.attacked_tick is not None and w.tick - w.attacked_tick <= THREAT_MEMORY_TICKS


def threatened(w: WorldModel, policy: Policy) -> bool:
    """A hostile is in range, or one hit us recently (A9).

    While this holds only the survival states may take the decision.
    ``on_hostile = "ignore"`` is never threatened, and neither is a known
    safe tile, where nothing can hurt us.
    """
    if policy.on_hostile == "ignore" or not w.alive or w.pos is None or on_safe_tile(w):
        return False
    return bool(hostiles_in_range(w, policy)) or recently_attacked(w)


def flee_from(w: WorldModel, policy: Policy) -> list[Entity]:
    """The hostiles Flee runs from: those in range, or every one in view while recently attacked.

    A pursuer that steps just past ``hostile_range`` between its hits is
    still chasing us (A58 run 9).
    """
    in_range = hostiles_in_range(w, policy)
    if in_range or not recently_attacked(w):
        return in_range
    return [e for e in w.entities if e.kind in policy.hostile]


def combat_group(w: WorldModel, policy: Policy) -> list[Entity]:
    """Hostiles in range plus any within ``GROUP_JOIN_RADIUS`` of the nearest."""
    in_range = hostiles_in_range(w, policy)
    if not in_range or w.pos is None:
        return []
    focus = min(in_range, key=lambda e: (chebyshev(e.pos, w.pos), e.id))
    group = [focus]
    for e in w.entities:
        if e.kind not in policy.hostile or e.id == focus.id:
            continue
        if chebyshev(e.pos, focus.pos) <= GROUP_JOIN_RADIUS and e not in group:
            group.append(e)
    return group


def max_hit_damage(group: list[Entity], threat: ThreatTable) -> int:
    if not group:
        return 0
    return max(threat.damage_per_hit(type_key_for_entity(e)) for e in group)


def retreat_by_health(health: int | None, retreat_hits: int, hit_damage: int) -> bool:
    if health is None or hit_damage <= 0:
        return False
    return health <= retreat_hits * hit_damage


def on_safe_tile(w: WorldModel) -> bool:
    if w.map_id is None or w.pos is None:
        return False
    fact = w.zones.get(w.map_id, {}).get(w.pos)
    return fact is not None and fact.safe


def nearest_safe_goal(w: WorldModel) -> Pos | None:
    """Nearest known safe cell on the current map, or None."""
    if w.map_id is None or w.pos is None:
        return None
    here = w.pos
    safes = safe_tiles(w, w.map_id)
    if not safes:
        return None
    if here in safes:
        return here
    return min(safes, key=lambda p: (chebyshev(p, here), p))


def ticks_to_kill_us(health: int, group: list[Entity], threat: ThreatTable) -> float:
    if health <= 0 or not group:
        return float("inf")
    dps = sum(threat.damage_per_hit(type_key_for_entity(e)) for e in group) / HOSTILE_ATTACK_INTERVAL_TICKS
    if dps <= 0:
        return float("inf")
    return health / dps


def ticks_to_kill_them(group: list[Entity]) -> float:
    if not group:
        return float("inf")
    return len(group) * UNKILLED_HOSTILE_HEALTH / (OUR_DAMAGE_PER_HIT / OUR_ATTACK_INTERVAL_TICKS)


def win_ratio(health: int | None, group: list[Entity], threat: ThreatTable) -> float:
    """Ticks for them to kill us, over ticks for us to kill them. Higher is better for us."""
    if health is None or not group:
        return float("inf")
    return ticks_to_kill_us(health, group, threat) / ticks_to_kill_them(group)


def has_unmeasured_type(w: WorldModel, group: list[Entity]) -> bool:
    for e in group:
        key = type_key_for_entity(e)
        if key is None or not w.threat.measured(key):
            return True
    return False


def would_lose(w: WorldModel, policy: Policy, params: dict[str, float | int]) -> bool:
    """True when the win estimate is below the effective fight margin.

    **Fight** (A23) and **Flee** gate on this; **Retreat** also fires when
    the group outclasses us.
    """
    group = combat_group(w, policy)
    if not group:
        return False
    eff_risk = effective_risk(float(params["risk"]), w.lives, int(params["lives_floor"]))
    if eff_risk < 0.5 and has_unmeasured_type(w, group):
        return True
    health = w.health if w.health is not None else w.max_health
    if health is None:
        health = NEW_CHARACTER_HEALTH
    margin = effective_fight_margin(float(params["fight_margin"]), eff_risk)
    return win_ratio(health, group, w.threat) <= margin


def should_retreat(w: WorldModel, policy: Policy, params: dict[str, float | int]) -> bool:
    """The next effective ``retreat_hits`` hits from the hostiles in range could kill.

    A hit's size comes from what is attacking (the threat table), so with no
    hostile in range there is nothing to retreat from. ``on_hostile = "ignore"``
    never retreats.
    """
    if policy.on_hostile == "ignore" or on_safe_tile(w):
        return False
    group = combat_group(w, policy)
    if not group:
        return False
    if policy.on_hostile == "fight" and would_lose(w, policy, params):
        return True
    eff = effective_risk(float(params["risk"]), w.lives, int(params["lives_floor"]))
    hits = effective_retreat_hits(int(params["retreat_hits"]), eff)
    return retreat_by_health(w.health, hits, max_hit_damage(group, w.threat))
