"""An op that walks to an entrance is done once he goes through it (A144).

Going through the op's door onto the map it leads to finishes the op, with
no ``no_way_on`` asked from the new map. Going through some other door keeps
the op, and the walk routes back toward the op's map by the way off the map
it landed on (``navigation.walk_toward``), a door not yet gone through or
unexplored ground the stuck ladder has not given up, and asks once none is
left.
"""

from __future__ import annotations

import random
import unittest

from agentrealm_agent.brain import Memory
from agentrealm_adk.directives import PARAM_DEFAULTS
from agentrealm_adk.knowledge_base import KnowledgeBase
from agentrealm_adk.knowledge_maps import record_warp, sync_map_from_view
from agentrealm_adk.navigation import CostGridParams, way_off_map
from agentrealm_adk.navigation import stuck as nav_stuck
from agentrealm_adk.passage import entrance_of, went_through
from agentrealm_agent.plan import Plan, goal_done
from agentrealm_agent.states import PlayContext, dispatch
from agentrealm_adk.world import MapView, Passage, WorldModel
from tests.fixtures.navigation import grids, sim
from tests.test_navigation_stuck import _cross_map_run, run_plan

DOOR = (4, 0)
ENTRANCE = {"op": "travel", "to": "entrance", "x": 4, "y": 0, "map_id": 1}


def _map(rows: list[str]) -> MapView:
    glyph = {".": "dirt", "#": "wall", "D": "framed_door"}
    view = MapView()
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            view.tiles[(x, y)] = glyph[ch]
    return view


def _plan(op: dict, top_since: int = 0, top_map: int = 1) -> Plan:
    plan = Plan([dict(op)], dict(PARAM_DEFAULTS))
    plan.top_since_tick, plan.top_map_id = top_since, top_map
    return plan


class WentThroughTest(unittest.TestCase):
    def _world(self, passage: Passage | None, map_id: int = 2) -> WorldModel:
        w = WorldModel(character_id=1, map_id=map_id, pos=(5, 0))
        w.passage = passage
        return w

    def test_through_the_op_door_onto_its_map_finishes_it(self):
        w = self._world(Passage(1, DOOR, 2, tick=10))
        self.assertTrue(goal_done(ENTRANCE, w, _plan(ENTRANCE, top_since=5)))

    def test_through_another_door_does_not(self):
        w = self._world(Passage(1, (6, 2), 2, tick=10))
        self.assertFalse(goal_done(ENTRANCE, w, _plan(ENTRANCE, top_since=5)))

    def test_the_same_cell_on_another_map_does_not(self):
        w = self._world(Passage(3, DOOR, 2, tick=10))
        self.assertFalse(goal_done(ENTRANCE, w, _plan(ENTRANCE, top_since=5)))

    def test_a_passage_before_the_op_reached_the_top_does_not(self):
        w = self._world(Passage(1, DOOR, 2, tick=4))
        self.assertFalse(goal_done(ENTRANCE, w, _plan(ENTRANCE, top_since=5)))
        self.assertFalse(went_through(ENTRANCE, w, None, 1), "an op not yet on top")

    def test_off_the_map_the_door_led_to_does_not(self):
        w = self._world(Passage(1, DOOR, 2, tick=10), map_id=3)
        self.assertFalse(goal_done(ENTRANCE, w, _plan(ENTRANCE, top_since=5)))

    def test_which_ops_walk_to_a_door(self):
        self.assertEqual(entrance_of(ENTRANCE), (1, DOOR))
        self.assertEqual(entrance_of({"op": "travel", "to": "entrance", "x": 0, "y": 0}), (None, None))
        self.assertEqual(entrance_of({"op": "travel", "to": "point", "x": 4, "y": 0}), (None, DOOR))
        self.assertEqual(entrance_of({"op": "enter_level", "x": 4, "y": 0}), (None, DOOR))
        self.assertIsNone(entrance_of({"op": "travel", "to": "town", "x": 0, "y": 0}))
        self.assertIsNone(entrance_of({"op": "gather_gems", "count": 3}))

    def test_the_nearest_entrance_is_done_through_any_door(self):
        op = {"op": "travel", "to": "entrance", "x": 0, "y": 0}
        w = self._world(Passage(1, (6, 2), 2, tick=10))
        self.assertTrue(goal_done(op, w, _plan(op, top_since=5)))

    def test_an_op_naming_no_map_means_the_map_it_reached_the_top_on(self):
        op = {"op": "travel", "to": "point", "x": 4, "y": 0}
        w = self._world(Passage(1, DOOR, 2, tick=10))
        self.assertTrue(goal_done(op, w, _plan(op, top_since=5, top_map=1)))
        w = self._world(Passage(3, DOOR, 2, tick=10))
        self.assertFalse(goal_done(op, w, _plan(op, top_since=5, top_map=1)), "the same cell on another map")

    def test_a_point_on_a_door_is_done_through_it(self):
        op = {"op": "travel", "to": "point", "x": 4, "y": 0, "map_id": 1}
        w = self._world(Passage(1, DOOR, 2, tick=10))
        self.assertTrue(goal_done(op, w, _plan(op, top_since=5)))


