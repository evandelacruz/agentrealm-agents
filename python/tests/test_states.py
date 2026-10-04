"""A5: priority dispatcher and list[Intent] test seam."""

import random
import unittest

from agentrealm_agent.brain import Memory, PlayContext, decide
from agentrealm_agent.config import Policy
from agentrealm_agent.states import dispatch, scripted_outcome
from agentrealm_agent.world import Entity, WorldModel


def world(rows: list[str], at=(0, 0), perception=3) -> WorldModel:
    glyph = {".": "dirt", "#": "wall", "D": "framed_door", "~": "lava"}
    w = WorldModel(character_id=1, map_id=7, pos=at, perception=perception)
    for y, row in enumerate(rows):
        for x, g in enumerate(row):
            w.view.tiles[(x, y)] = glyph[g]
    w.terrain_center, w.terrain_map = at, 7
    return w


def ctx(w: WorldModel, m: Memory | None = None, **policy_kw) -> PlayContext:
    return PlayContext(m or Memory(), Policy(kind="scripted", **policy_kw), random.Random(0))


class DispatchPriorityTest(unittest.TestCase):
    def test_sync_beats_explore(self):
        w = world(["..."])
        w.pos = None
        out = dispatch(w, ctx(w))
        self.assertEqual(out.state, "Sync")
        self.assertIsNone(out.intents)

    def test_downed_beats_explore(self):
        w = world(["..."])
        w.alive = False
        out = dispatch(w, ctx(w))
        self.assertEqual(out.state, "Downed")
        self.assertIsNone(out.intents)

    def test_explore_returns_intent_list(self):
        w = world(["....", "...."])
        for x in range(-1, 5):
            w.view.tiles.setdefault((x, -1), "")
            w.view.tiles.setdefault((x, 2), "")
        w.view.tiles[(-1, 0)] = w.view.tiles[(-1, 1)] = ""
        out = dispatch(w, ctx(w, goals=["explore"]))
        self.assertEqual(out.state, "Explore")
        self.assertIsNotNone(out.intents)
        self.assertEqual(out.intents[0]["verb"], "SetPosition")

    def test_idle_policy(self):
        w = world(["..."])
        out = dispatch(w, PlayContext(Memory(), Policy(kind="idle"), random.Random(0)))
        self.assertEqual(out.state, "Idle")
        self.assertIsNone(out.intents)

    def test_scripted_outcome_matches_decide(self):
        w = world(["...", "...", "..."], at=(1, 1))
        w.entities = [Entity("npc", 5, (2, 1))]
        pol = Policy(kind="scripted")
        m = Memory()
        rng = random.Random(0)
        out = scripted_outcome(w, m, pol, rng, never_attack=[])
        d = decide(w, Memory(), pol, rng)
        self.assertEqual(out.intents[0] if out.intents else None, d.intent)
        self.assertEqual(out.reflex, d.reflex)


if __name__ == "__main__":
    unittest.main()
