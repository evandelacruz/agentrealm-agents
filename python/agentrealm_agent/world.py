"""The agent's own model of the world. The server keeps no copy of it."""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field

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

# Extra cost of a step onto a `costly` tile: worth a long detour to avoid one.
COSTLY_STEP = 100

NEIGHBOURS = [(dx, dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1) if (dx, dy) != (0, 0)]


def chebyshev(a: Pos, b: Pos) -> int:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


@dataclass
class Entity:
    kind: str  # character | npc | supply | chest
    id: int
    pos: Pos
    code: str = ""  # outfit, npc type, or supply subtype; empty for a chest


@dataclass
class MapView:
    """What this character has seen of one map. Missing tiles are unknown."""

    tiles: dict[Pos, str] = field(default_factory=dict)

    def walkable(self, p: Pos) -> bool:
        return self.tiles.get(p) in WALKABLE

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
    pos: Pos | None = None
    perception: int = 1
    movement: int = 1
    alive: bool = True
    lives: int = 0
    tick: int = 0
    maps: dict[int, MapView] = field(default_factory=dict)
    entities: list[Entity] = field(default_factory=list)
    entities_tick: int = -10**9  # tick of the last entity read
    terrain_center: Pos | None = None  # where we stood at the last terrain read
    terrain_map: int | None = None
    recent_damage: list[tuple[int, int]] = field(default_factory=list)  # (tick, amount)
    # The chest our last death dropped: (map_id, position, chest_id), from Died
    # (docs/API.md Events, B103). Cleared once it is gone: a dropped chest
    # leaves the world when its last supply is withdrawn (B116).
    death_chest: tuple[int, Pos, int] | None = None
    # Supply ids inside each ground chest within reach, from the round trip's
    # snapshot (entities.chests[].contents). A chest farther away is absent.
    chest_contents: dict[int, list[int]] = field(default_factory=dict)

    @property
    def view(self) -> MapView:
        assert self.map_id is not None
        return self.maps.setdefault(self.map_id, MapView())

    # Applying reads.

    def apply_self(self, s: dict) -> None:
        self.perception = max(1, int(s.get("perception_range", 1)))
        self.movement = max(1, int(s.get("movement_range", 1)))
        self.alive = bool(s.get("alive", True))
        self.lives = int(s.get("lives", 0))

    def apply_position(self, p: dict) -> None:
        self.map_id = int(p["map_id"])
        self.pos = (int(p["x"]), int(p["y"]))

    def perception_rect(self) -> tuple[int, int, int, int]:
        """x0, y0, width, height of the perception window around us."""
        assert self.pos is not None
        r = self.perception
        return self.pos[0] - r, self.pos[1] - r, 2 * r + 1, 2 * r + 1

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
            view.tiles[(x, y)] = cell["block_type"]
        self.terrain_center = self.pos
        self.terrain_map = self.map_id

    def apply_entities(self, e: dict) -> None:
        out = []
        for c in e.get("characters") or []:
            if int(c["id"]) != self.character_id:
                out.append(Entity("character", int(c["id"]), (int(c["x"]), int(c["y"])), c.get("outfit_code", "")))
        for n in e.get("npcs") or []:
            out.append(Entity("npc", int(n["id"]), (int(n["x"]), int(n["y"])), n.get("npc_type_code", "")))
        for s in e.get("supplies") or []:
            out.append(Entity("supply", int(s["id"]), (int(s["x"]), int(s["y"])), s.get("supply_subtype_code", "")))
        for ch in e.get("chests") or []:
            out.append(Entity("chest", int(ch["id"]), (int(ch["x"]), int(ch["y"]))))
        self.entities = out
        self.entities_tick = int(e.get("tick", self.tick))

    def apply_events(self, events_by_tick: list[dict]) -> list[dict]:
        """Folds durable facts from events into the model and returns them flat.

        Attacked, Damaged, and Died on a queue happened to the queue owner and
        carry no subject_id (docs/API.md, Events), so each one is ours.
        """
        flat = []
        for group in events_by_tick or []:
            for ev in group.get("events") or []:
                flat.append(ev)
                kind = ev.get("kind")
                if kind == "Damaged":
                    self.recent_damage.append((int(ev.get("tick", group["tick"])), int(ev.get("amount", 0))))
                elif kind == "BlockChanged" and ev.get("map_id") in self.maps:
                    self.maps[ev["map_id"]].tiles[(int(ev["x"]), int(ev["y"]))] = ev.get("block_type", "")
                elif kind == "SupplyTaken":
                    self.entities = [x for x in self.entities if not (x.kind == "supply" and x.id == ev.get("supply_id"))]
                elif kind == "Died":
                    self.forget_position()
                    if ev.get("chest_id"):
                        self.death_chest = (int(ev["map_id"]), (int(ev["x"]), int(ev["y"])), int(ev["chest_id"]))
        return flat

    def apply_observation(self, obs: dict | None) -> None:
        """Reads ground chest contents from a complete snapshot.

        This agent never sends snapshot_version, so every observation it gets
        is complete (docs/API.md Snapshots). A chest within reach lists its
        contents. A dropped chest leaves the world once emptied (B116), so our
        death chest absent while its block is within reach was emptied, by us
        or by someone first, and is no longer worth going back for; one seen
        empty is not either.
        """
        if not obs or not obs.get("complete"):
            return
        entities = (obs.get("snapshot") or {}).get("entities") or {}
        self.chest_contents = {
            int(ch["id"]): [int(s["id"]) for s in ch["contents"]]
            for ch in entities.get("chests") or []
            if "contents" in ch
        }
        if self.death_chest is not None:
            map_id, at, chest_id = self.death_chest
            here = self.pos if self.map_id == map_id else None
            gone = here is not None and chebyshev(at, here) <= 1 and chest_id not in self.chest_contents
            if gone or self.chest_contents.get(chest_id) == []:
                self.death_chest = None

    def forget_position(self) -> None:
        self.pos = None
        self.map_id = None

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

    def path(
        self, goal: Pos, allow_goal_door: bool = False, avoid: set[Pos] = frozenset(), costly: set[Pos] = frozenset()
    ) -> list[Pos] | None:
        """A* over known walkable tiles, Chebyshev steps. Excludes the start.

        Occupied tiles and avoid are never entered; a step onto a costly tile
        costs COSTLY_STEP more, so the path crosses as few as it can. With
        allow_goal_door, the goal may be a door: the last step lands on it and
        warps.
        """
        assert self.pos is not None
        start = self.pos
        if start == goal:
            return []
        view = self.view
        occ = self.occupied() | avoid

        def passable(p: Pos) -> bool:
            if p == goal and allow_goal_door and view.tiles.get(p) in DOORS and p not in avoid:
                return True
            return view.walkable(p) and p not in occ

        if not passable(goal):
            return None
        frontier = [(chebyshev(start, goal), 0, start)]
        came: dict[Pos, Pos] = {}
        cost = {start: 0}
        while frontier:
            _, g, cur = heapq.heappop(frontier)
            if cur == goal:
                out = [cur]
                while out[-1] in came and came[out[-1]] != start:
                    out.append(came[out[-1]])
                return out[::-1]
            if g > cost.get(cur, 10**9):
                continue
            for n in self.neighbours(cur):
                if not passable(n):
                    continue
                ng = g + self.step_cost(n, costly)
                if ng < cost.get(n, 10**9):
                    cost[n] = ng
                    came[n] = cur
                    heapq.heappush(frontier, (ng + chebyshev(n, goal), ng, n))
        return None

    def step_cost(self, p: Pos, costly: set[Pos]) -> int:
        return 1 + COSTLY_STEP if p in costly else 1

    def nearest(
        self, targets: set[Pos], allow_goal_door: bool = False, avoid: set[Pos] = frozenset(),
        costly: set[Pos] = frozenset(),
    ) -> tuple[Pos, list[Pos]] | None:
        """The closest target by path cost, with its path.

        Tries targets in straight-line order and stops once no remaining target
        can beat the best path found.
        """
        assert self.pos is not None
        best: tuple[Pos, list[Pos]] | None = None
        best_cost = 0
        for t in sorted(targets, key=lambda t: chebyshev(self.pos, t)):
            if best is not None and chebyshev(self.pos, t) >= best_cost:
                break
            p = self.path(t, allow_goal_door, avoid, costly)
            if p is None:
                continue
            c = sum(self.step_cost(q, costly) for q in p)
            if best is None or c < best_cost:
                best, best_cost = (t, p), c
        return best