class TravelThroughTheEntranceTest(unittest.TestCase):
    def test_going_through_the_entrance_pops_the_op_and_asks_nothing(self):
        sc = grids.CROSS_MAP_OPEN
        cross = _cross_map_run(sc, DOOR, landing=(5, 0))
        policy = sim.scripted(goals=["goto"])
        plan = Plan([dict(ENTRANCE)], dict(PARAM_DEFAULTS))
        r = run_plan(sc, policy, plan, cross=cross, max_decisions=12)
        self.assertEqual(r.world.map_id, 2, "through the entrance")
        self.assertIsNone(plan.current(), "the travel op is done")
        self.assertIsNone(r.memory.no_way_on)
        self.assertFalse(any("no step toward it" in row["reason"] for row in r.trace), r.trace)

    def test_going_through_another_door_keeps_the_op_and_routes_back(self):
        kb = KnowledgeBase.empty("sandbox")
        sync_map_from_view(kb, 1, _map(["....D.D"]))
        record_warp(kb, 1, (6, 0), "framed_door", 2, (1, 0))
        sync_map_from_view(kb, 2, _map(["D.....", "......"]))
        w = WorldModel(character_id=1, map_id=2, pos=(1, 0), perception=3)
        w.maps[1] = _map(["....D.D"])
        w.maps[2] = _map(["D.....", "......"])
        w.terrain_center, w.terrain_map = w.pos, 2
        w.tick = 20
        w.passage = Passage(1, (6, 0), 2, tick=20)
        plan = _plan(ENTRANCE, top_since=0)
        m = Memory()
        out = dispatch(w, PlayContext(m, sim.scripted(), random.Random(0), knowledge=kb, plan=plan))
        self.assertIs(plan.current()["op"], "travel", "the op is kept")
        self.assertEqual(plan.current(), ENTRANCE)
        self.assertEqual(out.state, "Travel", out.reason)
        self.assertEqual(out.intents, [{"verb": "SetPosition", "x": 0, "y": 0}], out.reason)
        self.assertIsNone(m.no_way_on)


class WayOffMapTest(unittest.TestCase):
    def _world(self) -> WorldModel:
        w = WorldModel(character_id=1, map_id=2, pos=(2, 0), perception=3)
        w.maps[2] = _map(["D....D", "......"])
        w.terrain_center, w.terrain_map = w.pos, 2
        return w

    def test_a_door_not_yet_gone_through_first(self):
        kb = KnowledgeBase.empty("sandbox")
        record_warp(kb, 2, (0, 0), "framed_door", 1, (6, 0))
        self.assertEqual(way_off_map(self._world(), kb, CostGridParams())[-1], (5, 0))

    def test_a_door_the_ladder_gave_up_is_left_out(self):
        kb = KnowledgeBase.empty("sandbox")
        w, m = self._world(), Memory()
        nav_stuck.remember_give_up(m.nav_stuck, nav_stuck.goal_key("level:door", 2, (5, 0)), "level:door", w.tick)
        nav_stuck.remember_give_up(m.nav_stuck, nav_stuck.goal_key("level:door", 2, (0, 0)), "level:door", w.tick)
        path = way_off_map(w, kb, CostGridParams(), m.nav_stuck)
        self.assertNotIn(path[-1] if path else None, {(0, 0), (5, 0)})
        self.assertIn(path[-1], w.frontier(), "unexplored ground instead")

    def test_given_up_frontier_is_left_out_and_then_there_is_no_way(self):
        kb = KnowledgeBase.empty("sandbox")
        record_warp(kb, 2, (0, 0), "framed_door", 1, (6, 0))
        record_warp(kb, 2, (5, 0), "framed_door", 1, (6, 0))
        w, m = self._world(), Memory()
        for p in w.frontier():
            nav_stuck.remember_give_up(m.nav_stuck, nav_stuck.goal_key("explore", 2, p), "explore", w.tick)
        self.assertIsNone(way_off_map(w, kb, CostGridParams(), m.nav_stuck))

    def test_no_door_is_taken_without_a_knowledge_base(self):
        path = way_off_map(self._world(), None, CostGridParams())
        self.assertNotIn(path[-1] if path else None, {(0, 0), (5, 0)})


class LevelThroughTheEntranceTest(unittest.TestCase):
    def test_an_entrance_onto_no_level_finishes_enter_level(self):
        op = {"op": "enter_level", "x": 4, "y": 0}
        w = WorldModel(character_id=1, map_id=2, pos=(5, 0), perception=3)
        w.maps[2] = _map(["......"])
        w.terrain_center, w.terrain_map = w.pos, 2
        w.tick = 20
        w.passage = Passage(1, DOOR, 2, tick=20)
        plan = _plan(op, top_since=0)
        m = Memory()
        out = dispatch(w, PlayContext(m, sim.scripted(), random.Random(0), plan=plan))
        self.assertIsNone(plan.current(), out.reason)
        self.assertIsNone(m.no_way_on)


if __name__ == "__main__":
    unittest.main()
