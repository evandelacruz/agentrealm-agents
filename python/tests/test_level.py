"""A37: Level state walks toward unexplored doors inside a level."""

import random
import unittest

from agentrealm_agent.config import Policy
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.knowledge_maps import record_warp, sync_map_from_view
from agentrealm_agent.memory import Memory
from agentrealm_agent.navigation import CostGridParams
from agentrealm_agent.navigation import stuck as nav_stuck
from agentrealm_agent.states import PlayContext, dispatch
from agentrealm_agent.states.level import inside_level
from agentrealm_agent.travel import refresh_travel_stack, sync_town
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
        self.assertTrue(inside_level(w))
        w.apply_position({"map_id": 1, "x": 0, "y": 0, "level": 0})
        self.assertFalse(inside_level(w))

    def test_without_level_field_not_inside(self):
        w = grid(["."], map_id=8)
        w.apply_position({"map_id": 8, "x": 0, "y": 0})
        self.assertFalse(inside_level(w))
        w.apply_position({"map_id": 8, "x": 0, "y": 0, "level": None})
        self.assertFalse(inside_level(w))

    def test_delta_without_level_stays_inside(self):
        w = grid(["."], map_id=9)
        w.apply_position({"map_id": 9, "x": 0, "y": 0, "level": 2})
        w.apply_position({"map_id": 9, "x": 1, "y": 0})
        self.assertEqual(w.map_level, 2)
        self.assertTrue(inside_level(w))

    def test_new_map_without_level_resets_it(self):
        w = grid(["."], map_id=9)
        w.apply_position({"map_id": 9, "x": 0, "y": 0, "level": 2})
        w.apply_position({"map_id": 1, "x": 5, "y": 5})
        self.assertIsNone(w.map_level)
        self.assertFalse(inside_level(w))

    def test_non_integer_level_ignored(self):
        w = grid(["."], map_id=9)
        w.apply_position({"map_id": 9, "x": 0, "y": 0, "level": 2})
        for bad in ("2", "deep", 1.5, True, [1]):
            with self.subTest(level=bad):
                with self.assertLogs("agentrealm_agent.world", level="WARNING"):
                    w.apply_position({"map_id": 9, "x": 3, "y": 3, "level": bad})
                self.assertEqual((w.map_id, w.pos, w.map_level), (9, (3, 3), 2), "keeps the last good level")

    def test_malformed_level_in_delta_does_not_raise(self):
        w = grid(["."], map_id=9)
        w.apply_position({"map_id": 9, "x": 0, "y": 0, "level": 2})
        with self.assertLogs("agentrealm_agent.world", level="WARNING"):
            w.apply_observation({"version": 11, "delta": {"position": {"map_id": 9, "x": 2, "y": 2, "level": "deep"}}})
        self.assertEqual((w.pos, w.map_level), ((2, 2), 2))


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

    def test_travel_directive_outranks_level(self):
        kb = KnowledgeBase.empty("sandbox")
        w = grid(["....D"], at=(0, 0), map_id=8)
        w.apply_position({"map_id": 8, "x": 0, "y": 0, "level": 3})
        m = Memory()
        refresh_travel_stack(m, ["travel:point:3:0"])
        out = dispatch(w, ctx_for(m, kb))
        self.assertEqual(out.state, "Travel")

    def test_level_takes_over_once_travel_arrives(self):
        kb = KnowledgeBase.empty("sandbox")
        w = grid(["....D"], at=(0, 0), map_id=8)
        w.apply_position({"map_id": 8, "x": 0, "y": 0, "level": 3})
        m = Memory()
        refresh_travel_stack(m, ["travel:point:0:0"])
        dispatch(w, ctx_for(m, kb))  # arrival drops the op
        out = dispatch(w, ctx_for(m, kb))
        self.assertEqual(out.state, "Level")

    def test_no_level_step_falls_through(self):
        # Walled in with nothing unexplored in reach: Level sends nothing and
        # does not hold the round, so a lower state gets it (A44).
        w = grid(["###", "#.#", "###"], at=(1, 1), map_id=8)
        w.apply_position({"map_id": 8, "x": 1, "y": 1, "level": 1})
        m = Memory()
        ctx = PlayContext(m, Policy(kind="scripted", goals=["hold"]), random.Random(0), knowledge=KnowledgeBase.empty("sandbox"))
        out = dispatch(w, ctx)
        self.assertNotEqual(out.state, "Level")
        self.assertTrue(any(y.startswith("Level: no level step") for y in out.yielded), out.yielded)

    def test_skips_door_and_frontiers_given_up_on(self):
        # A door or frontier dropped by stuck detection stays out of Level's
        # choice until its backoff ends, as it does for the goals (A15).
        w = grid(["....D"], at=(0, 0), map_id=8)
        w.apply_position({"map_id": 8, "x": 0, "y": 0, "level": 1})
        m = Memory()
        until = w.tick + 300
        m.nav_stuck.backoff_until[nav_stuck.goal_key("doors", 8, (4, 0))] = until
        for p in w.view.frontier():
            m.nav_stuck.backoff_until[nav_stuck.goal_key("explore", 8, p)] = until
        ctx = PlayContext(m, Policy(kind="scripted", goals=["hold"]), random.Random(0), knowledge=KnowledgeBase.empty("sandbox"))
        out = dispatch(w, ctx)
        self.assertNotEqual(out.state, "Level")
        self.assertTrue(any(y.startswith("Level: no level step") for y in out.yielded), out.yielded)

if __name__ == "__main__":
    unittest.main()
