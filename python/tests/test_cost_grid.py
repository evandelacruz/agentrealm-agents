"""Cost-grid navigation (A12) and two-level search (A13)."""

import random
import unittest
from unittest import mock

from agentrealm_agent.brain import Memory, decide
from agentrealm_agent.config import Policy
from agentrealm_agent.directives import PARAM_DEFAULTS
from agentrealm_agent.navigation import (
    CostGridParams,
    NavSearchState,
    cost_path,
    known_prefix,
    learn_step_rejection,
    macro_cell,
    nearest_target,
)
from agentrealm_agent.navigation import planner
from agentrealm_agent.navigation.planner import COSTLY_STEP, _coarse_search, _Grid, _MacroCosts
from agentrealm_agent.plan import Plan
from agentrealm_agent.world import Entity, WorldModel
from agentrealm_agent.zone_discovery import apply_zone


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
        self.assertEqual(_Grid(w, {(2, 1)}, CostGridParams()).cost((1, 0)), 1 + 4)
        p = cost_path(w, (2, 1), CostGridParams())
        self.assertIsNotNone(p)
        self.assertNotIn((1, 0), p)

    def test_hazard_with_unnamed_damage_costs_a_costly_step(self):
        w = grid(["~"])
        self.assertEqual(_Grid(w, {(5, 5)}, CostGridParams()).cost((0, 0)), 1 + COSTLY_STEP)

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
        p = cost_path(w, (4, 1), CostGridParams())
        self.assertIsNotNone(p)
        self.assertNotIn((2, 1), p)

    def test_occupant_is_high_cost_not_impassable(self):
        w = grid([".."])
        w.entities = [Entity("character", 1, (1, 0))]
        self.assertEqual(cost_path(w, (1, 0), CostGridParams()), [(1, 0)])

    def test_break_nominated_without_cost_stays_impassable(self):
        w = grid(["..."])
        params = CostGridParams(break_nominated={(1, 0)})
        self.assertIsNone(_Grid(w, {(1, 0)}, params).cost((1, 0)))

    def test_break_cost_makes_a_nominated_cell_passable(self):
        w = grid(["..."])
        w.view.tiles[(1, 0)] = "bush"
        params = CostGridParams(break_nominated={(1, 0)}, break_costs={(1, 0): 11})
        self.assertEqual(_Grid(w, {(1, 0)}, params).cost((1, 0)), 11)

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


def strip(length: int, at=(0, 0), perception=3) -> WorldModel:
    """A known one-row corridor of dirt; everything else is fog."""
    return grid(["." * length], at=at, perception=perception)


def corridor_of(w: WorldModel, goal, nav: NavSearchState, params=None, budget=10**6):
    g = _Grid(w, {goal}, params or CostGridParams())
    return _coarse_search(g, _MacroCosts(g), nav, budget)


