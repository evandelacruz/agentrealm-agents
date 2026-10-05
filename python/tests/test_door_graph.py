"""Door graph and cross-map routing (A26)."""

import random
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agentrealm_agent import config
from agentrealm_agent import knowledge_base as kb_mod
from agentrealm_agent.brain import Memory, decide
from agentrealm_agent.config import CharacterConfig, Policy
from agentrealm_agent.knowledge_maps import record_warp, sync_map_from_view, view_from_kb
from agentrealm_agent.navigation import CostGridParams, doors_goal_path, route_first_leg
from agentrealm_agent.runner import Runner
from agentrealm_agent.world import MapView, WorldModel


def _grid(rows: list[str], at=(0, 0), map_id=1, perception=25) -> WorldModel:
    glyph = {".": "dirt", "#": "wall", "D": "framed_door"}
    w = WorldModel(character_id=1, map_id=map_id, pos=at, perception=perception)
    view = MapView()
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            if ch != " ":
                view.tiles[(x, y)] = glyph.get(ch, "wall")
    w.maps[map_id] = view
    return w


def _store_map(kb: kb_mod.KnowledgeBase, map_id: int, rows: list[str]) -> None:
    sync_map_from_view(kb, map_id, _grid(rows, map_id=map_id).view)


PARAMS = CostGridParams(allow_goal_door=True)


class KbTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for target, attr, value in (
            (kb_mod, "WORLDS_DIR", Path(tmp.name) / "worlds"),
            (config, "STATE_DIR", Path(tmp.name)),
        ):
            patch = mock.patch.object(target, attr, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.kb = kb_mod.KnowledgeBase.empty("sandbox")


class KnowledgeMapsTest(KbTestCase):
    def test_record_warp_and_reload_view(self) -> None:
        w = _grid(["D.."], at=(1, 0), map_id=10)
        sync_map_from_view(self.kb, 10, w.view)
        record_warp(self.kb, 10, (0, 0), "framed_door", 20, (5, 5))
        self.assertEqual(view_from_kb(self.kb, 10).tiles[(0, 0)], "framed_door")
        self.assertEqual(
            self.kb.maps["10"]["doors"],
            [{"x": 0, "y": 0, "block_type": "framed_door", "to_map_id": 20, "to_x": 5, "to_y": 5}],
        )


class DoorGraphRoutingTest(KbTestCase):
    def test_route_to_other_map_walks_to_the_known_door(self) -> None:
        w = _grid(["..D", "..."], map_id=1)
        sync_map_from_view(self.kb, 1, w.view)
        record_warp(self.kb, 1, (2, 0), "framed_door", 2, (0, 0))
        _store_map(self.kb, 2, ["...."])
        self.assertEqual(route_first_leg(w, self.kb, 2, (3, 0), PARAMS), [(1, 0), (2, 0)])

    def test_multi_hop_route_through_two_doors(self) -> None:
        # 1 → 2 by (2,0), 2 → 3 by (3,0); the goal is on map 3.
        w = _grid(["..D"], map_id=1)
        record_warp(self.kb, 1, (2, 0), "framed_door", 2, (0, 0))
        _store_map(self.kb, 2, ["...D"])
        record_warp(self.kb, 2, (3, 0), "framed_door", 3, (0, 0))
        _store_map(self.kb, 3, ["..."])
        self.assertEqual(route_first_leg(w, self.kb, 3, (2, 0), PARAMS), [(1, 0), (2, 0)])

    def test_cheapest_door_wins(self) -> None:
        # Both doors lead to map 2; the right one lands next to the goal.
        w = _grid(["D...D"], at=(2, 0), map_id=1)
        record_warp(self.kb, 1, (0, 0), "framed_door", 2, (0, 0))
        record_warp(self.kb, 1, (4, 0), "framed_door", 2, (9, 0))
        _store_map(self.kb, 2, [".........."])
        self.assertEqual(route_first_leg(w, self.kb, 2, (8, 0), PARAMS), [(3, 0), (4, 0)])

    def test_door_with_unknown_warp_is_a_dead_end(self) -> None:
        w = _grid(["..D"], map_id=1)
        sync_map_from_view(self.kb, 1, w.view)
        _store_map(self.kb, 2, ["...."])
        self.assertIsNone(route_first_leg(w, self.kb, 2, (3, 0), PARAMS))

    def test_unreachable_destination_has_no_route(self) -> None:
        w = _grid(["..D"], map_id=1)
        record_warp(self.kb, 1, (2, 0), "framed_door", 2, (0, 0))
        _store_map(self.kb, 2, ["..#"])
        self.assertIsNone(route_first_leg(w, self.kb, 2, (2, 0), PARAMS))

    def test_standing_on_a_door_walks_off_it(self) -> None:
        # The door under us did not warp us, so the route starts on foot.
        w = _grid(["D.D"], at=(0, 0), map_id=1)
        record_warp(self.kb, 1, (0, 0), "framed_door", 2, (0, 0))
        record_warp(self.kb, 1, (2, 0), "framed_door", 2, (3, 0))
        _store_map(self.kb, 2, ["...."])
        self.assertEqual(route_first_leg(w, self.kb, 2, (3, 0), PARAMS), [(1, 0), (2, 0)])

    def test_without_kb_only_the_current_map_is_reachable(self) -> None:
        w = _grid(["..D"], map_id=1)
        self.assertEqual(route_first_leg(w, None, 1, (1, 0), PARAMS), [(1, 0)])
        self.assertIsNone(route_first_leg(w, None, 2, (0, 0), PARAMS))

    def test_doors_goal_prefers_an_unvisited_door(self) -> None:
        w = _grid(["D..D"], at=(1, 0), map_id=1)
        record_warp(self.kb, 1, (0, 0), "framed_door", 2, (0, 0))
        self.assertEqual(doors_goal_path(w, self.kb, PARAMS), [(2, 0), (3, 0)])
        record_warp(self.kb, 1, (3, 0), "framed_door", 2, (1, 0))
        self.assertEqual(doors_goal_path(w, self.kb, PARAMS), [(0, 0)], "all visited: the nearest")

    def test_goto_map_goal_uses_cross_map_route(self) -> None:
        w = _grid(["..D", "..."], map_id=1)
        record_warp(self.kb, 1, (2, 0), "framed_door", 2, (0, 0))
        _store_map(self.kb, 2, ["...."])
        d = decide(
            w,
            Memory(),
            Policy(goals=["goto"], goto=(3, 0), goto_map=2, pickup=False),
            random.Random(0),
            knowledge=self.kb,
        )
        self.assertEqual((d.intent["x"], d.intent["y"]), (1, 0))

    def test_goto_map_rejected_without_goto(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "c.toml"
            p.write_text('world = "sandbox"\n[policy]\ngoto_map = 2\n')
            with self.assertRaisesRegex(config.ConfigError, "goto_map"):
                config.load(p)


class FakeReads:
    def __init__(self, position: dict | None = None, terrain: dict | None = None):
        self.position_reply, self.terrain_reply = position, terrain

    def position(self, cid):
        return self.position_reply

    def terrain(self, cid, map_id, x0, y0, width, height):
        return self.terrain_reply


class RunnerWarpRecordingTest(KbTestCase):
    def runner(self, client) -> Runner:
        cfg = CharacterConfig("T", "sandbox", Policy(pickup=False), Path("t.toml"))
        r = Runner(cfg, client, 1, threading.Event(), out=lambda _: None, knowledge=self.kb)
        self.addCleanup(r.trace.close)
        r.world = _grid(["..D"], map_id=1)
        r.mem = Memory(need_self=False, need_position=False)
        return r

    def door_step(self, r: Runner) -> None:
        r.mem.pending_intents, r.mem.pending_queue = [{"verb": "Step", "direction": "right"}], "q1"
        r.world.pos = (1, 0)
        self.assertFalse(r.on_result({"tick": 5, "outcome": "applied"}, 0))
        self.assertEqual(r.mem.warp_from, (1, (2, 0), "framed_door"))

    def test_position_read_after_a_door_step_records_the_warp(self) -> None:
        r = self.runner(FakeReads(position={"map_id": 2, "x": 4, "y": 6}))
        self.door_step(r)
        r.step("position")
        self.assertIsNone(r.mem.warp_from)
        door = self.kb.maps["1"]["doors"][0]
        self.assertEqual((door["x"], door["y"], door["to_map_id"], door["to_x"], door["to_y"]), (2, 0, 2, 4, 6))

    def test_read_still_on_the_door_records_no_self_loop(self) -> None:
        r = self.runner(FakeReads(position={"map_id": 1, "x": 2, "y": 0}))
        self.door_step(r)
        r.step("position")
        self.assertIsNone(r.mem.warp_from)
        self.assertNotIn("1", self.kb.maps)

    def test_position_read_without_a_door_step_records_nothing(self) -> None:
        r = self.runner(FakeReads(position={"map_id": 2, "x": 0, "y": 0}))
        r.world.pos = (2, 0)  # on a door, but no Step onto it was seen
        r.step("position")
        self.assertNotIn("1", self.kb.maps)

    def test_rejection_clears_the_pending_warp(self) -> None:
        r = self.runner(FakeReads(position={"map_id": 2, "x": 4, "y": 6}))
        self.door_step(r)
        r.mem.pending_intents, r.mem.pending_queue = [{"verb": "Step", "direction": "left"}], "q2"
        rejection = {"category": "occupied", "code": "block_occupied"}
        self.assertTrue(r.on_result({"tick": 6, "outcome": "rejected", "rejection": rejection}, 0))
        self.assertIsNone(r.mem.warp_from)
        r.step("position")
        self.assertNotIn("1", self.kb.maps)

    def test_terrain_read_merges_its_window(self) -> None:
        terrain = {"map_id": 1, "x0": 0, "y0": 0, "width": 2, "height": 1, "tick": 3,
                   "legend": {"d": {"block_type": "dirt"}, "D": {"block_type": "framed_door"}},
                   "rows": ["dD"]}
        r = self.runner(FakeReads(terrain=terrain))
        r.world.perception = 0
        r.world.maps[1].tiles[(9, 9)] = "dirt"  # known, but outside this read
        r.step("terrain")
        self.assertEqual(self.kb.maps["1"]["terrain"], {"0,0": "dirt", "1,0": "framed_door"})
        self.assertEqual(self.kb.maps["1"]["doors"], [{"x": 1, "y": 0, "block_type": "framed_door"}])


if __name__ == "__main__":
    unittest.main()
