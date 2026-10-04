"""M6 executor: queue invalidation with mocked tick results."""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agentrealm_agent import config
from agentrealm_agent.brain import Memory
from agentrealm_agent.config import CharacterConfig, Policy
from agentrealm_agent.executor import QUEUE_HORIZON_INTENTS, Executor, wait
from agentrealm_agent.runner import Runner
from agentrealm_agent.world import Entity, WorldModel
from tests.test_runner import FakeClient, rejected


class PacedQueueTest(unittest.TestCase):
    def setUp(self):
        self.ex = Executor(tick_rate_hz=10)
        self.w = WorldModel(character_id=1, map_id=7, pos=(0, 0), perception=3, tick=20)

    def test_four_ticks_between_moves_at_default_speed(self):
        q = self.ex.build_movement_queue([(1, 0), (2, 0)], self.w)
        self.assertEqual([i["verb"] for i in q], ["Step", "Wait", "Wait", "Wait", "Step"])

    def test_long_path_is_cut_after_the_last_step_that_fits(self):
        # 11 steps × 4 ticks is over the 40-intent horizon: keep 10 steps and
        # drop the trailing Waits; the next queue opens with the Waits owed.
        path = [(x, 0) for x in range(1, 12)]
        q = self.ex.build_movement_queue(path, self.w)
        self.assertLessEqual(len(q), QUEUE_HORIZON_INTENTS)
        self.assertEqual(sum(i["verb"] == "Step" for i in q), 10)
        self.assertEqual(q[-1], {"verb": "Step", "direction": "right"})

    def test_lead_waits_after_a_recent_move(self):
        self.w.pos = (1, 0)
        self.ex.last_move_tick = 19  # next move allowed at tick 23; first intent runs at 21
        q = self.ex.build_movement_queue([(2, 0), (3, 0)], self.w)
        self.assertEqual(q[:3], [wait(), wait(), {"verb": "Step", "direction": "right"}])

    def test_single_step_decision_is_paced_too(self):
        self.ex.last_move_tick = 19
        q = self.ex.build_from_decision({"verb": "SetPosition", "x": 1, "y": 0}, [], self.w)
        self.assertEqual(q, [wait(), wait(), {"verb": "Step", "direction": "right"}])

    def test_a_non_adjacent_target_goes_as_sent(self):
        intent = {"verb": "SetPosition", "x": 3, "y": 0}
        self.assertEqual(self.ex.build_from_decision(intent, [], self.w), [intent])


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
        self.assertEqual(first[0], {"verb": "Step", "direction": "right"})

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
        self.assertNotEqual(resend[0], {"verb": "Step", "direction": "right"})

    def test_beyond_movement_range_clears_remainder(self):
        ex = Executor(tick_rate_hz=10)
        w = WorldModel(character_id=1, map_id=7, pos=(0, 0), perception=3)
        w.view.tiles[(1, 0)] = "dirt"
        m = Memory()
        m.path = [(1, 0), (2, 0)]
        q = ex.build_movement_queue(m.path, w)
        ex.note_sent(q, "q1", w.pos, m)
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
        self.assertTrue(r.executor.active)
        self.assertEqual(r.world.pos, (1, 0))

    def test_entity_on_path_invalidates_without_waiting_for_rejection(self):
        ex = Executor(tick_rate_hz=10)
        w = WorldModel(character_id=1, map_id=7, pos=(0, 0), perception=3)
        w.view.tiles[(1, 0)] = w.view.tiles[(2, 0)] = "dirt"
        m = Memory()
        m.path = [(1, 0), (2, 0)]
        q = ex.build_movement_queue(m.path, w)
        ex.note_sent(q, "q1", w.pos, m)
        w.entities = [Entity("npc", 9, (2, 0))]
        self.assertTrue(ex.invalidate_if_stale(w, m))
        self.assertEqual(m.path, [])

    def test_stale_drop_with_nothing_to_do_replaces_the_held_queue(self):
        fake = FakeClient([
            {"tick": 10, "window_remaining_ms": 0},
            {"tick": 11, "window_remaining_ms": 0},
        ])
        r = self.world_and_runner(fake, Policy(goals=["hold"]))
        ex, w, m = r.executor, r.world, r.mem
        ex.note_sent(ex.build_movement_queue([(1, 0), (2, 0)], w), "q0", w.pos, m)
        w.entities = [Entity("npc", 9, (2, 0))]
        self.assertTrue(ex.invalidate_if_stale(w, m))
        self.assertTrue(m.need_position, "results of the dropped queue are no longer read")
        m.need_position, w.entities = False, []  # the forced position read; the NPC moved on
        r.tick()
        self.assertEqual(fake.sent[0], [wait()], "an empty request would leave the stale queue running")
        r.tick()
        self.assertIsNone(fake.sent[1], "replaced once, then nothing to send")

    def test_hostile_in_range_drops_the_queue_and_flees(self):
        # Reflex 3 runs every round trip, not only once the queue drains.
        pol = Policy(goals=["goto"], goto=(4, 0), pickup=False)
        fake = FakeClient([
            {"tick": 10, "window_remaining_ms": 0},
            {"tick": 11, "window_remaining_ms": 0},
        ])
        r = self.world_and_runner(fake, pol)
        r.tick()
        self.assertTrue(r.executor.active)
        r.world.entities = [Entity("npc", 9, (2, 1))]  # off the path, within hostile_range
        r.tick()
        self.assertIsNotNone(fake.sent[1], "the flee replaces the held queue")
        self.assertEqual(fake.sent[1][-1], {"verb": "Step", "direction": "down"})
        self.assertEqual(r.executor.in_flight.queue_id, "q2")
        self.assertTrue(r.mem.need_position, "results of the dropped queue are no longer read")

    def test_supply_in_reach_drops_the_queue_and_takes(self):
        pol = Policy(goals=["goto"], goto=(4, 0), pickup=True)
        fake = FakeClient([
            {"tick": 10, "window_remaining_ms": 0},
            {"tick": 11, "window_remaining_ms": 0},
        ])
        r = self.world_and_runner(fake, pol)
        r.tick()
        r.world.entities = [Entity("supply", 5, (0, 1))]
        r.tick()
        self.assertEqual(fake.sent[1], [{"verb": "Take", "supply_id": 5}])

    def test_hostile_ignored_leaves_the_queue_running(self):
        pol = Policy(goals=["goto"], goto=(4, 0), pickup=False, on_hostile="ignore")
        fake = FakeClient([
            {"tick": 10, "window_remaining_ms": 0},
            {"tick": 11, "window_remaining_ms": 0},
        ])
        r = self.world_and_runner(fake, pol)
        r.tick()
        r.world.entities = [Entity("npc", 9, (2, 1))]
        r.tick()
        self.assertIsNone(fake.sent[1])
        self.assertTrue(r.executor.active)

    def test_results_for_the_queue_just_sent_are_applied(self):
        pol = Policy(goals=["goto"], goto=(4, 0), pickup=False)
        fake = FakeClient([
            {"tick": 10, "window_remaining_ms": 0,
             "intent_results": [{"tick": 10, "queue_id": "q1", "index": 0, "outcome": "applied"}]},
        ])
        r = self.world_and_runner(fake, pol)
        r.tick()
        self.assertEqual(r.world.pos, (1, 0))
        self.assertEqual(r.executor.in_flight.next_index, 1)


