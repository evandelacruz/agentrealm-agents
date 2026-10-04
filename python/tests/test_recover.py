"""A11: Recover walks to the death chest only when the spot is safe (A7)."""

import random
import unittest

from agentrealm_agent.brain import decide
from agentrealm_agent.config import Policy
from agentrealm_agent.memory import Memory
from agentrealm_agent.states import dispatch
from agentrealm_agent.states.recover import recover_approach_target, recover_spot_safe
from agentrealm_agent.world import WorldModel
from agentrealm_agent.zone_discovery import apply_zone


def world(rows: list[str], at=(0, 0), perception=5) -> WorldModel:
    glyph = {".": "dirt", "#": "wall"}
    w = WorldModel(character_id=1, map_id=7, pos=at, perception=perception)
    for y, row in enumerate(rows):
        for x, g in enumerate(row):
            w.view.tiles[(x, y)] = glyph[g]
    w.terrain_center, w.terrain_map = at, 7
    return w


def scripted(**kw) -> Policy:
    return Policy(kind="scripted", **kw)


class RecoverSafetyTest(unittest.TestCase):
    def test_approach_from_safe_neighbour_when_chest_tile_unsafe(self):
        w = world(["....."], at=(4, 0))
        apply_zone(w, 7, 0, 0, {"safe": False, "brightness": 1})
        apply_zone(w, 7, 1, 0, {"safe": True, "brightness": 1})
        self.assertTrue(recover_spot_safe(w, 7, (0, 0)))
        self.assertEqual(recover_approach_target(w, 7, (0, 0)), (1, 0))

    def test_unsafe_until_zone_known(self):
        w = world(["....."], at=(4, 0))
        self.assertFalse(recover_spot_safe(w, 7, (0, 0)))

    def test_no_walk_without_safe_tile(self):
        w = world(["....."], at=(4, 0))
        w.apply_events(
            [{"tick": 5, "events": [{"kind": "Died", "cause": "killed", "chest_id": 80, "map_id": 7, "x": 0, "y": 0}]}]
        )
        w.map_id, w.pos = 7, (4, 0)
        d = decide(w, Memory(), scripted(goals=["hold"]), random.Random(0))
        self.assertIsNone(d.intent)

    def test_recovers_when_adjacent_tile_safe(self):
        w = world(["....."], at=(4, 0))
        w.apply_events(
            [{"tick": 5, "events": [{"kind": "Died", "cause": "killed", "chest_id": 80, "map_id": 7, "x": 0, "y": 0}]}]
        )
        w.map_id, w.pos = 7, (4, 0)
        apply_zone(w, 7, 1, 0, {"safe": True, "brightness": 1})
        d = decide(w, Memory(), scripted(goals=["hold"]), random.Random(0))
        self.assertEqual((d.intent["verb"], d.intent["x"]), ("SetPosition", 3))


class RecoverDispatchTest(unittest.TestCase):
    def test_recover_beats_explore(self):
        w = world(["....."], at=(4, 0))
        w.apply_events(
            [{"tick": 5, "events": [{"kind": "Died", "cause": "killed", "chest_id": 80, "map_id": 7, "x": 0, "y": 0}]}]
        )
        w.map_id, w.pos = 7, (4, 0)
        apply_zone(w, 7, 1, 0, {"safe": True, "brightness": 1})
        from agentrealm_agent.states import PlayContext

        out = dispatch(w, PlayContext(Memory(), scripted(goals=["hold"]), random.Random(0)))
        self.assertEqual(out.state, "Recover")


if __name__ == "__main__":
    unittest.main()
