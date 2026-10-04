"""M6 executor: queue invalidation with mocked tick results."""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agentrealm_agent import config
from agentrealm_agent.brain import Memory
from agentrealm_agent.config import CharacterConfig, Policy
from agentrealm_agent.executor import Executor, paced_set_positions
from agentrealm_agent.runner import Runner
from agentrealm_agent.world import Entity, WorldModel
from tests.test_runner import FakeClient, rejected


class PacedQueueTest(unittest.TestCase):
    def test_four_ticks_between_moves_at_default_speed(self):
        q = paced_set_positions([(1, 0), (2, 0)], tick_rate_hz=10, movement_speed=2500)
        self.assertEqual([i.get("verb") for i in q], ["SetPosition", "Wait", "Wait", "Wait", "SetPosition"])


class QueueInvalidationTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.object(config, "STATE_DIR", Path(tmp.name))
        patch.start()
        self.addCleanup(patch.stop)

    def world_and_runner(self, fake: FakeClient, pol: Policy) -> Runner:
        cfg = CharacterConfig("T", "default", "test", "sandbox", pol, Path("t.toml"))
        r = Runner(cfg, fake, 1, threading.Event(), out=lambda _: None)
        self.addCleanup(r.trace.close)
        w = WorldModel(character_id=1, map_id=7, pos=(0, 0), perception=3)
        for y in range(2):
            for x in range(5):
                w.view.tiles[(x, y)] = "dirt"
        r.world, r.mem = w, Memory(need_self=False, need_position=False)
        r.tick_rate_hz = 10
        r.executor.tick_rate_hz = 10
        return r

    def test_block_occupied_discards_remainder_and_replans(self):
        pol = Policy(goals=["goto"], goto=(4, 0), pickup=False)
        fake = FakeClient([
            {"tick": 10, "window_remaining_ms": 0},
            {"tick": 11, "window_remaining_ms": 0,
             "intent_results": [rejected("q1", "block_occupied", "occupied", 10)]},
            {"tick": 13, "window_remaining_ms": 0},
        ])
        r = self.world_and_runner(fake, pol)
        r.tick()
        first = fake.sent[0]
        self.assertGreater(len(first), 1, "multi-intent movement queue")
        self.assertEqual(first[0], {"verb": "SetPosition", "x": 1, "y": 0})

        r.tick()
        self.assertEqual(r.world.pos, (0, 0))
        self.assertTrue(r.mem.need_position)
        self.assertEqual(r.mem.path, [])
        self.assertTrue(r.executor.invalidated)

        r.world.apply_position({"map_id": 7, "x": 0, "y": 0})
        r.mem.need_position = False
        r.tick()
        resend = fake.sent[2]
        self.assertIsNotNone(resend)
        self.assertNotEqual(resend[0], {"verb": "SetPosition", "x": 1, "y": 0})

    def test_beyond_movement_range_clears_remainder(self):
        ex = Executor(tick_rate_hz=10)
        w = WorldModel(character_id=1, map_id=7, pos=(0, 0), perception=3)
        w.view.tiles[(1, 0)] = "dirt"
        m = Memory()
        m.path = [(1, 0), (2, 0)]
        q = ex.build_movement_queue(m.path, w)
        ex.note_sent(q, "q1", w.pos)
        res = rejected("q1", "beyond_movement_range", "range", 11)
        self.assertTrue(ex.ingest_results([res], w, m))
        self.assertEqual(m.path, [])
        self.assertTrue(ex.invalidated)
        self.assertEqual(w.pos, (0, 0))

    def test_poll_leaves_queue_while_still_valid(self):
        pol = Policy(goals=["goto"], goto=(2, 0), pickup=False)
        fake = FakeClient([
            {"tick": 10, "window_remaining_ms": 0},
            {"tick": 11, "window_remaining_ms": 0,
             "intent_results": [{"tick": 10, "queue_id": "q1", "index": 0, "outcome": "applied"}]},
            {"tick": 12, "window_remaining_ms": 0},
        ])
        r = self.world_and_runner(fake, pol)
        r.tick()
        r.tick()
        self.assertIsNone(fake.sent[1], "poll while queue runs")
        self.assertTrue(r.executor.active or r.executor.in_flight is not None)

    def test_entity_on_path_invalidates_without_waiting_for_rejection(self):
        ex = Executor(tick_rate_hz=10)
        w = WorldModel(character_id=1, map_id=7, pos=(0, 0), perception=3)
        w.view.tiles[(1, 0)] = w.view.tiles[(2, 0)] = "dirt"
        m = Memory()
        m.path = [(1, 0), (2, 0)]
        q = ex.build_movement_queue(m.path, w)
        ex.note_sent(q, "q1", w.pos)
        w.entities = [Entity("npc", 9, (2, 0))]
        self.assertTrue(ex.invalidate_if_stale(w, m))
        self.assertEqual(m.path, [])


if __name__ == "__main__":
    unittest.main()
