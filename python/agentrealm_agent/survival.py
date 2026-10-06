"""Survival params, retreat threshold and win estimate (A9, PLAYABLE_AGENT_PLAN Health and lives)."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from .threat import ThreatTable, type_key_for_entity
from .travel.knowledge import town_from_kb
from .world import Entity, Pos, WorldModel, chebyshev
from .zone_discovery import known_safe, safe_tiles

if TYPE_CHECKING:
    from .config import Policy
    from .knowledge_base import KnowledgeBase

# Stated assumptions until measured (GAME_NOTES.md Open questions, "Assumed
# until measured"). Only the win estimate reads them; A23 refines it.
HOSTILE_ATTACK_INTERVAL_TICKS = 15
OUR_ATTACK_INTERVAL_TICKS = 10
OUR_DAMAGE_PER_HIT = 1
UNKILLED_HOSTILE_HEALTH = 10
NEW_CHARACTER_HEALTH = 10
GROUP_JOIN_RADIUS = 2  # hostiles within this of the focus join the fight (PLAYABLE_AGENT_PLAN Fight)
# How long Flee keeps running from a hostile that hit us once it is out of
# range: two of its swings, so a pursuer stepping just past ``hostile_range``
# between hits does not end the flee (A9, A58 run 9).
THREAT_MEMORY_TICKS = 2 * HOSTILE_ATTACK_INTERVAL_TICKS
# A hostile's reach, assumed until measured: the API serves none (PLAN.md
# Server gaps). A hostile this close is in reach of us.
HOSTILE_REACH = 1


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


def is_hostile(w: WorldModel, policy: Policy, e: Entity) -> bool:
    """``e`` is a threat: an entity of a ``policy.hostile`` kind that is a
    character, a boss, the one that hit us last, or an NPC whose type has
    shown itself hostile (``WorldModel.hostile_types``, ``threat``).

    The API does not say which NPC types are hostile, so an NPC of a type
    never seen attacking or dying is not one: townsfolk, shopkeepers and
    helpers never start Flee, Retreat or a Fight (A23 survive-a-fight run 2).
    """
    if e.kind not in policy.hostile:
        return False
    if e.kind != "npc" or e.is_boss or is_attacker(w, e):
        return True
    key = type_key_for_entity(e)
    return key is not None and (key in w.hostile_types or w.threat.measured(key))


def hostiles_in_range(w: WorldModel, policy: Policy) -> list[Entity]:
    if w.pos is None:
        return []
    here = w.pos
    return [
        e
        for e in w.entities
        if is_hostile(w, policy, e) and chebyshev(e.pos, here) <= policy.hostile_range
    ]


def recently_attacked(w: WorldModel) -> bool:
    """A hostile hit us within ``THREAT_MEMORY_TICKS``: an ``Attacked``, or a
    ``Damaged`` from an NPC or character. Traps and hazard ground do not count."""
    return w.attacked_tick is not None and w.tick - w.attacked_tick <= THREAT_MEMORY_TICKS


def flee_from(w: WorldModel, policy: Policy) -> list[Entity]:
    """The hostiles Flee runs from: those in range, or, with ``on_hostile = "flee"``,
    the one that hit us recently when it is in view (``attacker``).

    A pursuer that steps just past ``hostile_range`` between its hits is
    still chasing us (A58 run 9); a bystander in view is not. ``fight`` keeps
    its own rule: it flees only a target in range it cannot beat or reach.
    """
    in_range = hostiles_in_range(w, policy)
    if in_range or policy.on_hostile != "flee" or not recently_attacked(w):
        return in_range
    return [e for e in w.entities if is_hostile(w, policy, e) and is_attacker(w, e)]


def approaching(w: WorldModel, e: Entity) -> bool:
    """``e``'s latest move, within ``THREAT_MEMORY_TICKS``, brought it closer to us."""
    moved = w.entity_moves.get((e.kind, e.id))
    if moved is None or w.pos is None:
        return False
    before, tick = moved
    return w.tick - tick <= THREAT_MEMORY_TICKS and chebyshev(e.pos, w.pos) < chebyshev(before, w.pos)


def threatening(w: WorldModel, group: list[Entity]) -> bool:
    """A hostile in ``group`` is coming for us: it hit us recently, stands in
    reach (``HOSTILE_REACH``), or is approaching. A hostile that just stands
    nearby is avoided, not run from (A9).

    A boss always is: a boss fight is one Boss chose to start, so a fight we
    would lose there means retreat out, not stepping in until it is in reach
    (A38, review on #131)."""
    if w.pos is None:
        return False
    if any(e.is_boss for e in group):
        return True
    if recently_attacked(w) and any(is_attacker(w, e) for e in group):
        return True
    return any(chebyshev(e.pos, w.pos) <= HOSTILE_REACH or approaching(w, e) for e in group)


