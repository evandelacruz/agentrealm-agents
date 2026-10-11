"""Survival params, retreat threshold and win estimate (A9, PLAYABLE_AGENT_PLAN Health and lives)."""

from __future__ import annotations

import math
from collections.abc import Collection
from typing import TYPE_CHECKING

from .supplies import weapon_damage
from .threat import ThreatTable, TypeKey, type_key_for_entity
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
UNKILLED_HOSTILE_HEALTH = 10
# Our swing, by the published roll (GAME_NOTES.md Combat): it hits when d20
# plus attack power is at least 10 plus the target's defense, which a
# hostile does not have; a 1 always misses and a 20 always hits. A hit deals
# 1 up to attack power plus weapon damage. The API serves no attack power
# (PLAN.md Server gaps), so ours is the world's base: every Olympuff
# character has 2 (A81), a hit on 8 or better, 65% of swings.
BASE_ATTACK_POWER = 2
COMBAT_DIE = 20
COMBAT_HIT_TARGET = 10
# Weapon damage is the Supplies reference's (``supplies.weapon_damage``, A54).
# An armed item it lists as no weapon (food, a tool, nothing) swings as the
# starting knife.
STARTING_WEAPON = "pocket_knife"
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
    return e.kind != "npc" or known_hostile(w, e)


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


def known_hostile(w: WorldModel, e: Entity) -> bool:
    """``e`` has shown it is hostile: a boss, the last thing that hit us, or of a
    type that has swung at us, hit us or died in view, this run or an earlier
    one (``WorldModel.hostile_types``, ``threat``). Helpers never do."""
    if e.is_boss or is_attacker(w, e):
        return True
    key = type_key_for_entity(e)
    return key is not None and (key in w.hostile_types or w.threat.measured(key))


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


def on_safe_tile(w: WorldModel) -> bool:
    if w.map_id is None or w.pos is None:
        return False
    return known_safe(w, w.map_id, w.pos)


def retreat_goal(w: WorldModel, knowledge: KnowledgeBase | None) -> Pos | None:
    """Where Retreat heads with no path checked: the nearest known safe cell
    on this map, with the town cell the world read gave as one more
    candidate, or None (A9). The walk itself picks the nearest one a path
    reaches (``safe_goals``, ``pathing.reachable_safe_goal``).

    So a character that has read no safe zone yet still runs for town
    instead of standing its ground (A16 Walk run 4).
    """
    goals = safe_goals(w, knowledge)
    return goals[0] if goals else None


def safe_goals(w: WorldModel, knowledge: KnowledgeBase | None) -> list[Pos]:
    """Every cell Retreat may head for, nearest first (ties to the smaller
    cell): the known safe cells on this map, and the town cell when it is on
    this map. The cell we stand on comes first when it is one."""
    if w.map_id is None or w.pos is None:
        return []
    goals = set(safe_tiles(w, w.map_id))
    town = town_cell(w, knowledge)
    if town is not None:
        goals.add(town)
    here = w.pos
    return sorted(goals, key=lambda p: (chebyshev(p, here), p))


def town_cell(w: WorldModel, knowledge: KnowledgeBase | None) -> Pos | None:
    """The town cell the world read gave, when it is on this map."""
    town = town_from_kb(knowledge)
    return town[1] if town is not None and town[0] == w.map_id else None


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


def hostile_reach(
    w: WorldModel, policy: Policy, skip: Collection[tuple[str, int]] = (), only: Entity | None = None
) -> set[Pos]:
    """Cells within ``policy.hostile_range`` of a known hostile (``is_hostile``),
    leaving out those whose (kind, id) is in ``skip``; of ``only`` alone when given.

    Standing there starts Retreat, Flee or Fight, so a Heal or Retreat walk
    prices them as ``costly`` and a held walk queue that comes to cross one is
    replanned (A63 run 4: Heal walked a 10-Step queue into a known pack).
    """
    reach = policy.hostile_range
    out: set[Pos] = set()
    for e in w.entities if only is None else [only]:
        if (e.kind, e.id) in skip or not is_hostile(w, policy, e):
            continue
        hx, hy = e.pos
        out.update((hx + dx, hy + dy) for dx in range(-reach, reach + 1) for dy in range(-reach, reach + 1))
    return out


def hostiles_reaching(
    w: WorldModel, policy: Policy, cells: Collection[Pos], skip: Collection[tuple[str, int]] = ()
) -> set[tuple[str, int]]:
    """The known hostiles (kind, id), not in ``skip``, with one of ``cells`` in their ``hostile_reach``.

    Hostiles, not cells: one that moves while in reach of a path shifts its
    reach but is no new threat to it, so a walk that could not go round it is
    not planned or sent again for every step it takes (A63 run 4).
    """
    cells = set(cells)
    if not cells:
        return set()
    return {(e.kind, e.id) for e in w.entities if not cells.isdisjoint(hostile_reach(w, policy, skip, only=e))}


