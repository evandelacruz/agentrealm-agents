"""The agent's own model of the world. The server keeps no copy of it."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .item_table import DEFAULT_CARRY_CAPACITY, InventorySupply, carried_from_inventory, supplies_from_list
from .threat import ThreatTable, absorb_damaged, damage_amount, hitter, hostile_hit

log = logging.getLogger(__name__)

# block_types.traversal (migrations/00024_block_traversal.sql). Door types are
# warp: never occupied, but stepping onto one warps.
WALKABLE = {"grass", "dirt", "tile", "fire", "lava"}
DOORS = {"framed_door", "rock_entry"}
VOID = ""  # inside a terrain read but no ground there
UNKNOWN = "?"  # terrain grid symbol for a cell the read carries no block for


def terrain_cells(t: dict) -> dict:
    """Decodes a terrain read's grid (docs/API.md Tiles, B102) to {(x, y): legend entry}.

    rows[j] is y0+j and its i-th character is x0+i; each character is a legend
    key or UNKNOWN. A cell with art also carries "art" and, when authored, "facing".
    """
    x0, y0 = int(t["x0"]), int(t["y0"])
    legend = t.get("legend") or {}
    cells = {}
    for j, row in enumerate(t.get("rows") or []):
        for i, sym in enumerate(row):
            if sym != UNKNOWN:
                cells[(x0 + i, y0 + j)] = dict(legend[sym])
    for a in t.get("art") or []:
        cells[(int(a["x"]), int(a["y"]))].update({k: a[k] for k in ("art", "facing") if k in a})
    return cells

Pos = tuple[int, int]


@dataclass(frozen=True)
class ZoneFact:
    """A get_zone answer for one cell (A7)."""

    safe: bool
    brightness: float = 1.0
    strength_ceiling: int | None = None


NEIGHBOURS = [(dx, dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1) if (dx, dy) != (0, 0)]


def chebyshev(a: Pos, b: Pos) -> int:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


@dataclass
class Entity:
    kind: str  # character | npc | supply | chest
    id: int
    pos: Pos
    code: str = ""  # outfit, npc type, or supply subtype; empty for a chest
    gem_price: int | None = None  # shop supplies on entity reads (Manual §9.3)
    health: int | None = None  # bosses only on entity reads (Manual §9.3, A38)
    max_health: int | None = None

    @property
    def is_boss(self) -> bool:
        """Only a boss NPC carries ``health`` on entity reads (API Reads; GAME_NOTES.md Combat)."""
        return self.kind == "npc" and self.health is not None


@dataclass
class MapView:
    """What this character has seen of one map. Missing tiles are unknown."""

    tiles: dict[Pos, str] = field(default_factory=dict)
    # occupy_damage named by terrain reads (Manual §9.2 legend), 0 included.
    damage: dict[Pos, int] = field(default_factory=dict)
    # Signs and statues (Manual §9.2): readable wall cells from terrain reads.
    readable: dict[Pos, bool] = field(default_factory=dict)
    # Doors may carry ``locked: true`` on terrain reads (Manual §9.2).
    locked: dict[Pos, bool] = field(default_factory=dict)

    def walkable(self, p: Pos) -> bool:
        return self.tiles.get(p) in WALKABLE

    def occupy_damage(self, p: Pos) -> int | None:
        """The tile's occupy_damage, or None when no read has named it."""
        return self.damage.get(p)

    def frontier(self) -> set[Pos]:
        """Known walkable tiles that touch an unknown one."""
        out = set()
        for p, block in self.tiles.items():
            if block not in WALKABLE:
                continue
            for dx, dy in NEIGHBOURS:
                if (p[0] + dx, p[1] + dy) not in self.tiles:
                    out.add(p)
                    break
        return out


