"""The agent's own model of the world. The server keeps no copy of it."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from .item_table import DEFAULT_CARRY_CAPACITY, InventorySupply, carried_from_inventory, supplies_from_list, worn_ids_from_inventory
from .supplies import afloat, chest_capacity, fords, worn_armor_defense
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


# block_types.traversal (migrations/00024_block_traversal.sql). Door types are
# warp: never occupied, but stepping onto one warps.
WALKABLE = {"grass", "dirt", "tile", "fire", "lava"}
# Walkable while a fording supply (a raft) is armed or worn (Manual Supplies
# capability table, ``fords``); a walk plans over it while one is carried (A133).
WATER = "water"


def walkable_block(block: str | None, fords: bool = False) -> bool:
    """Whether a block type is ground a walk may step on: ``WALKABLE``, and
    water while a fording supply is carried (``fords``, A133)."""
    return block in WALKABLE or (fords and block == WATER)
DOORS = {"framed_door", "rock_entry"}
VOID = ""  # no ground: a cell past the map's edge (a ``?`` inside the map is unknown, A146)
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


# A hostile that stands on one cell this long in view, not next to us, with
# no step that would close in on us, is blocked (Manual §11 Hostiles: a
# chaser stands still when no step closes in on the nearest character): the
# same stillness Greet takes for a helper (``investigation.HELPER_STILL_TICKS``).
# One next to us that stands still is swinging at us, not blocked.
STILL_TICKS = 50
# A hostile out of view that was moving is remembered where it was last
# seen for this long (60 s at 10 ticks/s), on any map: it goes for the
# nearest character, so that cell soon says little.
SIGHTING_TICKS = 600
# A blocked one stays where the ground stops it while the ground and the
# nearest character stay put, but either may change unseen, so it is
# remembered there out of view for at most this long (5 min at 10 ticks/s).
BLOCKED_SIGHTING_TICKS = 3000


@dataclass(frozen=True)
class Passage:
    """A door the character went through (A144): the door's map and cell, the
    map its warp landed on, the tick the landing was read, and the cell it
    landed on (A163: a boss room's exit door is the one by it)."""

    from_map: int
    from_pos: Pos
    to_map: int
    tick: int
    to_pos: Pos | None = None


@dataclass
class DeathChest:
    """A chest one of our deaths dropped, remembered until it is seen empty or gone (A122).

    ``contents`` are the subtype codes last seen in it, None until it was
    seen in reach. ``died`` is the tick it dropped: the largest is the newest.
    Kept across runs (``knowledge_base.death_chests``). A chest says neither
    whose it is nor when it goes (API Snapshots), so only our own deaths add
    one and only a look at it drops one.
    """

    map_id: int
    pos: Pos
    died: int
    contents: list[str] | None = None


@dataclass
class Sighting:
    """An NPC or character seen this run, kept after it leaves view (free-play run 5).

    A hostile has no post (Manual §11 Hostiles): it goes for the nearest
    living character on its map, with no sight or distance limit. A chaser
    steps greedily toward that character and stands still where no step
    closes in. ``still`` means blocked, not merely standing still: it stood
    on its cell for ``STILL_TICKS`` in view, not next to us (one next to us
    is swinging at us), and no step would bring it closer to us
    (``WorldModel.blocked``). The mark is cleared the next time it is seen
    otherwise (moving, next to us, or with a step open). A blocked one is
    remembered out of view where it stood until that cell is in sight with
    it gone, for at most ``BLOCKED_SIGHTING_TICKS``; one that was moving
    for ``SIGHTING_TICKS``. Whether it is a threat is asked at use
    (``survival.is_hostile``), so a type found hostile later counts.
    """

    entity: Entity  # as last seen
    map_id: int | None
    tick: int  # last seen
    still: bool = False
    in_view: bool = True


@dataclass(frozen=True)
class ZoneFact:
    """A get_zone answer for one cell (A7)."""

    safe: bool
    brightness: float = 1.0
    strength_ceiling: int | None = None


NEIGHBOURS = [(dx, dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1) if (dx, dy) != (0, 0)]


def chebyshev(a: Pos, b: Pos) -> int:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def chaser_step(at: Pos, target: Pos, ground: Callable[[Pos], bool]) -> Pos:
    """Where a chaser on ``at`` stands after one step toward ``target``
    (Manual §11 Hostiles): the neighbouring block that brings it closest,
    nearest by Chebyshev distance then straight-line, never onto ``target``
    itself, and ``at`` when no step on ``ground`` brings it closer. It finds
    no path, so a wall or water between stops it."""

    def gap(p: Pos) -> tuple[int, int]:
        return chebyshev(p, target), (p[0] - target[0]) ** 2 + (p[1] - target[1]) ** 2

    best, best_gap = at, gap(at)
    for dx, dy in NEIGHBOURS:
        n = (at[0] + dx, at[1] + dy)
        if n != target and ground(n) and gap(n) < best_gap:
            best, best_gap = n, gap(n)
    return best


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

    # The frontier as last found, and the cells changed since, kept apart
    # for each ``fords`` (A133). A changes entry None: find it afresh.
    _frontier: dict[bool, set[Pos]] | None = None
    _changes: dict[bool, set[Pos] | None] | None = None
    MAX_CHANGES = 4096  # more than this and a fresh pass is as quick
    # Counts the calls that may have changed a tile, so a search over this map
    # can be kept until it does (A120, ``states.way``).
    revision: int = 0

    def frontier(self, fords: bool = False) -> set[Pos]:
        """Known walkable tiles that touch an unknown one: water too when
        ``fords`` (a fording supply is carried, A133)."""
        if self._frontier is None or self._changes is None:
            self._frontier, self._changes = {}, {}
        found, changes = self._frontier.get(fords), self._changes.get(fords)
        if found is None or changes is None:
            found = {p for p in self if self._on_frontier(p, fords)}
        else:
            for c in changes:
                for p in [c] + [(c[0] + dx, c[1] + dy) for dx, dy in NEIGHBOURS]:
                    if self._on_frontier(p, fords):
                        found.add(p)
                    else:
                        found.discard(p)
        self._frontier[fords], self._changes[fords] = found, set()
        return set(found)

    def _on_frontier(self, p: Pos, fords: bool = False) -> bool:
        if not walkable_block(self.get(p), fords):
            return False
        x, y = p
        for dx, dy in NEIGHBOURS:
            if (x + dx, y + dy) not in self:
                return True
        return False

    def _changed(self, keys) -> None:
        self.revision += 1
        if not self._changes:
            return
        keys = list(keys)
        for fords, changes in self._changes.items():
            if changes is None:
                continue
            changes.update(keys)
            if len(changes) > self.MAX_CHANGES:
                self._changes[fords] = None

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
        self.revision += 1
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

    def walkable(self, p: Pos, *, fords: bool) -> bool:
        """Whether ``p`` is ground a walk may step on (``walkable_block``).
        ``fords``: a fording supply is carried, always named by the caller
        and never kept on the view (A133); outside this module only
        ``WorldModel.walkable`` decides it (A146)."""
        return walkable_block(self.tiles.get(p), fords)

    def occupy_damage(self, p: Pos) -> int | None:
        """The tile's occupy_damage, or None when no read has named it."""
        return self.damage.get(p)

    def frontier(self, *, fords: bool) -> set[Pos]:
        """Known walkable tiles that touch an unknown one (``Tiles.frontier``),
        water among them when ``fords`` (named, as for ``walkable``)."""
        return self.tiles.frontier(fords)


@dataclass
class WorldModel:
    character_id: int
    map_id: int | None = None
    map_level: int | None = None  # level number of the current map: 0 outside every level, None while unread (A145)
    map_levels: dict[int, int] = field(default_factory=dict)  # map id -> its level from the minimap, 0 outside every level (A145)
    map_sizes: dict[int, tuple[int, int]] = field(default_factory=dict)  # map id -> (width, height), from a minimap or terrain read (A146)
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
    # A self read and an inventory (a snapshot's ``inventory``) have each been
    # applied at least once: until both, armed, held and gems are unread, not
    # empty (``synced``).
    self_read: bool = False
    inventory_read: bool = False
    levels_cleared: list[int] = field(default_factory=list)  # level numbers cleared, from snapshots (A62)
    level_count: int | None = None  # levels the world authored, from GetWorld (A62)
    gems: int | None = None  # inventory counter from snapshots (A22)
    health: int | None = None
    max_health: int | None = None
    attack_range: int | None = None  # armed weapon reach from get_self (B100)
    armed_code: str | None = None
    armed_id: int | None = None  # supply id in the armed slot, when the snapshot names it (A76)
    worn_codes: dict[str, str] = field(default_factory=dict)
    worn_ids: dict[str, int] = field(default_factory=dict)  # worn slot -> supply id, when the snapshot names it (A154)
    worn_slots: dict[str, str] = field(default_factory=dict)  # subtype -> slot a snapshot served it worn in, for the run (A19)
    held_supplies: list[InventorySupply] = field(default_factory=list)  # inventory held[] (A10, A20)
    chest_supplies: list[InventorySupply] = field(default_factory=list)
    # Carry capacity is derived, never assumed (A115; ``sync_carry_capacity``):
    # the server serves no capacity and no carried chest's subtype, so it is the
    # size of the chest we applied (``carried_chest``, None for the blue chest a
    # character starts with), bounded by the most a carry_capacity_full refusal
    # left room for (``full_at``, ``loot.full_bound``) and never below the slots
    # the inventory carries. Every death resets it to the blue chest (A157,
    # Manual §11 How big your chest is, Death): a death seen (``Died``), or one
    # not seen, shown by fewer lives than when the chest was saved
    # (``chest_lives``, ``loot.load_carried_chest``) or by a served death
    # chest we never knew of.
    carried_chest: str | None = None
    chest_lives: int | None = None  # lives when the loaded chest was saved; checked at the first lives read
    full_at: int | None = None
    carry_capacity: int = DEFAULT_CARRY_CAPACITY
    undroppable: set[int] = field(default_factory=set)  # supply ids Drop refused not_transferable
    tick: int = 0
    passage: Passage | None = None  # the last door gone through (A144)
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
    # earlier one (``knowledge_base.hostiles``). Townsfolk and helpers never land here,
    # so Flee and Retreat never answer them (A9, A23).
    hostile_types: set[TypeKey] = field(default_factory=set)
    # (kind, id) -> where each NPC and character was last seen, and whether
    # it stood still there, kept out of view (``Sighting``, free-play run 5);
    # hostile NPCs' sightings are kept for later runs (``knowledge_base.hostiles``).
    sightings: dict[tuple[str, int], Sighting] = field(default_factory=dict)
    # NPC id -> the damage it has taken, from every ``NPCDamaged`` seen,
    # anyone's swing (Manual §11 Hostiles: a hostile never heals while it
    # lives). Dropped on its ``NPCDied``: it returns at full health. This run
    # only, since a death out of sight is never seen (``survival.health_left``).
    npc_damage: dict[int, int] = field(default_factory=dict)
    # Where each entity stood before its latest move, and the tick that move
    # was seen: ``survival.approaching`` reads it (A9).
    entity_moves: dict[tuple[str, int], tuple[Pos, int]] = field(default_factory=dict)
    # Every chest our deaths dropped, by chest id (A122): from Died and from
    # the observation's ``death_chest`` (API Events and Snapshots, B103, B124).
    # One is dropped once seen empty or gone: a dropped chest leaves the world
    # when its last supply is withdrawn (B116).
    death_chests: dict[int, DeathChest] = field(default_factory=dict)
    # Death chest ids seen empty or gone, oldest first: never remembered again,
    # though a served ``death_chest`` still names one. Kept across runs, the
    # latest ``death_chests.GONE_KEPT`` (A122).
    death_chests_gone: list[int] = field(default_factory=list)
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

    @property
    def fords(self) -> bool:
        """A carried supply fords water (A133): held, armed or worn. Holding
        one is enough to plan a walk over water; the walk arms it before the
        first water step (``fording.ford_queue``)."""
        carried = [h.code for h in self.held_supplies] + list(self.worn_codes.values())
        return any(fords(code) for code in carried + [self.armed_code])

    @property
    def afloat(self) -> bool:
        """A fording supply is armed or worn: water may be stepped on now,
        with no ``Arm`` first (``supplies.afloat``, A133)."""
        return afloat(self.armed_code, self.worn_codes.values())

    @property
    def map_size(self) -> tuple[int, int] | None:
        """The current map's width and height (``map_sizes``), None until a
        minimap or terrain read gives them."""
        return None if self.map_id is None else self.map_sizes.get(self.map_id)

    def walkable(self, p: Pos, *, map_id: int | None = None, now: bool = False, swing: bool = False) -> bool:
        """``p`` is ground a walk may step on, water too while a fording
        supply is carried (A133): the one test of whether water is ground
        (A146). Every route search, grid and step check asks it, on any map,
        a level's or a room's as much as the overworld's; one that asks for
        many cells takes ``walkable_test`` once instead.

        ``map_id``: another map than the current one. ``now``: water only
        while a fording supply is already armed or worn (``afloat``), for a
        lone Step with no ``Arm`` before it. ``swing``: a walk to swing at a
        hostile from, water only while a fording supply is worn, since one in
        the hand leaves no weapon there (A146)."""
        return self.walkable_test(map_id=map_id, now=now, swing=swing)(p)

    def chaser_ground(self) -> Callable[[Pos], bool]:
        """The cells a hostile may step on, as a test of one cell
        (``hostile_ground.chase``): walkable ground, never water, since a
        hostile carries no fording supply, and unseen ground, which may be
        walkable (Manual §11 Hostiles)."""
        tiles = self.view.tiles

        def ground(p: Pos) -> bool:
            block = tiles.get(p)
            return block is None or walkable_block(block)

        return ground

    def walkable_test(
        self, *, map_id: int | None = None, now: bool = False, swing: bool = False
    ) -> Callable[[Pos], bool]:
        """``walkable`` as a test of one cell, with whether water is ground
        worked out once from what is carried: for a search or a loop over
        many cells, which must not change the loadout while it runs."""
        if map_id is None or map_id == self.map_id:
            view = self.view
        else:
            view = self.maps.get(map_id) or MapView()
        if swing:
            water = afloat(None, self.worn_codes.values())
        else:
            water = self.afloat if now else self.fords
        return lambda p: view.walkable(p, fords=water)

    def frontier(self) -> set[Pos]:
        """The current map's frontier (``MapView.frontier``), water's edge
        too while a fording supply is carried (A133)."""
        return self.view.frontier(fords=self.fords)

    # Applying reads.

    def apply_self(self, s: dict) -> None:
        self.perception = max(1, int(s.get("perception_range", 1)))
        self.movement = max(1, int(s.get("movement_range", 1)))
        if "movement_speed" in s:
            self.movement_speed = max(1, int(s["movement_speed"]))
        self.alive = bool(s.get("alive", True))
        if "lives" in s:
            self._read_lives(int(s["lives"]))
        if "asleep" in s:
            self._set_asleep(bool(s["asleep"]))
        self.placed = bool(s.get("placed", True)) or (self.woke and self.alive and not self.asleep)
        # Absent while nothing, or no weapon, is armed (B100).
        self.attack_range = _opt_int(s.get("attack_range"))
        self._apply_vitals(s, complete=False)  # the self read serves health
        self.self_read = True

    def _set_asleep(self, asleep: bool) -> None:
        """A wake puts us on a block: placed until a position read says where (A5)."""
        if self.asleep and not asleep:
            self.placed = self.woke = True
        self.asleep = asleep

    def apply_position(self, p: dict) -> None:
        # `level` belongs to the map and comes only from its minimap entry
        # (`apply_minimap`, A145): the position read and the snapshot's
        # `position` carry none (Manual §5.3, §11 Bosses and levels, A164), so
        # nothing here reads one. A new map takes the level the minimap gave
        # it, else none until a minimap read names it.
        map_id = int(p["map_id"])
        if map_id != self.map_id:
            self.snapshot_version = None  # entities no longer match its base
            self.map_level = self.map_levels.get(map_id)
        self.map_id = map_id
        self.pos = (int(p["x"]), int(p["y"]))
        self.asleep = False  # a sleeping character is off the map (GAME_NOTES Sleep)
        self.placed, self.woke = True, False

    def apply_minimap(self, body: dict) -> None:
        """Each revealed map's ``level`` from a minimap read (A145): a map inside
        a level carries it, one outside every level omits it. The position
        read carries none, so this is the only place the current map's level
        comes from (A164)."""
        for m in body.get("maps") or []:
            if not isinstance(m, dict):
                continue
            map_id, level = _opt_int(m.get("map_id")), _opt_int(m.get("level"))
            if map_id is None:
                continue
            self.map_levels[map_id] = level if level is not None and level > 0 else 0
            self._note_map_size(map_id, m)
        if self.map_id in self.map_levels:
            self.map_level = self.map_levels[self.map_id]

    def _note_map_size(self, map_id: int, body: dict) -> None:
        """Keep a map's ``map_width`` and ``map_height`` when a read names both."""
        width, height = _opt_int(body.get("map_width")), _opt_int(body.get("map_height"))
        if width is not None and height is not None:
            self.map_sizes[map_id] = (width, height)

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

        A ``?`` in the read is unknown, never empty ground (API Tiles, B102):
        ground the character has neither in sight nor revealed, which in a
        dark zone is most of the window, since sight there is perception
        scaled by brightness (A146). Only a ``?`` past the map's edge has no
        ground: it is recorded as VOID, known and not walkable. The edge is
        the map's size as this read gives it, else as a minimap or an earlier
        read gave it (``map_sizes``), else only the near edges, below x 0 and
        y 0, are known. A ``?`` inside the map stays unknown, so a walk plans
        through it at fog cost and Explore goes to see it.
        """
        map_id = int(t["map_id"])
        view = self.maps.setdefault(map_id, MapView())
        self._note_map_size(map_id, t)
        x0, y0 = int(t["x0"]), int(t["y0"])
        width, height = self.map_sizes.get(map_id, (None, None))
        for y in range(y0, y0 + int(t["height"])):
            for x in range(x0, x0 + int(t["width"])):
                if x < 0 or y < 0 or (width is not None and (x >= width or y >= height)):
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
        """Keep ``sightings``: refresh the NPCs and characters in view, each
        ``still`` while it is ``blocked``, and forget one out of view once the
        cell it was last seen on is in sight with it gone, or once it has
        been unseen for ``SIGHTING_TICKS`` (a blocked one
        ``BLOCKED_SIGHTING_TICKS``) on any map (``Sighting``)."""
        in_view = set()
        for e in self.entities:
            if e.kind not in ("npc", "character"):
                continue
            key = self._entity_key(e)
            in_view.add(key)
            s = self.sightings.get(key)
            if s is None or s.map_id != self.map_id:
                s = self.sightings[key] = Sighting(e, self.map_id, tick)
            s.entity, s.tick, s.in_view = e, tick, True
            s.still = self.blocked(e)
        for key, s in list(self.sightings.items()):
            if key in in_view:
                continue
            s.in_view = False
            here = self.pos if s.map_id == self.map_id else None
            gone = here is not None and chebyshev(s.entity.pos, here) < self.perception
            if gone or tick - s.tick > (BLOCKED_SIGHTING_TICKS if s.still else SIGHTING_TICKS):
                del self.sightings[key]

    def blocked(self, e: Entity) -> bool:
        """``e`` is a hostile the ground stops (Manual §11 Hostiles): an NPC
        that has stood on its cell ``STILL_TICKS`` in view, not next to us
        (standing there it is swinging at us), and with no step on the
        ground a hostile walks that would bring it closer to us
        (``chaser_step``). Merely standing still is not being blocked."""
        if e.kind != "npc" or self.npc_still_ticks(e) < STILL_TICKS or self.pos is None:
            return False
        if chebyshev(e.pos, self.pos) <= 1:
            return False
        return chaser_step(e.pos, self.pos, self.chaser_ground()) == e.pos

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

    def newest_death_chest(self) -> tuple[int, DeathChest] | None:
        """The chest our latest death dropped, while it is remembered: (chest id, chest)."""
        if not self.death_chests:
            return None
        return max(self.death_chests.items(), key=lambda kv: (kv[1].died, kv[0]))

    def remember_death_chest(self, chest_id: int, map_id: int, at: Pos, died: int | None = None) -> None:
        """Remember a chest one of our deaths dropped at tick ``died`` (now when not known);
        one already known keeps what was seen of it."""
        if chest_id not in self.death_chests and chest_id not in self.death_chests_gone:
            self.death_chests[chest_id] = DeathChest(map_id, at, self.tick if died is None else died)

    def _refresh_death_chests(self) -> None:
        """Note what is seen of each remembered death chest on this map (A122):
        its contents once in reach, and drop it once seen empty, or gone while
        in reach. Out of reach, a chest missing from sight may only be unseen."""
        here = self.pos
        if here is None:
            return
        for chest_id, chest in list(self.death_chests.items()):
            if chest.map_id != self.map_id:
                continue
            contents = self.chest_contents.get(chest_id)
            gone = chebyshev(chest.pos, here) <= 1 and contents is None
            if gone or contents == []:
                del self.death_chests[chest_id]
                self.death_chests_gone.append(chest_id)
            elif contents is not None:
                chest.contents = [s.code for s in contents]

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
            self._read_lives(int(body["lives"]))
        if "alive" in body:
            self.alive = bool(body["alive"])
        if isinstance(body.get("levels_cleared"), list):
            self.levels_cleared = sorted({n for n in map(_opt_int, body["levels_cleared"]) if n is not None})
        if "asleep" in body:
            self._set_asleep(bool(body["asleep"]))
        served = body.get("death_chest")
        if isinstance(served, dict):
            try:
                chest_id = int(served["chest_id"])
                if chest_id not in self.death_chests and chest_id not in self.death_chests_gone:
                    self.reset_carried_chest()  # a death we never saw
                self.remember_death_chest(chest_id, int(served["map_id"]), (int(served["x"]), int(served["y"])))
            except (KeyError, TypeError, ValueError):
                pass  # a malformed field names no chest
        if "position" in body:
            pos = body["position"]
            if pos is None:
                self.forget_position()
            else:
                self.apply_position(pos)

    def note_level_clear(self, ceremony: dict | None) -> None:
        """Records a round trip's one-shot ``level_clear_ceremony`` (A38), and
        its ``level_number`` as cleared before the next snapshot says so (A145)."""
        if ceremony:
            self.level_clear_tick = self.tick
            level = _opt_int(ceremony.get("level_number")) if isinstance(ceremony, dict) else None
            if level is not None and level not in self.levels_cleared:
                self.levels_cleared = sorted({*self.levels_cleared, level})

    def _apply_inventory(self, inv: dict | None) -> None:
        if inv is None:
            return
        self.inventory_read = True
        if "gems" in inv:
            gems = _opt_int(inv.get("gems"))
            if gems is not None and gems >= 0:
                self.gems = gems
        self.held_supplies, self.chest_supplies, self.armed_code, self.worn_codes = carried_from_inventory(inv)
        armed = inv.get("armed")
        self.armed_id = _opt_int(armed.get("id")) if isinstance(armed, dict) else None
        self.worn_ids = worn_ids_from_inventory(inv)
        self.worn_slots.update((code, slot) for slot, code in self.worn_codes.items())
        self.sync_carry_capacity()

    def _apply_snapshot_body(self, snap: dict) -> None:
        self._apply_body_scalars(snap)
        self._apply_vitals(snap, complete=True)
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
        self._refresh_death_chests()

    def _apply_delta_body(self, delta: dict) -> None:
        self._apply_body_scalars(delta)
        self._apply_vitals(delta, complete=False)
        if "inventory" in delta:
            self._apply_inventory(delta.get("inventory"))
        if "entities" in delta:
            self._apply_entity_delta(delta["entities"])
            self.entities_tick = self.tick
        if "terrain" in delta:
            self._apply_terrain_delta(delta["terrain"])
        self._refresh_death_chests()

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
                    self.reset_carried_chest()
                    self.placed = self.woke = False  # off the map until Respawned: no position read can answer (A5)
                    if ev.get("chest_id"):
                        self.remember_death_chest(
                            int(ev["chest_id"]), int(ev["map_id"]), (int(ev["x"]), int(ev["y"])), int(ev.get("tick", group["tick"]))
                        )
                elif kind == "Respawned":
                    self.placed, self.attacked_tick = True, None
                    self.attacker = self.attacker_tick = None
                    self.reset_carried_chest()
                    try:
                        self.record_respawn_anchor(int(ev["map_id"]), (int(ev["x"]), int(ev["y"])))
                    except (KeyError, TypeError, ValueError):
                        pass  # no location on the event: nothing to anchor probes to
        return flat

    def learn_threat(self, events: list[dict], earlier: list[Entity]) -> None:
        """Folds this round trip's Damaged events into the threat table (A6),
        with each hostile type's hits and misses (A85), and the NPC types its
        Attacked, Damaged and NPCDied events show hostile into
        ``hostile_types``.

        Call after apply_observation, so a source first listed in the same
        response resolves to its type. A source that left view in that
        response is looked up in earlier, the entities before it. There is
        no entity list per event tick, so a source seen in neither is not
        recorded.

        A hit is filed gross of the armor worn now (``threat.absorb_damaged``),
        the loadout this response leaves: a hit landed before a swap in the
        same response is filed off by the armor swapped.
        """
        count_swings(self.threat, events, self.entities, earlier)
        armor = worn_armor_defense(self.worn_codes.values())
        for ev in events:
            if ev.get("kind") == "Damaged":
                absorb_damaged(self.threat, ev, self.entities, earlier, armor=armor)
            key = hostile_type_from_event(ev, self.entities, earlier)
            if key is not None:
                self.hostile_types.add(key)
            self._learn_npc_damage(ev)

    def _learn_npc_damage(self, ev: dict) -> None:
        """An ``NPCDamaged`` adds its amount to that NPC's ``npc_damage``,
        whoever swung: a hostile never heals while it lives. An ``NPCDied``
        forgets the NPC's sighting and damage: it returns at full health
        (free-play run 5, Manual §11 Hostiles)."""
        kind = ev.get("kind")
        npc_id = _opt_int(ev.get("npc_id"))
        if npc_id is None:
            return
        if kind == "NPCDied":
            self.sightings.pop(("npc", npc_id), None)
            self.npc_damage.pop(npc_id, None)
        elif kind == "NPCDamaged":
            amount = damage_amount(ev)
            if amount is not None and amount > 0:
                self.npc_damage[npc_id] = self.npc_damage.get(npc_id, 0) + amount

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

    def loadout_supplies(self) -> list[InventorySupply]:
        """The armed and worn supplies whose ids a snapshot named. Each supply
        is in exactly one place (Manual §11 Where your supplies are, A154), so
        none of these is also in ``held_supplies``."""
        armed = [InventorySupply(self.armed_id, self.armed_code)] if self.armed_id is not None and self.armed_code else []
        worn = [InventorySupply(sid, self.worn_codes[slot]) for slot, sid in sorted(self.worn_ids.items()) if slot in self.worn_codes]
        return armed + worn

    def slots_carried(self) -> int:
        """Slots the inventory fills: held, stowed, worn and armed (gems and
        lives are counters and take none)."""
        n = len(self.held_supplies) + len(self.chest_supplies) + len(self.worn_codes)
        return n + (1 if self.armed_code else 0)

    def reset_carried_chest(self) -> None:
        """A death drops the carried chest and the respawn brings a new, empty
        blue chest (10): any upgrade applied dies with it (A157, Manual §11
        How big your chest is, Death)."""
        self.carried_chest = self.full_at = self.chest_lives = None
        self.sync_carry_capacity()

    def _read_lives(self, lives: int) -> None:
        """Lives from a self read or a snapshot that carries them (a read
        without them leaves lives unknown and checks nothing). The first one
        after the applied chest was loaded checks it: fewer lives than when it
        was saved is a death since, which reset it (A157)."""
        self.lives = lives
        if self.chest_lives is not None:
            if lives < self.chest_lives:
                self.reset_carried_chest()
            self.chest_lives = None

    def sync_carry_capacity(self) -> None:
        """Carry capacity from what the server tells us, at every inventory
        sync (A115): the carried chest's Supplies reference ``chest_capacity``,
        no more than a ``carry_capacity_full`` refusal left room for, and never
        less than the slots the inventory carries now."""
        size = chest_capacity(self.carried_chest) or DEFAULT_CARRY_CAPACITY
        if self.full_at is not None:
            size = min(size, self.full_at)
        self.carry_capacity = max(size, self.slots_carried())

    @property
    def synced(self) -> bool:
        """Self and inventory have each been read once: what the character is,
        carries, wears and holds in gems is known, not merely unread."""
        return self.self_read and self.inventory_read

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

    def open_neighbours(self, p: Pos, avoid: set[Pos] = frozenset(), *, swing: bool = False) -> list[Pos]:
        """Walkable, unoccupied tiles one step from p, minus avoid (``swing``:
        as ``walkable``)."""
        occ = self.occupied() | self.for_sale() | avoid
        return [n for n in self.neighbours(p) if self.walkable(n, swing=swing) and n not in occ]


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