class TwoLevelSearchTest(unittest.TestCase):
    def in_perception(self, w: WorldModel, path) -> bool:
        x0, y0, width, height = w.perception_rect()
        return all(x0 <= x < x0 + width and y0 <= y < y0 + height for x, y in path)

    def test_far_goal_plans_inside_perception_when_direct_search_is_budgeted(self):
        w = strip(40)
        nav = NavSearchState(goal=(30, 0))
        p = cost_path(w, (30, 0), CostGridParams(), nav=nav, fine_budget=5)
        self.assertEqual(p, [(1, 0), (2, 0), (3, 0)])
        self.assertTrue(self.in_perception(w, p))
        self.assertIn(macro_cell((0, 0)), nav.closed)

    def test_unreachable_goal_still_returns_none(self):
        w = grid(["#####", "#...#", "#...#", "#...#", "#####"], at=(10, 10), perception=5)
        w.view.tiles[(10, 10)] = "dirt"
        self.assertIsNone(cost_path(w, (2, 2), CostGridParams()))

    def test_in_perception_goal_past_the_budget_still_gets_a_path(self):
        # A12 regression: a reachable goal in sight must never read as unreachable.
        w = strip(10, perception=5)
        self.assertEqual(cost_path(w, (5, 0), CostGridParams(), fine_budget=2), [(1, 0)])
        self.assertEqual(cost_path(w, (5, 0), CostGridParams(), fine_budget=6), [(1, 0), (2, 0), (3, 0), (4, 0), (5, 0)])

    def test_unfinished_corridor_still_steps_toward_the_goal(self):
        w = strip(100)
        nav = NavSearchState(goal=(90, 0))
        p = cost_path(w, (90, 0), CostGridParams(), nav=nav, coarse_budget=1, fine_budget=5)
        self.assertEqual(p, [(1, 0), (2, 0), (3, 0)])
        self.assertNotIn(macro_cell((0, 0)), nav.closed)

    def test_coarse_search_resumes_across_replans(self):
        # Two cache tiles a replan: it finishes in as many calls as a single
        # search needs expansions, halved, only if each call carries on.
        w = strip(100)
        fresh = NavSearchState(goal=(90, 0))
        corridor_of(w, (90, 0), fresh)
        needed = len(fresh.closed)
        nav = NavSearchState(goal=(90, 0))
        calls = 0
        while (0, 0) not in nav.closed:
            calls += 1
            self.assertLessEqual(calls, (needed + 1) // 2)
            cost_path(w, (90, 0), CostGridParams(), nav=nav, coarse_budget=2, fine_budget=5)
        self.assertGreater(calls, 1)

    def test_corridor_is_read_from_where_we_stand_after_moving(self):
        w = strip(100)
        nav = NavSearchState(goal=(90, 0))
        self.assertEqual(corridor_of(w, (90, 0), nav), [(0, 0), (1, 0), (2, 0), (3, 0), (4, 0), (5, 0)])
        w.pos = (40, 0)
        self.assertEqual(corridor_of(w, (90, 0), nav, budget=0), [(2, 0), (3, 0), (4, 0), (5, 0)])

    def test_corridor_is_redone_when_a_tile_on_it_is_walled_off(self):
        w = strip(100)
        nav = NavSearchState(goal=(90, 0))
        self.assertIn((2, 0), corridor_of(w, (90, 0), nav))
        for x in range(32, 48):
            for y in range(-16, 16):
                w.view.tiles[(x, y)] = "wall"
        c = corridor_of(w, (90, 0), nav)
        self.assertNotIn((2, 0), c)
        self.assertEqual((c[0], c[-1]), ((0, 0), (5, 0)))

    def test_corridor_prices_avoided_cells(self):
        w = strip(100)
        avoid = {(x, y) for x in range(32, 48) for y in range(0, 16)}
        c = corridor_of(w, (90, 0), NavSearchState(goal=(90, 0)), CostGridParams(avoid=avoid))
        self.assertNotIn((2, 0), c)

    def test_wall_forces_a_detour_through_the_gap(self):
        # A wall at x 32..47 from far above down to y 47; the short way is below it.
        w = grid(["." * 16], at=(8, 8), perception=6)
        for x in range(32, 48):
            for y in range(-80, 48):
                w.view.tiles[(x, y)] = "wall"
        w.view.tiles[(88, 8)] = "dirt"
        nav = NavSearchState(goal=(88, 8))
        p = cost_path(w, (88, 8), CostGridParams(), nav=nav, fine_budget=200)
        c = corridor_of(w, (88, 8), nav, budget=0)
        self.assertIn((2, 3), c)
        self.assertTrue(self.in_perception(w, p))
        self.assertGreater(p[-1][1], 8)  # heads down toward the gap

    def test_goal_change_mid_search_starts_over_from_the_new_goal(self):
        w = strip(100)
        nav = NavSearchState(goal=(90, 0))
        cost_path(w, (90, 0), CostGridParams(), nav=nav, coarse_budget=1, fine_budget=5)
        cost_path(w, (60, 0), CostGridParams(), nav=nav, coarse_budget=1, fine_budget=5)
        self.assertEqual(nav.goal, (60, 0))
        self.assertEqual(nav.cost[macro_cell((60, 0))], 0)
        self.assertNotIn(macro_cell((90, 0)), nav.cost)

    def test_nearest_target_ranks_by_cost_not_length(self):
        # (3, 1) is two steps away but only across unnamed lava; (5, 3) is five clear steps.
        w = grid(["#######", "#.~.###", "#.#####", "#.....#", "#######"], at=(1, 1))
        found = nearest_target(w, {(3, 1), (5, 3)}, CostGridParams())
        self.assertEqual(found, ((5, 3), [(1, 2), (2, 3), (3, 3), (4, 3), (5, 3)]))

    def test_nearest_target_path_ends_on_a_far_target(self):
        w = strip(60, perception=2)
        found = nearest_target(w, {(50, 0)}, CostGridParams())
        self.assertEqual(found[0], (50, 0))
        self.assertEqual(found[1][-1], (50, 0))


def decide_builtin(w: WorldModel, m: Memory, policy: Policy):
    """One decision under the built-in plan for ``policy`` (goto → a travel point op)."""
    return decide(w, m, policy, random.Random(0), plan=Plan.from_policy(policy, dict(PARAM_DEFAULTS)))


GOTO = "travel:point"  # the walk label of a goto's travel op


class TwoLevelBrainTest(unittest.TestCase):
    def test_corridor_search_resumes_across_decides(self):
        w = strip(100)
        m = Memory()
        policy = Policy(kind="scripted", goals=["goto"], goto=[90, 0])
        with mock.patch.object(planner, "COARSE_NODE_BUDGET", 2), mock.patch.object(planner, "FINE_NODE_BUDGET", 5):
            d = decide_builtin(w, m, policy)
            self.assertEqual((d.intent["x"], d.intent["y"]), (1, 0))
            nav = m.corridors[GOTO]
            self.assertEqual(len(nav.closed), 2)
            w.pos, m.path = m.path[-1], []  # walked the plan out; replan
            decide_builtin(w, m, policy)
            self.assertIs(m.corridors[GOTO], nav)
            self.assertEqual(len(nav.closed), 4)

    def test_chest_and_goto_keep_separate_searches(self):
        w = strip(100)
        w.death_chest = (1, (90, 0), 7)
        apply_zone(w, 1, 89, 0, {"safe": True, "brightness": 1})
        m = Memory()
        policy = Policy(kind="scripted", goals=["goto"], goto=[0, 60], pickup=True)
        with mock.patch.object(planner, "FINE_NODE_BUDGET", 5):
            decide_builtin(w, m, policy)
            chest = m.corridors["chest"]
            m.path, m.goal = [], ""
            decide_builtin(w, m, policy)
        self.assertIs(m.corridors["chest"], chest)
        self.assertEqual(chest.goal, (89, 0))

    def test_rejected_step_drops_the_corridor_searches(self):
        w = strip(100)
        m = Memory()
        policy = Policy(kind="scripted", goals=["goto"], goto=[90, 0])
        with mock.patch.object(planner, "FINE_NODE_BUDGET", 5):
            decide_builtin(w, m, policy)
        self.assertIn(GOTO, m.corridors)
        learn_step_rejection(m, w, None, (1, 0), "not_traversable", w.tick)
        self.assertEqual(m.corridors, {})

    def test_corridor_search_starts_over_on_another_map(self):
        w = strip(100)
        m = Memory()
        policy = Policy(kind="scripted", goals=["goto"], goto=[90, 0])
        with mock.patch.object(planner, "FINE_NODE_BUDGET", 5):
            decide_builtin(w, m, policy)
            first = m.corridors[GOTO]
            w2 = WorldModel(character_id=1, map_id=2, pos=(0, 0), perception=3)
            w2.view.tiles.update(w.view.tiles)
            w2.terrain_center, w2.terrain_map = (0, 0), 2
            m.path, m.goal = [], ""
            decide_builtin(w2, m, Policy(kind="scripted", goals=["goto"], goto=[90, 0], goto_map=2))
        self.assertIsNot(m.corridors[GOTO], first)
        self.assertEqual(m.corridors[GOTO].map_id, 2)


class KnownPrefixBrainTest(unittest.TestCase):
    def test_goal_starting_in_fog_falls_through_to_the_safe_default(self):
        # doors' travel path starts on an unseen tile; the safe default explores instead.
        w = grid([".."])
        w.view.tiles[(0, 5)] = "framed_door"
        policy = Policy(kind="scripted", goals=["doors", "explore"])
        self.assertNotIn(cost_path(w, (0, 5), CostGridParams(allow_goal_door=True))[0], w.view.tiles)
        d = decide_builtin(w, Memory(), policy)
        self.assertEqual((d.intent["x"], d.intent["y"]), (1, 0))
        self.assertTrue(d.reason.startswith("explore"), d.reason)

    def test_no_goal_with_a_seen_first_step_sends_nothing(self):
        w = grid(["."])
        d = decide_builtin(w, Memory(), Policy(kind="scripted", goals=["goto"], goto=[0, 5]))
        self.assertIsNone(d.intent)


if __name__ == "__main__":
    unittest.main()
