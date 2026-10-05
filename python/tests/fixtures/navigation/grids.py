"""Navigation scenarios for stuck detection (A15, PLAYABLE_AGENT_PLAN Navigation **Tests**).

Each scenario is the true map. The agent starts seeing only its perception
square; ``sim.run`` reveals more as it moves, so the planner meets fog the way
it does live. Outside the rows is wall: every map is bounded.

Glyphs: ``.`` dirt, ``#`` wall, ``b`` bush, ``~`` water, ``S`` start,
``G`` goal (dirt), ``D`` a door (the goal when there is no ``G``), ``N`` an
NPC parked on dirt. ``level`` puts the scenario on a level interior map, where
the Level state walks.
"""

from __future__ import annotations

from dataclasses import dataclass

from agentrealm_agent.world import Pos

GLYPHS = {".": "dirt", "#": "wall", "b": "bush", "~": "water", "S": "dirt", "G": "dirt", "N": "dirt", "D": "framed_door"}


@dataclass(frozen=True)
class Scenario:
    name: str
    rows: tuple[str, ...]
    perception: int = 2
    level: int | None = None

    def _find(self, glyph: str) -> list[Pos]:
        return [(x, y) for y, row in enumerate(self.rows) for x, g in enumerate(row) if g == glyph]

    @property
    def start(self) -> Pos:
        return self._find("S")[0]

    @property
    def goal(self) -> Pos:
        return (self._find("G") or self._find("D"))[0]

    @property
    def npcs(self) -> list[Pos]:
        return self._find("N")

    def block(self, p: Pos) -> str:
        x, y = p
        if 0 <= y < len(self.rows) and 0 <= x < len(self.rows[y]):
            return GLYPHS[self.rows[y][x]]
        return "wall"


# Greedy moves toward G walk into the cup; the way out is back past S.
U_TRAP = Scenario(
    "u_trap",
    (
        "...........",
        ".#######...",
        ".......#...",
        "...S...#.G.",
        ".......#...",
        ".#######...",
        "...........",
    ),
)

MAZE = Scenario(
    "maze",
    (
        "S.#.......",
        ".##.#####.",
        "....#...#.",
        "###.#.#.#.",
        "....#.#...",
        ".####.###.",
        "......#G..",
    ),
)

# A bush line across the whole map: no way round, and nothing to break it
# with until M9 (A28), so the goal is abandoned.
HEDGE_LINE = Scenario(
    "hedge_line",
    (
        "..S.....",
        "........",
        "bbbbbbbb",
        "........",
        "....G...",
    ),
)

# Water all round the goal. The ring is in sight from the start, so there is
# no path even with fog open; the rest of the map is fog for reveal to walk.
WATER_ENCLOSURE = Scenario(
    "water_enclosure",
    (
        "............",
        ".~~~........",
        ".~G~S.......",
        ".~~~........",
        "............",
        "............",
        "............",
    ),
    perception=3,
)

# A one-wide corridor with an NPC parked in it, and no other way through.
NPC_CORRIDOR = Scenario(
    "npc_corridor",
    (
        "#######",
        "S..N..G",
        "#######",
    ),
)

# The straight corridor toward G closes once its end is seen; the way round
# is the long loop south.
FOG_DEAD_END = Scenario(
    "fog_dead_end",
    (
        "S.......#.G",
        ".########.#",
        ".########.#",
        "...........",
    ),
)

# The same corridor with no way round: abandoned once the fog clears.
FOG_DEAD_END_CLOSED = Scenario(
    "fog_dead_end_closed",
    (
        "S.......#.G",
        "###########",
    ),
)

# Map 1: a hedge line blocks the door; map 2 holds the goal (cross-map stuck, A15).
CROSS_MAP_HEDGE = Scenario(
    "cross_map_hedge",
    (
        "..S....",
        ".......",
        "bbbbbbb",
        ".....D.",
    ),
    perception=3,
)

# Map 1: open corridor to the door; map 2 is a short walk to G (cross-map reach, A15).
CROSS_MAP_OPEN = Scenario(
    "cross_map_open",
    (
        "S...D..",
        ".......",
        ".......",
    ),
    perception=3,
)

# Straight corridor: fog beyond perception; goto walks 150+ blocks (A16 / M7).
OPEN_CORRIDOR_150 = Scenario(
    "open_corridor_150",
    ("S" + "." * 150 + "G",),
    perception=2,
)

# Inside a level: the door is in sight across a wall whose far end is fog, so
# Level plans round it; the wall runs the map's whole height, so the door is
# abandoned once the fog clears.
LEVEL_WALLED_DOOR = Scenario(
    "level_walled_door",
    (
        "...#....",
        "...#....",
        ".S.#D...",
        "...#....",
        "...#....",
        "...#....",
        "...#....",
        "...#....",
    ),
    perception=3,
    level=1,
)