class ExecutorTest(unittest.TestCase):
    def setUp(self):
        self.ex = Executor(tick_rate_hz=10)
        self.w = WorldModel(character_id=1, map_id=7, pos=(0, 0), perception=3)
        for x in range(4):
            self.w.view.tiles[(x, 0)] = "dirt"
        self.m = Memory(need_self=False, need_position=False)
        self.m.path = [(1, 0), (2, 0)]
        self.q = self.ex.build_movement_queue(self.m.path, self.w)
        self.ex.note_sent(self.q, "q1", self.w.pos, self.m)

    def applied(self, index: int, tick: int) -> dict:
        return {"tick": tick, "queue_id": "q1", "index": index, "outcome": "applied"}

    def test_queue_completes_through_applied_results(self):
        results = [self.applied(i, 10 + i) for i in range(len(self.q))]
        self.assertFalse(self.ex.ingest_results(results, self.w, self.m))
        self.assertIsNone(self.ex.in_flight)
        self.assertFalse(self.ex.active)
        self.assertEqual(self.w.pos, (2, 0))
        self.assertEqual(self.m.path, [])
        self.assertEqual(self.ex.last_move_tick, 14)  # the second Step, index 4
        self.assertIsNone(self.ex.tick_payload(None), "nothing held to replace")

    def test_a_door_mid_queue_drops_and_replaces_the_rest(self):
        # A door moves us, so the Steps queued behind it would walk from the wrong place.
        self.w.view.tiles[(1, 0)] = "framed_door"
        self.assertTrue(self.ex.ingest_results([self.applied(0, 10)], self.w, self.m))
        self.assertTrue(self.m.need_position)
        self.assertFalse(self.ex.active)
        self.assertEqual(self.ex.tick_payload(None), [wait()])

    def test_unknown_outcome_drops_the_queue(self):
        res = {"tick": 10, "queue_id": "q1", "index": 0, "outcome": "discarded"}
        self.assertTrue(self.ex.ingest_results([res], self.w, self.m))
        self.assertFalse(self.ex.active)
        self.assertIsNone(self.ex.in_flight)
        self.assertTrue(self.m.need_position)
        self.assertEqual(self.ex.tick_payload(None), [wait()])

    def test_missing_queue_id_forces_a_reread(self):
        ex = Executor(tick_rate_hz=10)
        ex.note_sent([wait()], None, self.w.pos, self.m)
        self.assertFalse(ex.active)
        self.assertIsNone(ex.in_flight)
        self.assertTrue(self.m.need_position)

    def test_damage_drops_and_replaces(self):
        ev = [{"tick": 11, "kind": "Damaged", "amount": 2}]
        self.assertTrue(self.ex.invalidate_from_events(ev, self.w, self.m))
        self.assertTrue(self.m.need_position)
        self.assertEqual(self.m.path, [])
        self.assertEqual(self.ex.tick_payload(None), [wait()])

    def test_attacked_drops(self):
        ev = [{"tick": 11, "kind": "Attacked"}]
        self.assertTrue(self.ex.invalidate_from_events(ev, self.w, self.m))
        self.assertFalse(self.ex.active)

    def test_death_drops_without_a_replacing_queue(self):
        ev = [{"tick": 11, "kind": "Died"}]
        self.assertTrue(self.ex.invalidate_from_events(ev, self.w, self.m))
        self.assertTrue(self.m.need_self and self.m.need_position)
        self.assertIsNone(self.ex.tick_payload(None))

    def test_block_changed_on_the_path_drops(self):
        self.w.view.tiles[(2, 0)] = "bush"
        ev = [{"tick": 11, "kind": "BlockChanged", "map_id": 7, "x": 2, "y": 0, "block_type": "bush"}]
        self.assertTrue(self.ex.invalidate_from_events(ev, self.w, self.m))
        self.assertTrue(self.m.need_position)
        self.assertFalse(self.ex.active)

    def test_block_changed_off_the_path_keeps_the_queue(self):
        self.w.view.tiles[(3, 0)] = "bush"
        ev = [{"tick": 11, "kind": "BlockChanged", "map_id": 7, "x": 3, "y": 0, "block_type": "bush"}]
        self.assertFalse(self.ex.invalidate_from_events(ev, self.w, self.m))
        self.assertTrue(self.ex.active)
        self.assertFalse(self.m.need_position)


if __name__ == "__main__":
    unittest.main()
