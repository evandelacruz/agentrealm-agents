"""The moment: one snapshot of the character and what it perceives, per decision.

Scalars are copied at the decision. ``world`` is the live world model, read
only, for what is too big to copy (the map). Facts derived from the moment
are methods here, so every engine and skill reads the same ones.
"""

from __future__ import annotations

from dataclasses import dataclass

from agentrealm_adk import supplies
from agentrealm_adk.survival import known_hostile
from agentrealm_adk.world import DOORS, Entity, Pos, WorldModel, chebyshev

# An NPC is a threat by how it behaves (Manual §11 Hostiles): a boss or one
# that has hit us is; one that moves may be a chaser or a patroller; one
# that stands still is most likely a helper (they stay put, §11 Speech).


@dataclass(frozen=True)
class Carried:
    id: int
    code: str
    where: str  # armed | worn | held


@dataclass(frozen=True)
class State:
    world: WorldModel
    tick: int
    tick_hz: int
    map_id: int
    level: int | None  # the current map's level number; 0 outside every level; None while unread
    pos: Pos
    health: int
    max_health: int
    lives: int
    gems: int
    alive: bool
    carried: tuple[Carried, ...]
    levels_cleared: tuple[int, ...]
    level_count: int | None
    attack_ready: bool
    move_ready_in: int  # ticks until the next step may land
    speech_ready: bool = True  # one Say or Broadcast a second (Manual §11 Speech)

    @classmethod
    def of(cls, w: WorldModel, tick_hz: int, last_attack_tick: int | None, attack_ticks: int,
           last_step_tick: int | None, step_ticks: int, last_speech_tick: int | None = None) -> State:
        carried = [Carried(s.id, s.code, "held") for s in w.held_supplies]
        if w.armed_code and w.armed_id is not None:
            carried = [c for c in carried if c.id != w.armed_id] + [Carried(w.armed_id, w.armed_code, "armed")]
        for slot, code in w.worn_codes.items():
            carried.append(Carried(w.worn_ids.get(slot, 0), code, "worn"))
        attack_ready = last_attack_tick is None or w.tick - last_attack_tick >= attack_ticks
        move_in = 0 if last_step_tick is None else max(0, step_ticks - (w.tick - last_step_tick))
        return cls(
            world=w, tick=w.tick, tick_hz=tick_hz, map_id=w.map_id or 0, level=w.map_level, pos=w.pos or (0, 0),
            health=w.health if w.health is not None else 0, max_health=w.max_health or 10, lives=w.lives,
            gems=w.gems or 0, alive=w.alive and w.placed and w.pos is not None, carried=tuple(carried),
            levels_cleared=tuple(sorted(w.levels_cleared)), level_count=w.level_count,
            attack_ready=attack_ready, move_ready_in=move_in,
            speech_ready=last_speech_tick is None or w.tick - last_speech_tick > tick_hz,
        )

    # Self.

    @property
    def armed(self) -> str | None:
        return next((c.code for c in self.carried if c.where == "armed"), None)

    @property
    def worn(self) -> list[str]:
        return [c.code for c in self.carried if c.where == "worn"]

    def has(self, code: str) -> bool:
        return any(c.code == code for c in self.carried)

    def held(self, code: str) -> Carried | None:
        return next((c for c in self.carried if c.code == code and c.where == "held"), None)

    @property
    def health_share(self) -> float:
        return max(0.0, min(1.0, self.health / max(1, self.max_health)))

    @property
    def in_level(self) -> bool:
        return bool(self.level)

    @property
    def on_safe_ground(self) -> bool:
        return self.pos in self.world.view.safe

    # What is around.

    def npcs(self) -> list[Entity]:
        return [e for e in self.world.entities if e.kind == "npc"]

    def supplies(self) -> list[Entity]:
        return [e for e in self.world.entities if e.kind == "supply"]

    def chests(self) -> list[Entity]:
        return [e for e in self.world.entities if e.kind == "chest"]

    def is_threat(self, e: Entity) -> bool:
        """A boss, or of a type that has swung at us, hit us or died in view, in any run: the ADK's one hostility
        test (``survival.known_hostile``). Moving is no sign: townsfolk walk about town (Manual §11 Hostiles)."""
        if e.kind != "npc":
            return False
        return known_hostile(self.world, e)

    def fights(self, e: Entity) -> bool:
        """Of a type seen to fight: it has swung at us, hit us or died in view (``agentrealm_adk.survival``)."""
        return e.kind == "npc" and known_hostile(self.world, e)

    def threats(self) -> list[Entity]:
        return [e for e in self.npcs() if self.is_threat(e)]

    def known_threats(self) -> list[Entity]:
        """Threats on this map in view, or seen and still remembered where they were (``WorldModel.sightings``)."""
        seen = [sg.entity for sg in self.world.sightings.values()
                if sg.map_id == self.map_id and sg.entity.kind == "npc" and self.is_threat(sg.entity)]
        in_view = {e.id for e in self.threats()}
        return self.threats() + [e for e in seen if e.id not in in_view]

    def ground_held(self, cell: Pos) -> bool:
        """A known threat would get to ``cell`` before we do: it holds that ground (Manual §11 Hostiles:
        it goes for the nearest character, at about our pace)."""
        ours = chebyshev(self.pos, cell)
        return any(chebyshev(e.pos, cell) <= ours for e in self.known_threats())

    def boss(self) -> Entity | None:
        return next((e for e in self.npcs() if e.is_boss), None)

    def doors(self) -> list[Pos]:
        return sorted(p for p, t in self.world.view.tiles.items() if t in DOORS)

    def signs(self) -> list[Pos]:
        return sorted(p for p, readable in self.world.view.readable.items() if readable)

    def near(self, p: Pos, reach: int = 1) -> bool:
        return chebyshev(self.pos, p) <= reach

    def free_gems(self) -> list[Entity]:
        return [e for e in self.supplies() if not e.gem_price and _is_gem(e.code)]


def _is_gem(code: str) -> bool:
    row = supplies.row(code)
    return row is not None and row.supply_class == "gem"