def hostile_swing_damage(damage: int, threat: ThreatTable | None = None, key: TypeKey | None = None) -> float:
    """Expected damage of one hostile swing at us. A hostile swings with its
    damage number as attack power and no weapon damage (GAME_NOTES.md
    Combat), so it hits on the same roll ours does; once ``threat`` has
    counted the type's hits and misses, on its measured rate, with that
    roll as the prior (``ThreatTable.hit_rate``). ``damage`` is the threat
    table's largest hit, only a floor on that number, so each landed hit is
    priced at it, not at the mean below it. Our armor is not counted: the
    agent does not know its defense."""
    chance = hit_chance(attack_power=damage)
    if threat is not None:
        chance = threat.hit_rate(key, chance)
    return chance * max(1, damage)


def _hostile_damage(threat: ThreatTable, key: TypeKey | None) -> float:
    """A type's expected swing (``hostile_swing_damage``): at its largest
    measured hit, or, never measured, at the world's base attack power
    (``UNMEASURED_DEFAULT``, the published rules; A85)."""
    return hostile_swing_damage(threat.damage_per_hit(key), threat, key)


def ticks_to_kill_us(health: int, group: list[Entity], threat: ThreatTable) -> float:
    if health <= 0 or not group:
        return float("inf")
    dps = sum(_hostile_damage(threat, type_key_for_entity(e)) for e in group) / HOSTILE_ATTACK_INTERVAL_TICKS
    if dps <= 0:
        return float("inf")
    return health / dps


def hit_chance(attack_power: int = BASE_ATTACK_POWER, defense: int = 0) -> float:
    """The share of d20 faces that hit a target with ``defense``: 0.65 at
    attack power 2 against a hostile, never below 1 face nor above 19."""
    lowest = COMBAT_HIT_TARGET + defense - attack_power  # the lowest face that hits
    faces = COMBAT_DIE - max(lowest, 2) + 1
    return min(max(faces, 1), COMBAT_DIE - 1) / COMBAT_DIE


def swing_damage(weapon: str | None) -> float:
    """Expected damage of one swing of ``weapon`` at a hostile: the hit
    chance times the mean of 1 up to attack power plus weapon damage (the
    pocket knife: 0.65 × 2.5)."""
    damage = weapon_damage(weapon)
    if damage is None:
        damage = weapon_damage(STARTING_WEAPON) or 0
    return hit_chance() * (1 + max(1, BASE_ATTACK_POWER + damage)) / 2


def ticks_to_kill_them(group: list[Entity], weapon: str | None = None) -> float:
    if not group:
        return float("inf")
    return len(group) * UNKILLED_HOSTILE_HEALTH / (swing_damage(weapon) / OUR_ATTACK_INTERVAL_TICKS)


def win_ratio(health: int | None, group: list[Entity], threat: ThreatTable, weapon: str | None = None) -> float:
    """Ticks for them to kill us, over ticks for us to kill ``group`` with
    ``weapon`` armed. Higher is better for us."""
    if health is None or not group:
        return float("inf")
    return ticks_to_kill_us(health, group, threat) / ticks_to_kill_them(group, weapon)


def estimate_health(w: WorldModel) -> int:
    """The health the win estimate counts: live health, else max, else a new character's."""
    health = w.health if w.health is not None else w.max_health
    return NEW_CHARACTER_HEALTH if health is None else health


def should_retreat(w: WorldModel, policy: Policy, params: dict[str, float | int], fight: bool) -> bool:
    """The next effective ``retreat_hits`` hits from the hostiles in range could
    kill, or (``on_hostile = "fight"``) we would lose to a group that is
    ``threatening`` us: ``fight`` is the engagement's fight-or-flee decision
    (``engagement.fights``), the one Fight and Flee read too.

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
    if policy.on_hostile == "fight" and not fight and threatening(w, group):
        return True
    return at_health_floor(w, params, group)


def at_health_floor(w: WorldModel, params: dict[str, float | int], group: list[Entity]) -> bool:
    """The next effective ``retreat_hits`` hits from ``group`` could kill: the health floor (A9).

    Retreat runs at or below it, and Flee never picks a fight it would lose there.
    """
    floor = health_floor(w, params, group)
    return w.health is not None and floor > 0 and w.health <= floor


def health_floor(w: WorldModel, params: dict[str, float | int], group: list[Entity]) -> int:
    """Retreat's floor against ``group``: effective ``retreat_hits`` times the
    hardest hit among them (threat table). Detour keeps above it too (A82)."""
    eff = effective_risk(float(params["risk"]), w.lives, int(params["lives_floor"]))
    return effective_retreat_hits(int(params["retreat_hits"]), eff) * max_hit_damage(group, w.threat)
