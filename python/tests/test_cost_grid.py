"""Cost-grid navigation (A12)."""

import random
import unittest

from agentrealm_agent.brain import Memory, decide
from agentrealm_agent.config import Policy
from agentrealm_agent.navigation import CostGridParams, cost_path, known_prefix, nearest_target
from agentrealm_agent.navigation.planner import COSTLY_STEP, _Grid
from agentrealm_agent.world import Entity, WorldModel


def grid(rows: list[str], at=(0, 0), perception=5) -> WorldModel:
    glyph = {".": "dirt", "#": "wall", "~": "lava", "D": "framed_door", "?": None}
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
        self.assertEqual(_Grid(w, (2, 1), CostGridParams()).cost((1, 0)), 1 + 4)
        p = cost_path(w, (2, 1), CostGridParams())
        self.assertIsNotNone(p)
        self.assertNotIn((1, 0), p)

    def test_hazard_with_unnamed_damage_costs_a_costly_step(self):
        w = grid(["~"])
        self.assertEqual(_Grid(w, (5, 5), CostGridParams()).cost((0, 0)), 1 + COSTLY_STEP)

    def test_zero_occupy_damage_is_kept(self):
        w = WorldModel(character_id=1, map_id=1, pos=(0, 0))
        w.apply_terrain({"map_id": 1, "x0": 0, "y0": 0, "width": 1, "height": 1,
                         "legend": {"l": {"block_type": "lava", "occupy_damage": 0}}, "rows": ["l"]})
        self.assertEqual(w.view.occupy_damage((0, 0)), 0)

    def test_costly_tiles_are_crossed_as_few_as_possible(self):
        # Escape off hazards: costly tiles are passable but avoided.
        w = grid(["...", "...", "..."], at=(0, 1))
        p = cost_path(w, (2, 1), CostGridParams(costly={(1, 0), (1, 1)}))
        self.assertEqual(p, [(1, 2), (2, 1)])

    def test_hostile_danger_biases_away(self):
        w = grid([".....", ".....", "....."], at=(0, 1))
        w.entities = [Entity("npc", 1, (2, 1))]
        p = cost_path(w, (4, 1), CostGridParams(hostile_kinds=frozenset({"npc"})))
        self.assertIsNotNone(p)
        self.assertNotIn((2, 1), p)

    def test_occupant_is_high_cost_not_impassable(self):
        w = grid([".."])
        w.entities = [Entity("character", 1, (1, 0))]
        self.assertEqual(cost_path(w, (1, 0), CostGridParams()), [(1, 0)])

    def test_break_nominated_stays_impassable(self):
        w = grid(["..."])
        params = CostGridParams(break_nominated={(1, 0)})
        self.assertIsNone(_Grid(w, (1, 0), params).cost((1, 0)))

    def test_doors_are_entered_only_as_the_goal(self):
        # A door warps, so a path never crosses one on the way somewhere else.
        w = grid(["#######", "#..D..#", "#######"], at=(1, 1))
        self.assertIsNone(cost_path(w, (5, 1), CostGridParams(allow_goal_door=True)))
        self.assertIsNone(cost_path(w, (3, 1), CostGridParams()))
        self.assertEqual(cost_path(w, (3, 1), CostGridParams(allow_goal_door=True)), [(2, 1), (3, 1)])

    def test_avoid_on_the_goal_is_unreachable(self):
        w = grid(["..."])
        self.assertIsNone(cost_path(w, (2, 0), CostGridParams(avoid={(2, 0)})))

    def test_unreachable_goal_terminates(self):
        # Goal walled in, everything else fog: the search is boxed, not endless.
        w = grid(["#####", "#...#", "#...#", "#...#", "#####"], at=(10, 10))
        w.view.tiles[(10, 10)] = "dirt"
        self.assertIsNone(cost_path(w, (2, 2), CostGridParams()))

    def test_nearest_compares_path_costs(self):
        # The nearer target sits behind a hostile; the farther one is cheaper.
        w = grid(["........."], at=(4, 0))
        w.entities = [Entity("npc", 1, (2, 0))]
        found = nearest_target(w, {(0, 0), (8, 0)}, CostGridParams())
        self.assertEqual(found[0], (8, 0))

    def test_known_prefix_stops_at_fog(self):
        w = grid([".."])
        self.assertEqual(known_prefix([(1, 0), (2, 0), (3, 0)], w.view), [(1, 0)])


class KnownPrefixBrainTest(unittest.TestCase):
    def test_goal_starting_in_fog_falls_through_to_the_next_goal(self):
        # goto's cheapest path starts on an unseen tile; explore takes the move.
        w = grid([".."])
        policy = Policy(kind="scripted", goals=["goto", "explore"], goto=[0, 5])
        self.assertNotIn(cost_path(w, (0, 5))[0], w.view.tiles)
        d = decide(w, Memory(), policy, random.Random(0))
        self.assertEqual((d.intent["x"], d.intent["y"]), (1, 0))

    def test_no_goal_with_a_seen_first_step_sends_nothing(self):
        w = grid(["."])
        d = decide(w, Memory(), Policy(kind="scripted", goals=["goto"], goto=[0, 5]), random.Random(0))
        self.assertIsNone(d.intent)


if __name__ == "__main__":
    unittest.main()
