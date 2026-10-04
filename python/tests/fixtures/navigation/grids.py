"""Navigation scenario grids for stuck detection (A15)."""

from __future__ import annotations

from agentrealm_agent.world import Entity, WorldModel
from tests.test_cost_grid import grid


def u_trap(at: tuple[int, int] = (1, 1)) -> WorldModel:
    """A U-shaped basin: greedy descent can trap before finding the exit."""
    rows = [
        "#######",
        "#.....#",
        "#.###.#",
        "#.#.#.#",
        "#.....#",
        "#######",
    ]
    return grid(rows, at=at, perception=6)


def simple_maze(at: tuple[int, int] = (0, 0)) -> WorldModel:
    rows = [
        "#########",
        "#...#...#",
        "#.#.#.#.#",
        "#.#...#.#",
        "#.#####.#",
        "#.....#.#",
        "#########",
    ]
    w = grid(rows, at=at, perception=5)
    w.view.tiles[(7, 5)] = "dirt"
    return w


def hedge_line(at: tuple[int, int] = (0, 2)) -> WorldModel:
    """Goal behind a hedge the agent cannot break yet (M9)."""
    rows = [
        "........",
        "........",
        "........",
        "........",
        "........",
    ]
    w = grid(rows, at=at, perception=8)
    for x in range(8):
        w.view.tiles[(x, 3)] = "bush"
    return w


def water_enclosure(at: tuple[int, int] = (2, 2)) -> WorldModel:
    rows = [
        ".....",
        ".~~~.",
        ".~G~.",
        ".~~~.",
        ".....",
    ]
    w = grid(rows, at=at, perception=5)
    for x in range(5):
        for y in range(5):
            if rows[y][x] == "~":
                w.view.tiles[(x, y)] = "water"
            elif rows[y][x] == "G":
                w.view.tiles[(x, y)] = "dirt"
    return w


def npc_corridor(at: tuple[int, int] = (0, 1)) -> WorldModel:
    rows = [
        "#####",
        "#...#",
        "#####",
    ]
    w = grid(rows, at=at, perception=4)
    w.entities = [Entity("npc", 1, (2, 1))]
    return w


def fog_dead_end(at: tuple[int, int] = (0, 0)) -> WorldModel:
    """Corridor that is open in fog but closes once revealed."""
    w = WorldModel(character_id=1, map_id=1, pos=at, perception=3)
    for x in range(6):
        w.view.tiles[(x, 0)] = "dirt"
    w.terrain_center, w.terrain_map = at, 1
    return w
