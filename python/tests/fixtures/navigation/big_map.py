"""A large, seeded known map for the navigation speed guard (A23 Run 2).

Shaped like the knowledge base a long run builds up: about 33k known cells
round the character, ragged edges into fog (a few hundred frontier cells),
scattered walls and water, walled rooms whose half-seen insides are frontier
nobody can reach, and a crowd of NPCs. Fixture names only; no live character.
"""

from __future__ import annotations

import math
import random

from agentrealm_agent.world import Entity, WorldModel

RADIUS = 100  # known ground is roughly the disc of this radius round START
START = (400, 400)


def big_world(seed: int = 7, npcs: int = 40) -> WorldModel:
    rng = random.Random(seed)
    w = WorldModel(character_id=1, map_id=1, pos=START, perception=8)
    tiles = w.view.tiles
    cx, cy = START
    # A ragged edge: known out to RADIUS, give or take, varying round the disc.
    wobble = [rng.randint(-8, 4) for _ in range(72)]
    for x in range(cx - RADIUS - 10, cx + RADIUS + 11):
        for y in range(cy - RADIUS - 10, cy + RADIUS + 11):
            dx, dy = x - cx, y - cy
            if dx * dx + dy * dy > (RADIUS + wobble[int(math.degrees(math.atan2(dy, dx)) + 180) // 5 % 72]) ** 2:
                continue
            r = rng.random()
            tiles[(x, y)] = "water" if r < 0.04 else "wall" if r < 0.08 else ("grass" if r < 0.6 else "dirt")
    # Walled rooms near the start, their far halves still fog: frontier
    # cells inside that no path reaches.
    for i in range(24):
        rx = cx + rng.randint(-60, 60)
        ry = cy + rng.randint(-60, 60)
        if abs(rx - cx) < 6 and abs(ry - cy) < 6:
            continue
        for x in range(rx - 3, rx + 4):
            for y in range(ry - 3, ry + 4):
                edge = x in (rx - 3, rx + 3) or y in (ry - 3, ry + 3)
                if edge:
                    tiles[(x, y)] = "wall"
                elif y > ry:
                    tiles.pop((x, y), None)
                else:
                    tiles[(x, y)] = "dirt"
    tiles[START] = "grass"
    for i in range(npcs):
        p = (cx + rng.randint(-RADIUS // 2, RADIUS // 2), cy + rng.randint(-RADIUS // 2, RADIUS // 2))
        w.entities.append(Entity(kind="npc", id=1000 + i, pos=p, code="fixture_npc"))
    w.terrain_center, w.terrain_map = START, 1
    return w
