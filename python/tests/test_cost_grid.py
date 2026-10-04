"""Cost-grid navigation (A12)."""

import unittest

from agentrealm_agent.navigation import CostGridParams, cost_path, known_prefix
from agentrealm_agent.world import Entity, WorldModel


def grid(rows: list[str], at=(0, 0), perception=5) -> WorldModel:
    glyph = {".": "dirt", "#": "wall", "~": "lava", "?": None}
    w = WorldModel(character_id=1, map_id=1, pos=at, perception=perception)
    for y, row in enumerate(rows):
        for x, g in enumerate(row):
            if g == "?":
                continue
            w.view.tiles[(x, y)] = glyph[g]
    w.terrain_center, w.terrain_map = at, 1
    return w


class CostGridTest(unittest.TestCase):
    def test_fog_is_cheaper_than_a_long_detour(self):
        w = grid(["....", "...."])
        p = cost_path(w, (3, 0), CostGridParams())
        self.assertIsNotNone(p)
        self.assertIn((1, 0), p)

    def test_hazard_cost_uses_occupy_damage(self):
        w = grid([".~.", "...", "..."], at=(0, 1))
        w.view.damage[(1, 0)] = 4
        p = cost_path(w, (2, 1), CostGridParams())
        self.assertIsNotNone(p)
        # Low-damage lava still costs more than going around (y=2).
        self.assertNotIn((1, 0), p)

    def test_hostile_danger_biases_away(self):
        w = grid([".....", ".....", "....."], at=(0, 1))
        w.entities = [Entity("npc", 1, (2, 1))]
        params = CostGridParams(hostile_kinds=frozenset({"npc"}))
        p = cost_path(w, (4, 1), params)
        self.assertIsNotNone(p)
        self.assertNotIn((2, 1), p)

    def test_occupant_is_high_cost_not_impassable(self):
        w = grid(["."], at=(0, 0))
        w.view.tiles[(1, 0)] = "dirt"
        w.entities = [Entity("npc", 1, (1, 0))]
        self.assertEqual(cost_path(w, (1, 0), CostGridParams()), [(1, 0)])

    def test_break_nominated_stays_impassable(self):
        from agentrealm_agent.navigation.planner import step_cost

        w = grid(["..."], at=(0, 0))
        params = CostGridParams(break_nominated={(1, 0)})
        self.assertIsNone(step_cost(w, (1, 0), params, goal=(1, 0), hostiles=[]))

    def test_known_prefix_stops_at_fog(self):
        w = grid([".."], at=(0, 0))
        full = [(1, 0), (2, 0), (3, 0)]
        self.assertEqual(known_prefix(full, w.view), [(1, 0)])


class PathCompatTest(unittest.TestCase):
    def test_goto_through_fog(self):
        w = grid([".."])
        p = w.path((4, 0))
        self.assertIsNotNone(p)


if __name__ == "__main__":
    unittest.main()
