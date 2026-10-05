"""A52: starter agent decide order and explore/flee behavior."""

import random
import threading
import unittest
from pathlib import Path

from agentrealm_agent.client import ApiError
from agentrealm_agent.config import CharacterConfig, Policy
from agentrealm_agent.world import Entity
from starter_agent import (
    StarterDecision,
    StarterMemory,
    StarterRunner,
    apply_tick,
    choose_call,
    decide,
    walk_step,
)
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


class StarterPolicyTest(unittest.TestCase):
    def test_hostile_range_and_kinds_come_from_policy(self):
        w = world([".......", ".......", "......."], at=(3, 1))
        w.entities = [Entity("npc", 1, (6, 1))]
        self.assertEqual(decide(w, StarterMemory(), random.Random(0)).mode, "Explore")
        self.assertEqual(decide(w, StarterMemory(policy=Policy(hostile_range=3)), random.Random(0)).mode, "Flee")
        self.assertNotEqual(decide(w, StarterMemory(policy=Policy(hostile=["character"])), random.Random(0)).mode, "Flee")

    def test_avoid_blocks_come_from_policy(self):
        w = world(["~~~", "~.~", "~~~"], at=(1, 1))
        self.assertIsNone(walk_step(w, StarterMemory(policy=Policy(avoid_blocks=["lava"])), random.Random(0)))
        self.assertIsNotNone(walk_step(w, StarterMemory(policy=Policy(avoid_blocks=[])), random.Random(0)))


class StarterResyncTest(unittest.TestCase):
    def test_died_event_forces_resync(self):
        w = world(["...", "...", "..."], at=(1, 1))
        m = StarterMemory(need_self=False, need_position=False, path=[(2, 2)])
        apply_tick(w, m, {"tick": 5, "events_by_tick": [{"tick": 5, "events": [{"kind": "Died"}]}]})
        self.assertTrue(m.need_self and m.need_position)
        self.assertEqual(m.path, [])
        self.assertEqual(choose_call(w, m), "self")


class StarterErrorTest(unittest.TestCase):
    def runner(self):
        # on_error needs only cfg, out, mem, and stop; skip __init__ so no trace file opens.
        r = StarterRunner.__new__(StarterRunner)
        r.cfg = CharacterConfig("T", "default", "x", "sandbox", Policy(), Path("t.toml"))
        r.lines, r.stop = [], threading.Event()
        r.out = r.lines.append
        r.mem = StarterMemory(need_self=False, need_position=False, path=[(1, 1)])
        return r

    def test_auth_failure_stops(self):
        for status in (401, 403):
            r = self.runner()
            self.assertFalse(r.on_error("tick", ApiError(status, "unauthorized")))
            self.assertIn("stopping", r.lines[-1])

    def test_not_live_resyncs_and_keeps_going(self):
        for code in ("not_on_map", "character_not_live"):
            r = self.runner()
            self.assertTrue(r.on_error("tick", ApiError(409, code)))
            self.assertTrue(r.mem.need_self and r.mem.need_position)
            self.assertEqual(r.mem.path, [])
