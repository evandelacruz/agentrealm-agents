"""Survival params and retreat thresholds (A9, PLAYABLE_AGENT_PLAN Health and lives)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .directives import PARAM_DEFAULTS
from .threat import ThreatTable, type_key_for_entity
from .world import Entity, Pos, WorldModel, chebyshev
from .zone_discovery import safe_tiles

if TYPE_CHECKING:
    from .config import Policy

# Conservative combat timing when intervals are not yet learned (A9; A23 refines).
ASSUMED_ATTACK_INTERVAL_TICKS = 10
ASSUMED_ENEMY_HEALTH = 10  # assumption until a type is killed (PLAYABLE_AGENT_PLAN Combat)
ASSUMED_OUR_DAMAGE_PER_HIT = 1
GROUP_JOIN_RADIUS = 2  # hostiles within this of the focus join the fight (PLAYABLE_AGENT_PLAN Fight)


def effective_risk(risk: float, lives: int, lives_floor: int) -> float:
    """``risk × min(1, (lives − lives_floor) / lives_floor)``, floored at 0."""
    if lives_floor < 1:
        return 0.0
    headroom = lives - lives_floor
    if headroom <= 0:
        return 0.0
    return max(0.0, risk * min(1.0, headroom / lives_floor))


def effective_retreat_hits(retreat_hits: int, eff_risk: float) -> int:
    """``retreat_hits + round(1 − 2 × effective risk)``, never below 1."""
    return max(1, int(retreat_hits) + round(1 - 2 * eff_risk))


def effective_fight_margin(fight_margin: float, eff_risk: float) -> float:
    return fight_margin * (1.5 - eff_risk)


def params_from(raw: dict[str, float | int] | None) -> dict[str, float | int]:
    out = dict(PARAM_DEFAULTS)
    if raw:
        for k, v in raw.items():
            if k in out:
                out[k] = v
    return out


def hostiles_in_range(w: WorldModel, policy: Policy) -> list[Entity]:
    if w.pos is None:
        return []
    here = w.pos
    return [
        e
        for e in w.entities
        if e.kind in policy.hostile and chebyshev(e.pos, here) <= policy.hostile_range
    ]


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
    dps = 0.0
    for e in group:
        dmg = threat.damage_per_hit(type_key_for_entity(e))
        dps += dmg / ASSUMED_ATTACK_INTERVAL_TICKS
    if dps <= 0:
        return float("inf")
    return health / dps


def ticks_to_kill_them(group: list[Entity]) -> float:
    if not group:
        return float("inf")
    total_health = len(group) * ASSUMED_ENEMY_HEALTH
    our_dps = ASSUMED_OUR_DAMAGE_PER_HIT / ASSUMED_ATTACK_INTERVAL_TICKS
    return total_health / our_dps


def win_ratio(health: int | None, group: list[Entity], threat: ThreatTable) -> float:
    """Ticks for them to kill us, over ticks for us to kill them. Higher is better for us."""
    if health is None or not group:
        return float("inf")
    them = ticks_to_kill_us(health, group, threat)
    us = ticks_to_kill_them(group)
    if us <= 0:
        return 0.0
    return them / us


def has_unmeasured_type(w: WorldModel, group: list[Entity]) -> bool:
    for e in group:
        key = type_key_for_entity(e)
        if key is None or not w.threat.measured(key):
            return True
    return False


def would_lose(
    w: WorldModel,
    policy: Policy,
    params: dict[str, float | int],
    *,
    eff_risk: float | None = None,
) -> bool:
    """True when the win estimate is below the effective fight margin."""
    group = combat_group(w, policy)
    if not group:
        return False
    eff_risk = eff_risk if eff_risk is not None else effective_risk(
        float(params["risk"]), w.lives, int(params["lives_floor"])
    )
    if eff_risk < 0.5 and has_unmeasured_type(w, group):
        return True
    health = w.health if w.health is not None else w.max_health
    if health is None:
        health = ASSUMED_ENEMY_HEALTH
    margin = effective_fight_margin(float(params["fight_margin"]), eff_risk)
    return win_ratio(health, group, w.threat) <= margin


def threat_outclasses(w: WorldModel, policy: Policy, params: dict[str, float | int]) -> bool:
    """The fight group would beat us (PLAYABLE_AGENT_PLAN Retreat guard)."""
    return would_lose(w, policy, params)


def should_retreat(w: WorldModel, policy: Policy, params: dict[str, float | int]) -> bool:
    """Low health for the retreat threshold, or the group outclasses us."""
    if on_safe_tile(w):
        return False
    group = combat_group(w, policy)
    if not group and not hostiles_in_range(w, policy):
        return False
    p = params_from(params)
    eff = effective_risk(float(p["risk"]), w.lives, int(p["lives_floor"]))
    hits = effective_retreat_hits(int(p["retreat_hits"]), eff)
    hit = max_hit_damage(group or hostiles_in_range(w, policy), w.threat)
    health = w.health
    if retreat_by_health(health, hits, hit):
        return True
    if group and policy.on_hostile != "ignore" and threat_outclasses(w, policy, p):
        return True
    return False
