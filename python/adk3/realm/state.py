"""The State for Agent Realm: one snapshot per decision, and the facts derived from it.

Scalars are copied at the decision; ``w`` is the live world model, read only,
for what is too big to copy (the map). Derived facts are plain functions of
State, so every layer reads the same ones.
"""

from __future__ import annotations

from dataclasses import dataclass

from agentrealm_adk.survival import hit_damage, known_hostile, swing_damage, worn_defense
from agentrealm_adk.threat import type_key_for_entity
from agentrealm_adk.world import DOORS, Entity, Pos, WorldModel, chebyshev

UNKNOWN_NPC_PRIOR = 0.3  # how much an NPC never seen to fight counts as a threat (a weight, not a rule)


@dataclass(frozen=True)
class State:
    w: WorldModel
    tick: int
    map_id: int
    level: int | None  # 0 outside every level, None while unread
    pos: Pos
    health: int
    max_health: int
    lives: int
    gems: int
    alive: bool
    armed: str | None
    worn: tuple[str, ...]
    held: tuple[tuple[int, str], ...]  # (supply id, code), the armed one included
    levels_cleared: tuple[int, ...]
    level_count: int | None
    entities: tuple[Entity, ...]
    running: tuple | None  # key of the action still under way, if any
    tick_hz: int

    @classmethod
    def of(cls, w: WorldModel, running: tuple | None, tick_hz: int) -> State:
        held = tuple((s.id, s.code) for s in w.held_supplies)
        if w.armed_id is not None and w.armed_code and all(i != w.armed_id for i, _ in held):
            held += ((w.armed_id, w.armed_code),)
        return cls(
            w=w, tick=w.tick, map_id=w.map_id or 0, level=w.map_level, pos=w.pos or (0, 0),
            health=w.health if w.health is not None else 1, max_health=w.max_health or 1,
            lives=w.lives, gems=w.gems or 0, alive=w.alive and w.placed, armed=w.armed_code,
            worn=tuple(sorted(w.worn_codes.values())), held=held,
            levels_cleared=tuple(sorted(w.levels_cleared)), level_count=w.level_count,
            entities=tuple(w.entities), running=running, tick_hz=tick_hz,
        )

    @property
    def health_frac(self) -> float:
        return max(0.0, min(1.0, self.health / max(1, self.max_health)))

    def codes(self) -> set[str]:
        """Every supply code carried: held, armed and worn."""
        return {c for _, c in self.held} | set(self.worn) | ({self.armed} if self.armed else set())

    def npcs(self) -> list[Entity]:
        return [e for e in self.entities if e.kind == "npc"]

    def supplies(self) -> list[Entity]:
        return [e for e in self.entities if e.kind == "supply"]

    def doors(self) -> list[Pos]:
        return sorted(p for p, t in self.w.view.tiles.items() if t in DOORS)

    def signs(self) -> list[Pos]:
        return sorted(p for p, r in self.w.view.readable.items() if r)


def threat_of(s: State, e: Entity) -> float:
    """How much ``e`` counts as a threat, 0 to 1: a known hostile 1, any other NPC its prior."""
    if e.kind != "npc":
        return 0.0
    return 1.0 if known_hostile(s.w, e) else UNKNOWN_NPC_PRIOR


def hit_on_us(s: State, e: Entity) -> float:
    """What one landed hit from ``e`` takes off us, through the armor we wear."""
    return float(hit_damage(s.w.threat, type_key_for_entity(e), worn_defense(s.w)))


def danger_at(s: State, cell: Pos) -> float:
    """The share of our health the threats in view could take standing on ``cell`` for a few seconds.

    A threat counts once it is within a step and a swing of ``cell``; nearer
    counts no more, farther counts nothing. Several threats add up.
    """
    total = 0.0
    for e in s.npcs():
        t = threat_of(s, e)
        if t and chebyshev(e.pos, cell) <= 2:
            total += t * hit_on_us(s, e) * 3  # about three swings while we decide again
    return total / max(1, s.health)


def our_hit(s: State) -> float:
    """Average damage one of our swings deals."""
    return swing_damage(s.armed)
