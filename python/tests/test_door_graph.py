"""Door graph and cross-map routing (A26)."""

import tempfile
import unittest
from unittest import mock

from agentrealm_agent import knowledge_base as kb_mod
from agentrealm_agent.brain import Memory, decide
from agentrealm_agent.config import Policy
from agentrealm_agent.knowledge_maps import record_warp, sync_map_from_view, view_from_kb
from agentrealm_agent.navigation.door_graph import route_first_leg
from agentrealm_agent.navigation.planner import CostGridParams
from agentrealm_agent.world import MapView, WorldModel
from pathlib import Path


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


class KnowledgeMapsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.object(kb_mod, "WORLDS_DIR", Path(self.tmp.name) / "worlds")
        patch.start()
        self.addCleanup(patch.stop)
        self.kb = kb_mod.KnowledgeBase.empty("sandbox")

    def test_record_warp_and_reload_view(self) -> None:
        w = _grid(["D.."], at=(0, 0), map_id=10)
        sync_map_from_view(self.kb, 10, w.view)
        record_warp(self.kb, 10, (0, 0), "framed_door", 20, (5, 5))
        again = view_from_kb(self.kb, 10)
        self.assertEqual(again.tiles[(0, 0)], "framed_door")
        doors = self.kb.maps["10"]["doors"]
        self.assertEqual(doors[0]["to_map_id"], 20)


class DoorGraphRoutingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.object(kb_mod, "WORLDS_DIR", Path(self.tmp.name) / "worlds")
        patch.start()
        self.addCleanup(patch.stop)
        self.kb = kb_mod.KnowledgeBase.empty("sandbox")

    def test_route_to_other_map_uses_door_warp(self) -> None:
        # Map 1: start at (0,0), door at (2,0)
        w = _grid(["...", "..."], at=(0, 0), map_id=1)
        w.maps[1].tiles[(2, 0)] = "framed_door"
        sync_map_from_view(self.kb, 1, w.maps[1])
        record_warp(self.kb, 1, (2, 0), "framed_door", 2, (0, 0))
        # Map 2: landing to goal
        view2 = MapView()
        for x in range(4):
            view2.tiles[(x, 0)] = "dirt"
        self.kb.maps["2"] = {"terrain": {f"{x},0": "dirt" for x in range(4)}, "doors": []}

        path = route_first_leg(w, self.kb, 2, (3, 0), CostGridParams(allow_goal_door=True))
        self.assertEqual(path[-1], (2, 0))

    def test_goto_map_goal_uses_cross_map_route(self) -> None:
        w = _grid(["...", "..."], at=(0, 0), map_id=1)
        w.maps[1].tiles[(2, 0)] = "framed_door"
        sync_map_from_view(self.kb, 1, w.maps[1])
        record_warp(self.kb, 1, (2, 0), "framed_door", 2, (0, 0))
        self.kb.maps["2"] = {"terrain": {f"{x},0": "dirt" for x in range(4)}, "doors": []}

        import random

        d = decide(
            w,
            Memory(),
            Policy(kind="scripted", goals=["goto"], goto=(3, 0), goto_map=2, pickup=False),
            random.Random(0),
            knowledge=self.kb,
        )
        self.assertIsNotNone(d.intent)
        self.assertEqual((d.intent["x"], d.intent["y"]), (1, 0))


if __name__ == "__main__":
    unittest.main()
