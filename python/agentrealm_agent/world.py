"""The agent's own model of the world. The server keeps no copy of it."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .item_table import DEFAULT_CARRY_CAPACITY, InventorySupply, carried_from_inventory, supplies_from_list
from .supplies import worn_armor_defense
from .threat import (
    ThreatTable,
    TypeKey,
    absorb_damaged,
    count_swings,
    damage_amount,
    hitter,
    hostile_hit,
    hostile_type_from_event,
)

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


# An NPC that stands on one cell this long in view keeps a post there: the
# same stillness Greet takes for a helper (``investigation.HELPER_STILL_TICKS``).
POST_STILL_TICKS = 50
# A hostile out of view that keeps no post is remembered where it was last
# seen for this long (60 s at 10 ticks/s), on any map.
SIGHTING_TICKS = 600
# A remembered post fades: hostiles roam and respawn (A85).
# Its ``Sighting.strength`` is 1 while the guard is in view and halves every
# ``POST_HALF_LIFE_TICKS`` of world time out of view (5 min at 10 ticks/s),
# times the spells it was seen on its post, up to ``POST_MAX_SPELLS``: a post
# seen again and again stays strong for longer. A spell counts when the guard
# is back on its post after ``SIGHTING_TICKS`` out of view, so a guard at the
# edge of sight flickering in and out adds none. A post in sight with nobody
# on it halves every ``EMPTY_POST_HALF_LIFE_TICKS`` looked at (1 s), at most
# once a look. It holds ground while at least ``POST_HOLD_STRENGTH``, only
# prices it below that, and is forgotten below ``POST_FORGET_STRENGTH``.
POST_HALF_LIFE_TICKS = 3000
POST_MAX_SPELLS = 4
EMPTY_POST_HALF_LIFE_TICKS = 10
POST_HOLD_STRENGTH = 0.5
POST_FORGET_STRENGTH = 0.1


@dataclass
class Sighting:
    """An NPC or character seen this run, kept after it leaves view (free-play run 5).

    ``home`` is its post, the cell an NPC stood on for ``POST_STILL_TICKS``
    (``post`` True), else the cell it was first seen on, which holds no
    ground: a roamer keeps to no cell. ``reach`` is the furthest from its
    post it has hit us from or come into a fight with us: a guard that leaves
    its post for us shows how far it guards. Whether it is a threat is asked at use
    (``survival.is_hostile``), so a type found hostile later counts.

    A post fades once its guard is out of view (``strength``, as of tick
    ``noted``; ``fade_post``). ``spells`` counts the times its guard was
    seen back on its post after a while away.
    """

    entity: Entity  # as last seen
    map_id: int | None
    tick: int  # last seen
    home: Pos
    post: bool = False
    reach: int = 0
    strength: float = 1.0
    noted: int = 0  # the tick ``strength`` is as of
    spells: int = 1
    in_view: bool = True

    def fade_post(self, tick: int, empty: bool) -> None:
        """Bring ``strength`` up to ``tick`` out of view: halved every
        ``POST_HALF_LIFE_TICKS`` times ``spells`` (up to ``POST_MAX_SPELLS``),
        and when ``empty`` (its post in sight with nobody on it) also every
        ``EMPTY_POST_HALF_LIFE_TICKS``, at most once for this look."""
        dt = max(0, tick - self.noted)
        half_life = POST_HALF_LIFE_TICKS * min(max(1, self.spells), POST_MAX_SPELLS)
        self.strength *= 0.5 ** (dt / half_life)
        if empty:
            self.strength *= 0.5 ** (min(dt, EMPTY_POST_HALF_LIFE_TICKS) / EMPTY_POST_HALF_LIFE_TICKS)
        self.noted = tick


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


class Tiles(dict):
    """A map's tiles: a plain dict that keeps its own frontier up to date.

    A big map's frontier is a pass over every known tile, and one decision
    asks for it several times, so it is kept, and only the cells round a
    changed one are looked at again (A23 Run 2). So change it only through
    the dict methods below: anything that goes round them (``dict.__setitem__``
    called directly) leaves the frontier stale.
    """

    # The frontier as last found, and the cells changed since. Either None:
    # find it afresh.
    _frontier: set[Pos] | None = None
    _changes: set[Pos] | None = None
    MAX_CHANGES = 4096  # more than this and a fresh pass is as quick

    def frontier(self) -> set[Pos]:
        """Known walkable tiles that touch an unknown one."""
        found, changes = self._frontier, self._changes
        if found is None or changes is None:
            found = {p for p in self if self._on_frontier(p)}
        else:
            for c in changes:
                for p in [c] + [(c[0] + dx, c[1] + dy) for dx, dy in NEIGHBOURS]:
                    if self._on_frontier(p):
                        found.add(p)
                    else:
                        found.discard(p)
        self._frontier, self._changes = found, set()
        return set(found)

    def _on_frontier(self, p: Pos) -> bool:
        if self.get(p) not in WALKABLE:
            return False
        x, y = p
        for dx, dy in NEIGHBOURS:
            if (x + dx, y + dy) not in self:
                return True
        return False

    def _changed(self, keys) -> None:
        changes = self._changes
        if changes is None:
            return
        changes.update(keys)
        if len(changes) > self.MAX_CHANGES:
            self._changes = None

    def __setitem__(self, key, value) -> None:
        super().__setitem__(key, value)
        self._changed((key,))

    def __delitem__(self, key) -> None:
        super().__delitem__(key)
        self._changed((key,))

    def __ior__(self, other):
        self._changed(dict(other))
        return super().__ior__(other)

    def update(self, *args, **kwargs) -> None:
        new = dict(*args, **kwargs)
        super().update(new)
        self._changed(new)

    def setdefault(self, key, default=None):
        self._changed((key,))
        return super().setdefault(key, default)

    def pop(self, key, *default):
        self._changed((key,))
        return super().pop(key, *default)

    def popitem(self):
        key, value = super().popitem()
        self._changed((key,))
        return key, value

    def clear(self) -> None:
        super().clear()
        self._changes = None


@dataclass
class MapView:
    """What this character has seen of one map. Missing tiles are unknown."""

    # Always a ``Tiles`` (a dict passed in is wrapped), changed only through
    # its own dict methods: one that bypasses them, like
    # ``dict.__setitem__(tiles, …)``, or a plain dict assigned later, leaves
    # the kept frontier stale.
    tiles: Tiles = field(default_factory=Tiles)
    # occupy_damage named by terrain reads (Manual §9.2 legend), 0 included.
    damage: dict[Pos, int] = field(default_factory=dict)
    # Signs and statues (Manual §9.2): readable wall cells from terrain reads.
    readable: dict[Pos, bool] = field(default_factory=dict)
    # Doors may carry ``locked: true`` on terrain reads (Manual §9.2).
    locked: dict[Pos, bool] = field(default_factory=dict)
    # Cells a terrain read marked ``safe: true``: ground in any safe zone, town
    # or a respawn patch alike (Manual §9.2 legend, B127). A full read leaves
    # the flag off elsewhere.
    safe: set[Pos] = field(default_factory=set)

    def __post_init__(self) -> None:
        if not isinstance(self.tiles, Tiles):
            self.tiles = Tiles(self.tiles)

    def walkable(self, p: Pos) -> bool:
        return self.tiles.get(p) in WALKABLE

    def occupy_damage(self, p: Pos) -> int | None:
        """The tile's occupy_damage, or None when no read has named it."""
        return self.damage.get(p)

    def frontier(self) -> set[Pos]:
        """Known walkable tiles that touch an unknown one (``Tiles.frontier``)."""
        return self.tiles.frontier()


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
    # Woke since the last position read: placed, whatever a self read says,
    # since a wake does not refresh GetSelf's ``placed`` (GAME_NOTES Sleep).
    woke: bool = False
    asleep: bool = False  # GetSelf and a sleeping round trip carry it (GAME_NOTES Sleep)
    lives: int = 0
    levels_cleared: list[int] = field(default_factory=list)  # level numbers cleared, from snapshots (A62)
    level_count: int | None = None  # levels the world authored, from GetWorld (A62)
    gems: int | None = None  # inventory counter from snapshots (A22)
    health: int | None = None
    max_health: int | None = None
    attack_range: int | None = None  # armed weapon reach from get_self (B100)
    armed_code: str | None = None
    armed_id: int | None = None  # supply id in the armed slot, when the snapshot names it (A76)
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
    entities_tick: int = -10**9  # tick of the last entity read, or of a tick delta that carried entities
    # Tick and (map, cell) of the last real entity read. A delta carries only
    # what changed near us, so a walk into new ground needs real reads (A16 Walk run 4).
    entities_read_tick: int = -10**9
    entities_read_at: tuple[int | None, Pos | None] | None = None
    terrain_center: Pos | None = None  # where we stood at the last terrain read
    terrain_map: int | None = None
    snapshot_version: int | None = None  # last applied observation version (Manual §7.1)
    recent_damage: list[tuple[int, int]] = field(default_factory=list)  # (tick, amount)
    attacked_tick: int | None = None  # tick of the last hostile hit on us: Attacked, or Damaged from an NPC or character (A9)
    attacker: tuple[str, int] | None = None  # (entity kind, id) the last hostile hit named as its source (A9)
    attacker_tick: int | None = None  # tick of that hit; a later hit naming no one clears both
    changed_blocks: list[tuple[int, Pos]] = field(default_factory=list)  # BlockChanged cells of the last apply_events
    threat: ThreatTable = field(default_factory=ThreatTable)
    # NPC id -> (cell, tick it was first seen there): how long each NPC in
    # view has stood still. Helpers stay put (GAME_NOTES NPCs); Greet (A65).
    npc_still: dict[int, tuple[Pos, int]] = field(default_factory=dict)
    # NPC types that have shown they are hostile: one swung at or hit us, or
    # one died in view (``NPCDied`` names only hostiles), this run or an
    # earlier one (``hostile_memory``). Townsfolk and helpers never land here,
    # so Flee and Retreat never answer them (A9, A23).
    hostile_types: set[TypeKey] = field(default_factory=set)
    # (kind, id) -> where each NPC and character was seen, its post and the
    # reach it hit us from, kept out of view (``Sighting``, free-play run 5);
    # hostile NPCs' sightings are kept for later runs (``hostile_memory``).
    sightings: dict[tuple[str, int], Sighting] = field(default_factory=dict)
    # Where each entity stood before its latest move, and the tick that move
    # was seen: ``survival.approaching`` reads it (A9).
    entity_moves: dict[tuple[str, int], tuple[Pos, int]] = field(default_factory=dict)
    # The chest our last death dropped: (map_id, position, chest_id), from Died
    # (docs/API.md Events, B103). Cleared once it is gone: a dropped chest
    # leaves the world when its last supply is withdrawn (B116).
    death_chest: tuple[int, Pos, int] | None = None
    # Supply ids inside each ground chest within reach, from the round trip's
    # snapshot (entities.chests[].contents). A chest farther away is absent.
    chest_contents: dict[int, list[InventorySupply]] = field(default_factory=dict)
    # Zone facts from get_zone (A7): map_id -> cell -> fact. Safe tiles derive
    # from these and from terrain reads' ``MapView.safe`` (zone_discovery.safe_tiles).
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
        self.lives = int(s.get("lives", 0))
        if "asleep" in s:
            self._set_asleep(bool(s["asleep"]))
        self.placed = bool(s.get("placed", True)) or (self.woke and self.alive and not self.asleep)
        # Absent while nothing, or no weapon, is armed (B100).
        self.attack_range = _opt_int(s.get("attack_range"))

    def _set_asleep(self, asleep: bool) -> None:
        """A wake puts us on a block: placed until a position read says where (A5)."""
        if self.asleep and not asleep:
            self.placed = self.woke = True
        self.asleep = asleep

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
        self.placed, self.woke = True, False
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
            _set_safe(view, p, cell, full=True)
        self.terrain_center = self.pos
        self.terrain_map = self.map_id

    def apply_entities(self, e: dict) -> None:
        tick = int(e.get("tick", self.tick))
        self._set_entities(self._entities_from_payload(e), tick)
        self.entities_tick = self.entities_read_tick = tick
        self.entities_read_at = (self.map_id, self.pos)
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
        self._set_entities(list(by_key.values()), self.tick)

    def _set_entities(self, entities: list[Entity], tick: int) -> None:
        """Replaces the entity list, noting in ``entity_moves`` each one that moved
        and in ``npc_still`` how long each NPC has stood still (A65)."""
        before = {self._entity_key(e): e.pos for e in self.entities}
        for e in entities:
            key = self._entity_key(e)
            old = before.get(key)
            if old is not None and old != e.pos:
                self.entity_moves[key] = (old, tick)
        seen = {self._entity_key(e) for e in entities}
        for key in [k for k in self.entity_moves if k not in seen]:
            del self.entity_moves[key]
        self.entities = entities
        self._note_npc_cells(tick)
        self._note_sightings(tick)

    def _note_npc_cells(self, tick: int) -> None:
        """Keep ``npc_still`` for the NPCs in view: a moved NPC starts again."""
        still = {}
        for e in self.entities:
            if e.kind != "npc":
                continue
            seen = self.npc_still.get(e.id)
            still[e.id] = seen if seen is not None and seen[0] == e.pos else (e.pos, tick)
        self.npc_still = still

    def _note_sightings(self, tick: int) -> None:
        """Keep ``sightings``: refresh the NPCs and characters in view, note a
        post once one has stood still ``POST_STILL_TICKS``, and forget one
        that keeps no post once the cell it was last seen on is in sight with
        it gone, or it has been unseen for ``SIGHTING_TICKS`` on any map. A
        post out of view fades (``Sighting.fade_post``), fast while it is in
        sight with nobody on it, and is forgotten once faded below
        ``POST_FORGET_STRENGTH``; on a map left behind it fades by time alone."""
        in_view = set()
        for e in self.entities:
            if e.kind not in ("npc", "character"):
                continue
            key = self._entity_key(e)
            in_view.add(key)
            s = self.sightings.get(key)
            if s is None or s.map_id != self.map_id:
                s = self.sightings[key] = Sighting(e, self.map_id, tick, e.pos, noted=tick)
            elif s.post and not s.in_view and e.pos == s.home and tick - s.tick >= SIGHTING_TICKS:
                s.spells += 1  # back on its post after a while away: one more spell
            s.entity, s.tick, s.strength, s.noted, s.in_view = e, tick, 1.0, tick, True
            if not s.post and e.kind == "npc" and self.npc_still_ticks(e) >= POST_STILL_TICKS:
                s.home, s.post = e.pos, True
        for key, s in list(self.sightings.items()):
            if key in in_view:
                continue
            s.in_view = False
            cell = s.home if s.post else s.entity.pos
            here = self.pos if s.map_id == self.map_id else None
            gone = here is not None and chebyshev(cell, here) < self.perception
            if s.post:
                s.fade_post(tick, gone)
                if s.strength < POST_FORGET_STRENGTH:
                    del self.sightings[key]
            elif gone or tick - s.tick > SIGHTING_TICKS:
                del self.sightings[key]

    def npc_still_ticks(self, e: Entity) -> int:
        """How long ``e`` has stood on its cell while in view; 0 when it just moved or was never noted."""
        seen = self.npc_still.get(e.id)
        return self.tick - seen[1] if seen is not None and seen[0] == e.pos else 0

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
            _set_safe(v, p, cell)
        for cell in patch.get("removed") or []:
            map_id = int(cell["map_id"])
            p = (int(cell["x"]), int(cell["y"]))
            v = self.maps.setdefault(map_id, MapView())
            v.tiles.pop(p, None)
            v.damage.pop(p, None)
            v.readable.pop(p, None)
            v.locked.pop(p, None)
            v.safe.discard(p)

    def _apply_snapshot_terrain(self, terrain: dict) -> None:
        for cell in terrain.get("cells") or []:
            map_id = int(cell["map_id"])
            p = (int(cell["x"]), int(cell["y"]))
            v = self.maps.setdefault(map_id, MapView())
            v.tiles[p] = cell.get("block_type", "")
            _set_damage(v, p, cell)
            _set_readable(v, p, cell)
            _set_locked(v, p, cell)
            _set_safe(v, p, cell)

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
        if isinstance(body.get("levels_cleared"), list):
            self.levels_cleared = sorted({n for n in map(_opt_int, body["levels_cleared"]) if n is not None})
        if "asleep" in body:
            self._set_asleep(bool(body["asleep"]))
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
        armed = inv.get("armed")
        self.armed_id = _opt_int(armed.get("id")) if isinstance(armed, dict) else None
        self.worn_slots.update((code, slot) for slot, code in self.worn_codes.items())

    def _apply_snapshot_body(self, snap: dict) -> None:
        self._apply_body_scalars(snap)
        self._apply_vitals(snap, complete=True)
        self._apply_boss_fight_clock(snap, complete=True)
        if "inventory" in snap:
            self._apply_inventory(snap.get("inventory"))
        if "entities" in snap:
            entities = snap["entities"] or {}
            self._set_entities(self._entities_from_payload(entities), self.tick)
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
                if "tick" not in ev and "tick" in group:
                    ev = {**ev, "tick": group["tick"]}  # flat, each event keeps its tick (count_swings pairs on it)
                flat.append(ev)
                kind = ev.get("kind")
                if hostile_hit(ev):
                    t = int(ev.get("tick", group["tick"]))
                    self.attacked_tick = t
                    source = hitter(ev)
                    if source is not None:
                        self.attacker, self.attacker_tick = source, t
                    elif self.attacker_tick != t:
                        # A hit naming no one (an Attacked, or a source we cannot
                        # tell): we no longer know who is hitting us. One on the
                        # same tick as a named Damaged is that same swing.
                        self.attacker = self.attacker_tick = None
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
                    self.placed = self.woke = False  # off the map until Respawned: no position read can answer (A5)
                    if ev.get("chest_id"):
                        self.death_chest = (int(ev["map_id"]), (int(ev["x"]), int(ev["y"])), int(ev["chest_id"]))
                elif kind == "Respawned":
                    self.placed, self.attacked_tick = True, None
                    self.attacker = self.attacker_tick = None
                    self.carry_capacity = DEFAULT_CARRY_CAPACITY  # a new, empty blue chest (10), Manual §11
                    try:
                        self.record_respawn_anchor(int(ev["map_id"]), (int(ev["x"]), int(ev["y"])))
                    except (KeyError, TypeError, ValueError):
                        pass  # no location on the event: nothing to anchor probes to
        return flat

    def learn_threat(self, events: list[dict], earlier: list[Entity]) -> None:
        """Folds this round trip's Damaged events into the threat table (A6),
        with each hostile type's hits and misses (A85), and the NPC types its Attacked, Damaged and NPCDied events show hostile
        into ``hostile_types``.

        Call after apply_observation, so a source first listed in the same
        response resolves to its type. A hit is filed gross of the armor worn
        now (``threat.absorb_damaged``): the loadout this response leaves. A source that left view in that
        response is looked up in earlier, the entities before it. There is
        no entity list per event tick, so a source seen in neither is not
        recorded.
        """
        count_swings(self.threat, events, self.entities, earlier)
        armor = worn_armor_defense(self.worn_codes.values())
        for ev in events:
            if ev.get("kind") == "Damaged":
                absorb_damaged(self.threat, ev, self.entities, earlier, armor=armor)
            key = hostile_type_from_event(ev, self.entities, earlier)
            if key is not None:
                self.hostile_types.add(key)
            self._learn_reach(ev)

    def _learn_reach(self, ev: dict) -> None:
        """A hit stretches its hitter's ``Sighting.reach`` to where we stand
        from its post, so ground that far from its post is known to be in its
        reach; a hitter with no post learns none (its first-seen cell is no
        ground it keeps). An ``NPCDied`` forgets that NPC (free-play run 5)."""
        if ev.get("kind") == "NPCDied":
            self.sightings.pop(("npc", _opt_int(ev.get("npc_id"))), None)
            return
        source = hitter(ev)
        if source is None and ev.get("kind") == "Attacked" and ev.get("actor_kind") in ("npc", "character"):
            source = (ev["actor_kind"], ev.get("actor_id"))
        if source is not None:
            self._stretch_reach((source[0], _opt_int(source[1])))

    def note_came_for_us(self, e: Entity) -> None:
        """``e`` joined a fight with us here: when it has left its post to
        do so, like a hit, it stretches its post's reach to where we stand
        (``engagement.sync_engagement``). A guard still on its post came out
        for nobody, so it learns no reach."""
        s = self.sightings.get((e.kind, e.id))
        if s is not None and e.pos != s.home:
            self._stretch_reach((e.kind, e.id))

    def _stretch_reach(self, key: tuple[str, int | None]) -> None:
        s = self.sightings.get(key)
        if s is not None and s.post and self.pos is not None and s.map_id == self.map_id:
            s.reach = max(s.reach, chebyshev(self.pos, s.home))

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

    def for_sale(self) -> set[Pos]:
        """Cells of priced supplies in sight. Stepping onto one buys it, so
        no walk ever does: a buy is the Shop executor's ``Take`` (A21)."""
        return {e.pos for e in self.entities if e.kind == "supply" and e.gem_price}

    def entity_read_due(self, refresh: int) -> bool:
        """We moved since the last real entity read, and it is ``refresh`` ticks old.

        Tick deltas keep ``entities_tick`` fresh, but a walk into new ground
        still needs a real read on this fixed cadence (A16 Walk run 4).
        """
        if self.entities_read_at is None:
            return False  # no read yet: entities_tick still brings the first one
        moved = self.entities_read_at != (self.map_id, self.pos)
        return moved and self.tick - self.entities_read_tick >= refresh

    def damage_since(self, tick: int) -> int:
        return sum(a for t, a in self.recent_damage if t >= tick)

    def neighbours(self, p: Pos) -> list[Pos]:
        return [(p[0] + dx, p[1] + dy) for dx, dy in NEIGHBOURS]

    def open_neighbours(self, p: Pos, avoid: set[Pos] = frozenset()) -> list[Pos]:
        """Walkable, unoccupied tiles one step from p, minus avoid."""
        occ = self.occupied() | self.for_sale() | avoid
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


def _set_safe(view: MapView, p: Pos, cell: dict, *, full: bool = False) -> None:
    if cell.get("safe"):
        view.safe.add(p)
    elif full or "safe" in cell:
        view.safe.discard(p)


def _opt_int(v) -> int | None:
    if isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError, OverflowError):
        return None
