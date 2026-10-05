"""A52: starter agent decide order and explore/flee behavior."""

import random
import unittest

from agentrealm_agent.world import Entity
from starter_agent import StarterDecision, StarterMemory, choose_call, decide, hostiles_in_range, walk_step
from tests.test_states import world


class StarterChooseCallTest(unittest.TestCase):
    def test_sync_reads_position_when_unknown(self):
        w = world(["...", "...", "..."], at=(1, 1))
        w.pos = None
        m = StarterMemory(need_self=False, need_position=True)
        self.assertEqual(choose_call(w, m), "position")

    def test_tick_when_placed_and_fresh(self):
        w = world(["...", "...", "..."], at=(1, 1))
        w.entities_tick = w.tick
        m = StarterMemory(need_self=False, need_position=False)
        self.assertEqual(choose_call(w, m), "tick")


class StarterDecideTest(unittest.TestCase):
    def test_flee_before_explore(self):
        w = world([".....", ".....", "....."], at=(2, 2))
        w.entities = [Entity("npc", 1, (2, 3))]
        m = StarterMemory()
        d = decide(w, m, random.Random(0))
        self.assertIsInstance(d, StarterDecision)
        self.assertEqual(d.mode, "Flee")
        self.assertEqual(d.intents[0]["verb"], "SetPosition")

    def test_explore_when_no_hostiles(self):
        w = world([".....", ".....", "....."], at=(1, 1))
        m = StarterMemory()
        d = decide(w, m, random.Random(0))
        self.assertEqual(d.mode, "Explore")
        self.assertEqual(d.intents[0]["verb"], "SetPosition")

    def test_sync_wake_when_asleep(self):
        w = world(["...", "...", "..."], at=(1, 1))
        w.asleep = True
        d = decide(w, StarterMemory(), random.Random(0))
        self.assertEqual(d.mode, "Sync")
        self.assertEqual(d.intents[0]["verb"], "Wait")


class StarterWalkTest(unittest.TestCase):
    def test_walk_step_moves_toward_frontier(self):
        w = world([".....", ".....", "....."], at=(1, 1))
        m = StarterMemory()
        step = walk_step(w, m, random.Random(0))
        self.assertIsNotNone(step)
        self.assertNotEqual(step, w.pos)

    def test_hostiles_in_range_respects_chebyshev(self):
        w = world([".......", ".......", "......."], at=(3, 3))
        w.entities = [Entity("npc", 1, (3, 6))]
        self.assertEqual(hostiles_in_range(w), [])
        w.entities = [Entity("npc", 1, (3, 5))]
        self.assertEqual(len(hostiles_in_range(w)), 1)
