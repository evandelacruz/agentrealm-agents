"""A37: Level state walks toward unexplored doors inside a level."""

import random
import unittest

from agentrealm_agent.config import Policy
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.knowledge_maps import record_warp, sync_map_from_view
from agentrealm_agent.level import inside_level
from agentrealm_agent.memory import Memory
from agentrealm_agent.navigation import CostGridParams
from agentrealm_agent.states import PlayContext, dispatch
from agentrealm_agent.travel import sync_town
from agentrealm_agent.world import WorldModel


def grid(rows: list[str], at=(0, 0), map_id=1, perception=5) -> WorldModel:
    glyph = {".": "dirt", "#": "wall", "D": "framed_door"}
    w = WorldModel(character_id=1, map_id=map_id, pos=at, perception=perception)
    for y, row in enumerate(rows):
        for x, g in enumerate(row):
            w.view.tiles[(x, y)] = glyph.get(g, g)
    w.terrain_center, w.terrain_map = at, map_id
    return w


def ctx_for(m: Memory, kb: KnowledgeBase | None = None) -> PlayContext:
    return PlayContext(m, Policy(kind="scripted", goals=["explore"]), random.Random(0), knowledge=kb)


class InsideLevelTest(unittest.TestCase):
    def test_map_level_from_position(self):
        w = grid(["."], map_id=9)
        w.apply_position({"map_id": 9, "x": 0, "y": 0, "level": 2})
        self.assertTrue(inside_level(w, None))
        w.apply_position({"map_id": 1, "x": 0, "y": 0, "level": 0})
        self.assertFalse(inside_level(w, None))

    def test_without_level_field_not_inside(self):
        kb = KnowledgeBase.empty("sandbox")
        sync_town(kb, {"map_id": 1, "x": 0, "y": 0})
        w = grid(["."], map_id=8)
        self.assertFalse(inside_level(w, kb))


class LevelStateTest(unittest.TestCase):
    PARAMS = CostGridParams()

    def test_level_beats_explore_on_interior_map(self):
        kb = KnowledgeBase.empty("sandbox")
        w = grid(["....D"], at=(0, 0), map_id=8)
        w.apply_position({"map_id": 8, "x": 0, "y": 0, "level": 1})
        m = Memory()
        out = dispatch(w, ctx_for(m, kb))
        self.assertEqual(out.state, "Level")
        self.assertEqual((out.intents[0]["x"], out.intents[0]["y"]), (1, 0))

    def test_overworld_uses_explore(self):
        w = grid(["....."], at=(0, 0), map_id=1)
        w.apply_position({"map_id": 1, "x": 0, "y": 0, "level": 0})
        out = dispatch(w, ctx_for(Memory(), KnowledgeBase.empty("sandbox")))
        self.assertEqual(out.state, "Explore")

    def test_prefers_unvisited_door(self):
        kb = KnowledgeBase.empty("sandbox")
        w = grid(["..D.D"], at=(0, 0), map_id=8)
        w.apply_position({"map_id": 8, "x": 0, "y": 0, "level": 1})
        sync_map_from_view(kb, 8, w.view)
        record_warp(kb, 8, (2, 0), "framed_door", 9, (0, 0))
        m = Memory()
        out = dispatch(w, ctx_for(m, kb))
        self.assertEqual(out.state, "Level")
        self.assertEqual(m.path[-1], (4, 0), "walk toward the door whose warp is unknown")

    def test_level_beats_travel_while_inside(self):
        kb = KnowledgeBase.empty("sandbox")
        sync_town(kb, {"map_id": 2, "x": 0, "y": 0})
        w = grid(["...."], at=(0, 0), map_id=8)
        w.apply_position({"map_id": 8, "x": 0, "y": 0, "level": 3})
        m = Memory()
        from agentrealm_agent.travel import refresh_travel_stack

        refresh_travel_stack(m, ["travel:point:3:0"])
        out = dispatch(w, ctx_for(m, kb))
        self.assertEqual(out.state, "Level")


if __name__ == "__main__":
    unittest.main()
