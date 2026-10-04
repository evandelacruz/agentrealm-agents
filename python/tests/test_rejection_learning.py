"""Rejection learning for the navigation map (A14)."""

import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentrealm_agent.brain import Memory, decide
from agentrealm_agent.config import Policy
from agentrealm_agent import knowledge_base as kb_mod
from agentrealm_agent.navigation import CostGridParams, cost_path
from agentrealm_agent.navigation.rejection import (
    NavMemory,
    learn_step_rejection,
    navigation_avoid_costly,
    on_block_changed,
)
from agentrealm_agent.world import WorldModel, ZoneFact
from tests.test_cost_grid import grid


def scripted(**kw) -> Policy:
    kw.setdefault("pickup", False)
    return Policy(kind="scripted", **kw)


class RejectionLearningTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.worlds = Path(self.tmp.name) / "worlds"
        patch = mock.patch.object(kb_mod, "WORLDS_DIR", self.worlds)
        patch.start()
        self.addCleanup(patch.stop)
        self.kb = kb_mod.KnowledgeBase.empty("sandbox")

    def test_not_traversable_stays_impassable_until_block_changed(self):
        w = grid(["#####", "#...#", "#####"], at=(1, 1))
        m = Memory()
        learn_step_rejection(m, w, None, (2, 1), "not_traversable", 10)
        avoid, _ = navigation_avoid_costly(m.nav, None, 1, 10)
        self.assertIn((2, 1), avoid)
        self.assertIsNone(cost_path(w, (3, 1), CostGridParams(avoid=avoid)))
        on_block_changed(m, 1, (2, 1), 1)
        avoid, _ = navigation_avoid_costly(m.nav, None, 1, 11)
        self.assertNotIn((2, 1), avoid)
        self.assertIsNotNone(cost_path(w, (3, 1), CostGridParams(avoid=avoid)))

    def test_block_occupied_waits_one_decision_then_costs(self):
        w = grid(["..."])
        m = Memory()
        learn_step_rejection(m, w, None, (1, 0), "block_occupied", 10)
        avoid, costly = navigation_avoid_costly(m.nav, None, 1, 10)
        self.assertIn((1, 0), avoid)
        self.assertIn((1, 0), costly)
        d = decide(w, m, scripted(goals=["goto"], goto=(2, 0), pickup=False), random.Random(0))
        if d.intent:
            self.assertNotEqual((d.intent["x"], d.intent["y"]), (1, 0))
        avoid, costly = navigation_avoid_costly(m.nav, None, 1, 11)
        self.assertNotIn((1, 0), avoid)
        self.assertIn((1, 0), costly)

    def test_conflict_lost_does_not_block_the_tile(self):
        w = grid(["..."])
        m = Memory()
        learn_step_rejection(m, w, None, (1, 0), "conflict_lost", 10)
        avoid, costly = navigation_avoid_costly(m.nav, None, 1, 10)
        self.assertEqual(avoid, set())
        self.assertEqual(costly, set())

    def test_door_locked_records_in_knowledge_base(self):
        w = grid(["..D"], at=(0, 0))
        m = Memory()
        learn_step_rejection(m, w, self.kb, (1, 0), "door_locked", 5)
        doors = self.kb.maps["1"]["doors"]
        self.assertTrue(any(d["x"] == 1 and d.get("locked") for d in doors))
        avoid, _ = navigation_avoid_costly(m.nav, self.kb, 1, 5)
        self.assertIn((1, 0), avoid)

    def test_over_strength_ceiling_records_hunting_closure(self):
        w = grid(["..."])
        w.zones[1] = {(1, 0): ZoneFact(safe=False, strength_ceiling=12)}
        m = Memory()
        learn_step_rejection(m, w, self.kb, (1, 0), "over_strength_ceiling", 20)
        hunting = self.kb.maps["1"]["hunting"]["1,0"]
        self.assertTrue(hunting["closed"])
        self.assertEqual(hunting["strength_ceiling"], 12)

    def test_would_strand_steps_to_land_first(self):
        w = grid(["..."], at=(0, 0))
        m = Memory(nav=NavMemory(prefer_land=(1, 0)))
        d = decide(
            w,
            m,
            scripted(goals=["goto"], goto=(2, 0), pickup=False),
            random.Random(0),
            knowledge=self.kb,
        )
        self.assertEqual((d.intent["x"], d.intent["y"]), (1, 0))
        self.assertEqual(d.reason, "land first")


if __name__ == "__main__":
    unittest.main()