def is_attacker(w: WorldModel, e: Entity) -> bool:
    """``e`` is the hostile the last hostile hit named as its source."""
    return w.attacker == (e.kind, e.id)


def combat_group(w: WorldModel, policy: Policy, also: Entity | None = None) -> list[Entity]:
    """Hostiles in range, and ``also`` when given, plus any within ``GROUP_JOIN_RADIUS`` of the nearest."""
    in_range = hostiles_in_range(w, policy)
    if also is not None and also not in in_range:
        in_range.append(also)
    if not in_range or w.pos is None:
        return []
    focus = min(in_range, key=lambda e: (chebyshev(e.pos, w.pos), e.id))
    group = [focus]
    for e in w.entities:
        if not is_hostile(w, policy, e) or e.id == focus.id:
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
    return known_safe(w, w.map_id, w.pos)


def retreat_goal(w: WorldModel, knowledge: KnowledgeBase | None) -> Pos | None:
    """Where Retreat heads: the nearest known safe cell on this map, with the
    town cell the world read gave as one more candidate, or None (A9).

    So a character that has read no safe zone yet still runs for town
    instead of standing its ground (A16 Walk run 4).
    """
    if w.map_id is None or w.pos is None:
        return None
    goals = set(safe_tiles(w, w.map_id))
    town = town_from_kb(knowledge)
    if town is not None and town[0] == w.map_id:
        goals.add(town[1])
    if not goals:
        return None
    here = w.pos
    if here in goals:
        return here
    return min(goals, key=lambda p: (chebyshev(p, here), p))


# The corridor search each retreat danger profile keeps (``pathing.nav_search``):
# a corridor priced on one profile is never reused on another, since a finished
# corridor is replanned only when a tile on it gets dearer (A13).
RETREAT_NAV = "safe:retreat"
LOSING_NAV = "safe:losing"


def pursuer_peaks(w: WorldModel, policy: Policy, everyone: bool = False) -> dict[tuple[str, int], int]:
    """Danger peak 0 for the hostiles a retreat runs from: the fight's group and
    whoever hit us last, or every hostile in view when ``everyone`` (A9).

    So a retreat path takes the shortest way to safety instead of detouring
    round a chaser that follows anyway (A23 survive-a-fight run 1), and still
    keeps clear of hostiles it has not met. Their cells stay occupied, so it
    never runs through one.
    """
    if everyone:
        chasing = [e for e in w.entities if is_hostile(w, policy, e)]
    else:
        chasing = combat_group(w, policy) + [e for e in w.entities if is_attacker(w, e)]
    return {(e.kind, e.id): 0 for e in chasing}


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


def would_lose(
    w: WorldModel, policy: Policy, params: dict[str, float | int], also: Entity | None = None
) -> bool:
    """True when the win estimate is below the effective fight margin.

    **Fight** (A23) and **Flee** gate on this; **Retreat** also fires when
    the group outclasses us. ``also`` counts one more hostile as in range,
    for a fight Gather would start on one further off (A63 run 3).
    """
    group = combat_group(w, policy, also)
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
    """The next effective ``retreat_hits`` hits from the hostiles in range could
    kill, or (``on_hostile = "fight"``) we would lose to a group that is
    ``threatening`` us.

    A hit's size comes from what is attacking (the threat table), so with no
    hostile in range there is nothing to retreat from. A fight we would lose
    against hostiles that are not coming for us is avoided, not retreated
    from: Retreat started three times at full health from that (A23
    survive-a-fight run 2). ``on_hostile = "ignore"`` never retreats.
    """
    if policy.on_hostile == "ignore" or on_safe_tile(w):
        return False
    group = combat_group(w, policy)
    if not group:
        return False
    if policy.on_hostile == "fight" and would_lose(w, policy, params) and threatening(w, group):
        return True
    return at_health_floor(w, params, group)


def at_health_floor(w: WorldModel, params: dict[str, float | int], group: list[Entity]) -> bool:
    """The next effective ``retreat_hits`` hits from ``group`` could kill: the health floor (A9).

    Retreat runs at or below it, and Flee never picks a fight it would lose there.
    """
    eff = effective_risk(float(params["risk"]), w.lives, int(params["lives_floor"]))
    hits = effective_retreat_hits(int(params["retreat_hits"]), eff)
    return retreat_by_health(w.health, hits, max_hit_damage(group, w.threat))