@dataclass
class WorldModel:
    character_id: int
    map_id: int | None = None
    map_level: int | None = None  # level number on interior maps (position read, Manual §5.3)
    pos: Pos | None = None
    perception: int = 1
    movement: int = 1
    movement_speed: int = 2500  # thousandths of a block per second (GetSelf)
    alive: bool = True
    placed: bool = True  # on a map: GetSelf's ``placed``; false once Died until Respawned (A5)
    asleep: bool = False  # GetSelf and a sleeping round trip carry it (GAME_NOTES Sleep)
    lives: int = 0
    gems: int | None = None  # inventory counter from snapshots (A22)
    health: int | None = None
    max_health: int | None = None
    attack_range: int | None = None  # armed weapon reach from get_self (B100)
    armed_code: str | None = None
    worn_codes: dict[str, str] = field(default_factory=dict)
    worn_slots: dict[str, str] = field(default_factory=dict)  # subtype -> slot a snapshot served it worn in, for the run (A19)
    held_supplies: list[InventorySupply] = field(default_factory=list)  # inventory held[] (A10, A20)
    chest_supplies: list[InventorySupply] = field(default_factory=list)
    # Lowered by a carry_capacity_full rejection; back to the default on respawn,
    # which brings a new 10-slot chest (Manual §11; loot.learn_loot_rejection).
    carry_capacity: int = DEFAULT_CARRY_CAPACITY
    undroppable: set[int] = field(default_factory=set)  # supply ids Drop refused not_transferable
    tick: int = 0
    maps: dict[int, MapView] = field(default_factory=dict)
    entities: list[Entity] = field(default_factory=list)
    entities_tick: int = -10**9  # tick of the last entity read
    terrain_center: Pos | None = None  # where we stood at the last terrain read
    terrain_map: int | None = None
    snapshot_version: int | None = None  # last applied observation version (Manual §7.1)
    recent_damage: list[tuple[int, int]] = field(default_factory=list)  # (tick, amount)
    attacked_tick: int | None = None  # tick of the last hostile hit on us: Attacked, or Damaged from an NPC or character (A9)
    attacker: tuple[str, int] | None = None  # (entity kind, id) a hostile Damaged named as its source, the last one (A9)
    changed_blocks: list[tuple[int, Pos]] = field(default_factory=list)  # BlockChanged cells of the last apply_events
    threat: ThreatTable = field(default_factory=ThreatTable)
    # The chest our last death dropped: (map_id, position, chest_id), from Died
    # (docs/API.md Events, B103). Cleared once it is gone: a dropped chest
    # leaves the world when its last supply is withdrawn (B116).
    death_chest: tuple[int, Pos, int] | None = None
    # Supply ids inside each ground chest within reach, from the round trip's
    # snapshot (entities.chests[].contents). A chest farther away is absent.
    chest_contents: dict[int, list[InventorySupply]] = field(default_factory=dict)
    # Zone facts from get_zone (A7): map_id -> cell -> fact. Safe tiles derive
    # from these (zone_discovery.safe_tiles).
    zones: dict[int, dict[Pos, ZoneFact]] = field(default_factory=dict)
    # Cells whose get_zone read failed, never probed again (A7).
    zone_failed: set[tuple[int, Pos]] = field(default_factory=set)
    # Town and Respawned locations used to seed safe-tile probes.
    respawn_anchors: list[tuple[int, Pos]] = field(default_factory=list)
    # Boss fight clock, read only when a round trip carries it; the published
    # docs name no such field (GAME_NOTES.md Levels and bosses, A38).
    boss_fight_end_tick: int | None = None
    # Tick of the last `level_clear_ceremony` (a boss clear; API Round Trip).
    level_clear_tick: int | None = None

    def record_respawn_anchor(self, map_id: int, pos: Pos) -> None:
        """Seeds safe-tile probes around a town or Respawned location (A7)."""
        anchor = (map_id, pos)
        if anchor not in self.respawn_anchors:
            self.respawn_anchors.append(anchor)

    @property
    def view(self) -> MapView:
        assert self.map_id is not None
        return self.maps.setdefault(self.map_id, MapView())

    # Applying reads.

    def apply_self(self, s: dict) -> None:
        self.perception = max(1, int(s.get("perception_range", 1)))
        self.movement = max(1, int(s.get("movement_range", 1)))
        if "movement_speed" in s:
            self.movement_speed = max(1, int(s["movement_speed"]))
        self.alive = bool(s.get("alive", True))
        self.placed = bool(s.get("placed", True))
        self.lives = int(s.get("lives", 0))
        if "asleep" in s:
            self.asleep = bool(s["asleep"])
        # Absent while nothing, or no weapon, is armed (B100).
        self.attack_range = _opt_int(s.get("attack_range"))

    def apply_position(self, p: dict) -> None:
        # `level` belongs to the map (Manual §5.3): a read that omits it keeps
        # the map's level, and a new map starts with none until a read names
        # it. A non-integer `level` is logged and read as omitted (A37).
        has_level = "level" in p
        level = p.get("level")
        if level is not None and (isinstance(level, bool) or not isinstance(level, int)):
            log.warning("position level must be an integer, got %r; ignored", level)
            has_level, level = False, None
        map_id = int(p["map_id"])
        if map_id != self.map_id:
            self.snapshot_version = None  # entities no longer match its base
            self.map_level = None
        self.map_id = map_id
        self.pos = (int(p["x"]), int(p["y"]))
        self.asleep = False  # a sleeping character is off the map (GAME_NOTES Sleep)
        self.placed = True
        if has_level:
            self.map_level = level

    def perception_rect(self) -> tuple[int, int, int, int]:
        """x0, y0, width, height of the perception window around us."""
        assert self.pos is not None
        r = self.perception
        return self.pos[0] - r, self.pos[1] - r, 2 * r + 1, 2 * r + 1

    def terrain_stale(self) -> bool:
        """True when a terrain read is due (PLAYABLE_AGENT_PLAN Executor: Terrain reads)."""
        if self.pos is None or self.map_id is None:
            return False
        if self.terrain_map != self.map_id or self.terrain_center is None:
            return True
        return chebyshev(self.terrain_center, self.pos) > self.perception // 2

    def apply_terrain(self, t: dict) -> None:
        """Records a terrain read of our perception window.

        Everything inside perception is visible, so an unknown cell in the
        read has no ground: it is recorded as VOID, known and not walkable.
        """
        view = self.maps.setdefault(int(t["map_id"]), MapView())
        x0, y0 = int(t["x0"]), int(t["y0"])
        for y in range(y0, y0 + int(t["height"])):
            for x in range(x0, x0 + int(t["width"])):
                view.tiles.setdefault((x, y), VOID)
        for (x, y), cell in terrain_cells(t).items():
            p = (x, y)
            view.tiles[p] = cell["block_type"]
            _set_damage(view, p, cell)
            # A full read: a cell whose legend entry lacks the flag is not readable.
            _set_readable(view, p, cell, full=True)
            _set_locked(view, p, cell, full=True)
        self.terrain_center = self.pos
        self.terrain_map = self.map_id

    def apply_entities(self, e: dict) -> None:
        self.entities = self._entities_from_payload(e)
        self.entities_tick = int(e.get("tick", self.tick))
        # A separate read replaced the state the next delta would apply to.
        self.snapshot_version = None

    def _entities_from_payload(self, e: dict) -> list[Entity]:
        out: list[Entity] = []
        for c in e.get("characters") or []:
            if int(c["id"]) != self.character_id:
                out.append(Entity("character", int(c["id"]), (int(c["x"]), int(c["y"])), c.get("outfit_code", "")))
        for n in e.get("npcs") or []:
            out.append(
                Entity(
                    "npc",
                    int(n["id"]),
                    (int(n["x"]), int(n["y"])),
                    n.get("npc_type_code", ""),
                    health=_opt_int(n.get("health")),
                    max_health=_opt_int(n.get("max_health")),
                )
            )
        for s in e.get("supplies") or []:
            price = _opt_int(s.get("gem_price"))
            out.append(
                Entity(
                    "supply",
                    int(s["id"]),
                    (int(s["x"]), int(s["y"])),
                    s.get("supply_subtype_code", ""),
                    gem_price=price,
                )
            )
        for ch in e.get("chests") or []:
            out.append(Entity("chest", int(ch["id"]), (int(ch["x"]), int(ch["y"]))))
        return out

    def _entity_key(self, ent: Entity) -> tuple[str, int]:
        return ent.kind, ent.id

    def _entity_from_entry(self, kind: str, entry: dict) -> Entity | None:
        eid = int(entry["id"])
        pos = (int(entry["x"]), int(entry["y"]))
        if kind == "character":
            if eid == self.character_id:
                return None
            return Entity("character", eid, pos, entry.get("outfit_code", ""))
        if kind == "npc":
            return Entity(
                "npc",
                eid,
                pos,
                entry.get("npc_type_code", ""),
                health=_opt_int(entry.get("health")),
                max_health=_opt_int(entry.get("max_health")),
            )
        if kind == "supply":
            return Entity(
                "supply",
                eid,
                pos,
                entry.get("supply_subtype_code", ""),
                gem_price=_opt_int(entry.get("gem_price")),
            )
        if kind == "chest":
            return Entity("chest", eid, pos)
        return None

    def _apply_entity_delta(self, patch: dict) -> None:
        """Merges an observation entities patch (Manual §7.2)."""
        by_key = {self._entity_key(e): e for e in self.entities}
        kind_map = {
            "characters": "character",
            "npcs": "npc",
            "supplies": "supply",
            "chests": "chest",
        }
        for field, kind in kind_map.items():
            part = patch.get(field)
            if not part:
                continue
            for entry in part.get("added") or []:
                ent = self._entity_from_entry(kind, entry)
                if ent is not None:
                    by_key[self._entity_key(ent)] = ent
            for entry in part.get("changed") or []:
                ent = self._entity_from_entry(kind, entry)
                if ent is not None:
                    by_key[self._entity_key(ent)] = ent
            for eid in part.get("removed") or []:
                by_key.pop((kind, int(eid)), None)
            for entry in (part.get("added") or []) + (part.get("changed") or []):
                if kind != "chest" or "contents" not in entry:
                    continue
                cid = int(entry["id"])
                self.chest_contents[cid] = supplies_from_list(entry["contents"])
            for eid in part.get("removed") or []:
                self.chest_contents.pop(int(eid), None)
        self.entities = list(by_key.values())

    def _apply_terrain_delta(self, patch: dict) -> None:
        """Updates known tiles from an observation terrain patch."""
        for cell in patch.get("changed") or []:
            map_id = int(cell["map_id"])
            p = (int(cell["x"]), int(cell["y"]))
            v = self.maps.setdefault(map_id, MapView())
            v.tiles[p] = cell.get("block_type", "")
            _set_damage(v, p, cell)
            _set_readable(v, p, cell)
            _set_locked(v, p, cell)
        for cell in patch.get("removed") or []:
            map_id = int(cell["map_id"])
            p = (int(cell["x"]), int(cell["y"]))
            v = self.maps.setdefault(map_id, MapView())
            v.tiles.pop(p, None)
            v.damage.pop(p, None)
            v.readable.pop(p, None)
            v.locked.pop(p, None)

    def _apply_snapshot_terrain(self, terrain: dict) -> None:
        for cell in terrain.get("cells") or []:
            map_id = int(cell["map_id"])
            p = (int(cell["x"]), int(cell["y"]))
            v = self.maps.setdefault(map_id, MapView())
            v.tiles[p] = cell.get("block_type", "")
            _set_damage(v, p, cell)
            _set_readable(v, p, cell)
            _set_locked(v, p, cell)

    def _chest_contents_from_entities(self, entities: dict) -> dict[int, list[InventorySupply]]:
        return {
            int(ch["id"]): supplies_from_list(ch["contents"])
            for ch in entities.get("chests") or []
            if "contents" in ch
        }

    def _refresh_death_chest(self) -> None:
        if self.death_chest is None:
            return
        map_id, at, chest_id = self.death_chest
        here = self.pos if self.map_id == map_id else None
        gone = here is not None and chebyshev(at, here) <= 1 and chest_id not in self.chest_contents
        if gone or self.chest_contents.get(chest_id) == []:
            self.death_chest = None

    def _apply_vitals(self, body: dict, complete: bool) -> None:
        """Reads health and max health from a snapshot or a delta.

        A complete snapshot is authoritative, so a field it omits (asleep)
        clears the old value. A delta replaces only the fields it carries.
        A value that is not a number reads as unknown.
        """
        for key in ("health", "max_health"):
            if complete or key in body:
                setattr(self, key, _opt_int(body.get(key)))

    def _apply_body_scalars(self, body: dict) -> None:
        """Fields a snapshot and a delta share: present means replace."""
        if "lives" in body:
            self.lives = int(body["lives"])
        if "alive" in body:
            self.alive = bool(body["alive"])
        if "asleep" in body:
            self.asleep = bool(body["asleep"])
        if "position" in body:
            pos = body["position"]
            if pos is None:
                self.forget_position()
            else:
                self.apply_position(pos)

    def note_level_clear(self, ceremony: dict | None) -> None:
        """Records a round trip's one-shot ``level_clear_ceremony`` (A38)."""
        if ceremony:
            self.level_clear_tick = self.tick

    def _apply_boss_fight_clock(self, body: dict, *, complete: bool) -> None:
        if complete or "boss_fight_end_tick" in body:
            self.boss_fight_end_tick = _opt_int(body.get("boss_fight_end_tick"))

    def in_boss_fight(self) -> bool:
        """True while the served fight clock has not expired (A38)."""
        end = self.boss_fight_end_tick
        return end is not None and end > self.tick

    def boss_fight_ticks_left(self) -> int | None:
        end = self.boss_fight_end_tick
        if end is None:
            return None
        return max(0, end - self.tick)

    def _apply_inventory(self, inv: dict | None) -> None:
        if inv is None:
            return
        if "gems" in inv:
            gems = _opt_int(inv.get("gems"))
            if gems is not None and gems >= 0:
                self.gems = gems
        self.held_supplies, self.chest_supplies, self.armed_code, self.worn_codes = carried_from_inventory(inv)
        self.worn_slots.update((code, slot) for slot, code in self.worn_codes.items())

    def _apply_snapshot_body(self, snap: dict) -> None:
        self._apply_body_scalars(snap)
        self._apply_vitals(snap, complete=True)
        self._apply_boss_fight_clock(snap, complete=True)
        if "inventory" in snap:
            self._apply_inventory(snap.get("inventory"))
        if "entities" in snap:
            entities = snap["entities"] or {}
            self.entities = self._entities_from_payload(entities)
            self.chest_contents = self._chest_contents_from_entities(entities)
            self.entities_tick = self.tick
        terrain = snap.get("terrain")
        if terrain:
            self._apply_snapshot_terrain(terrain)
        self._refresh_death_chest()

    def _apply_delta_body(self, delta: dict) -> None:
        self._apply_body_scalars(delta)
        self._apply_vitals(delta, complete=False)
        self._apply_boss_fight_clock(delta, complete=False)
        if "inventory" in delta:
            self._apply_inventory(delta.get("inventory"))
        if "entities" in delta:
            self._apply_entity_delta(delta["entities"])
            self.entities_tick = self.tick
        if "terrain" in delta:
            self._apply_terrain_delta(delta["terrain"])
        self._refresh_death_chest()

    def apply_events(self, events_by_tick: list[dict]) -> list[dict]:
        """Folds durable facts from events into the model and returns them flat.

        Attacked, Damaged, and Died on a queue happened to the queue owner and
        carry no subject_id (docs/API.md, Events), so each one is ours.
        """
        flat = []
        self.changed_blocks = []
        for group in events_by_tick or []:
            for ev in group.get("events") or []:
                flat.append(ev)
                kind = ev.get("kind")
                if hostile_hit(ev):
                    self.attacked_tick = int(ev.get("tick", group["tick"]))
                    self.attacker = hitter(ev) or self.attacker
                if kind == "Damaged":
                    amount = damage_amount(ev)
                    if amount is not None:
                        self.recent_damage.append((int(ev.get("tick", group["tick"])), amount))
                elif kind == "BlockChanged" and ev.get("map_id") in self.maps:
                    v = self.maps[ev["map_id"]]
                    p = (int(ev["x"]), int(ev["y"]))
                    v.tiles[p] = ev.get("block_type", "")
                    v.damage.pop(p, None)
                    self.changed_blocks.append((ev["map_id"], p))
                elif kind == "SupplyTaken":
                    self.entities = [x for x in self.entities if not (x.kind == "supply" and x.id == ev.get("supply_id"))]
                elif kind == "Died":
                    self.forget_position()
                    self.placed = False  # off the map until Respawned: no position read can answer (A5)
                    if ev.get("chest_id"):
                        self.death_chest = (int(ev["map_id"]), (int(ev["x"]), int(ev["y"])), int(ev["chest_id"]))
                elif kind == "Respawned":
                    self.placed, self.attacked_tick, self.attacker = True, None, None
                    self.carry_capacity = DEFAULT_CARRY_CAPACITY  # a new, empty blue chest (10), Manual §11
                    try:
                        self.record_respawn_anchor(int(ev["map_id"]), (int(ev["x"]), int(ev["y"])))
                    except (KeyError, TypeError, ValueError):
                        pass  # no location on the event: nothing to anchor probes to
        return flat

    def learn_threat(self, events: list[dict], earlier: list[Entity]) -> None:
        """Folds this round trip's Damaged events into the threat table (A6).

        Call after apply_observation, so a source first listed in the same
        response resolves to its type. A source that left view in that
        response is looked up in earlier, the entities before it. There is
        no entity list per event tick, so a source seen in neither is not
        recorded.
        """
        for ev in events:
            if ev.get("kind") == "Damaged":
                absorb_damaged(self.threat, ev, self.entities, earlier)

    def apply_observation(self, obs: dict | None) -> None:
        """Folds a tick observation into the model (Manual §7.2).

        Observations may be unchanged, a delta against the last applied
        version, or a complete snapshot when the client had no version or one
        too old. Entity patches merge by id; terrain patches update known
        tiles; a complete snapshot replaces only the parts it carries. A
        separate entity read, losing our position, or a map change clears
        snapshot_version, since the next delta's base no longer matches.
        """
        if not obs:
            return
        version = obs.get("version")
        if obs.get("unchanged"):
            if version is not None:
                self.snapshot_version = int(version)
            return
        if obs.get("complete"):
            self._apply_snapshot_body(obs.get("snapshot") or {})
        elif "delta" in obs:
            self._apply_delta_body(obs["delta"])
        if version is not None:
            self.snapshot_version = int(version)

    def forget_position(self) -> None:
        self.pos = None
        self.map_id = None
        self.snapshot_version = None

    # Queries.

    def occupied(self) -> set[Pos]:
        return {e.pos for e in self.entities if e.kind in ("character", "npc")}

    def damage_since(self, tick: int) -> int:
        return sum(a for t, a in self.recent_damage if t >= tick)

    def neighbours(self, p: Pos) -> list[Pos]:
        return [(p[0] + dx, p[1] + dy) for dx, dy in NEIGHBOURS]

    def open_neighbours(self, p: Pos, avoid: set[Pos] = frozenset()) -> list[Pos]:
        """Walkable, unoccupied tiles one step from p, minus avoid."""
        occ = self.occupied() | avoid
        return [n for n in self.neighbours(p) if self.view.walkable(n) and n not in occ]


def _set_damage(view: MapView, p: Pos, cell: dict) -> None:
    """Record a cell's occupy_damage; 0 is a value, absence forgets it."""
    dmg = cell.get("occupy_damage")
    if dmg is not None:
        view.damage[p] = int(dmg)
    else:
        view.damage.pop(p, None)


def _set_readable(view: MapView, p: Pos, cell: dict, *, full: bool = False) -> None:
    if cell.get("readable"):
        view.readable[p] = True
    elif full or "readable" in cell:
        view.readable.pop(p, None)


def _set_locked(view: MapView, p: Pos, cell: dict, *, full: bool = False) -> None:
    if cell.get("locked"):
        view.locked[p] = True
    elif full or "locked" in cell:
        view.locked.pop(p, None)


def _opt_int(v) -> int | None:
    if isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError, OverflowError):
        return None
