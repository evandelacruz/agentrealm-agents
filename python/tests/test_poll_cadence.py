"""M6 dual poll cadence: calm spacing vs urgent every-tick polling."""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agentrealm_agent import config, runner
from agentrealm_agent.brain import Memory, choose_call
from agentrealm_agent.config import CharacterConfig, Policy
from agentrealm_agent.poll_cadence import calm_poll_interval, gate_tick_call, hostile_within, is_urgent
from agentrealm_agent.world import Entity, WorldModel


def world(at=(5, 5), tick=20) -> WorldModel:
    w = WorldModel(character_id=42, map_id=1, pos=at, perception=25, tick=tick)
    w.terrain_center, w.terrain_map = at, 1
    w.entities_tick = tick
    return w


def calm_mem(**kw) -> Memory:
    defaults = dict(need_self=False, need_position=False, last_poll_tick=20, calm_poll_interval=7)
    defaults.update(kw)
    return Memory(**defaults)


class PollCadenceTest(unittest.TestCase):
    def test_calm_interval_is_between_four_and_ten(self):
        for cid in (1, 2, 42, 1001):
            seen = {calm_poll_interval(t, cid) for t in range(200)}
            self.assertEqual(seen, set(range(4, 11)), cid)

    def test_calm_skips_until_the_interval_elapses(self):
        m = calm_mem(last_poll_tick=20, calm_poll_interval=7)
        self.assertEqual(gate_tick_call(world(tick=26), m, Policy()), "skip")
        self.assertEqual(gate_tick_call(world(tick=27), m, Policy()), "tick")

    def test_a_queued_intent_caps_the_calm_gap(self):
        # A one-intent queue runs one tick; skipping after it would leave the
        # character standing until the calm gap is up.
        m = calm_mem(last_poll_tick=20, calm_poll_interval=9, queued_ticks=1)
        self.assertEqual(gate_tick_call(world(tick=21), m, Policy()), "tick")
        m.queued_ticks = 3
        self.assertEqual(gate_tick_call(world(tick=22), m, Policy()), "skip")
        self.assertEqual(gate_tick_call(world(tick=23), m, Policy()), "tick")

    def test_hostile_within_three_blocks(self):
        w = world(at=(0, 0))
        w.entities = [Entity("npc", 1, (3, 0))]
        self.assertTrue(hostile_within(w, Policy(hostile=["npc"])))
        w.entities = [Entity("npc", 1, (4, 0))]
        self.assertFalse(hostile_within(w, Policy(hostile=["npc"])))
        w.entities = [Entity("character", 1, (1, 0))]
        self.assertFalse(hostile_within(w, Policy(hostile=["npc"])))

    def test_urgent_on_alarm_or_damage_in_the_last_poll(self):
        w = world()
        self.assertFalse(is_urgent(w, calm_mem(), Policy()))
        self.assertTrue(is_urgent(w, calm_mem(alarm=True), Policy()))
        # The entity read clears alarm; Damaged in the last poll still counts.
        self.assertTrue(is_urgent(w, calm_mem(hurt_last_poll=True), Policy()))

    def test_urgent_polls_every_tick_inside_the_calm_gap(self):
        w = world(tick=21)
        w.entities = [Entity("npc", 9, (7, 7))]
        m = calm_mem(last_poll_tick=20, calm_poll_interval=9)
        self.assertEqual(choose_call(w, m, Policy(hostile=["npc"])), "tick")
        self.assertEqual(choose_call(w, m, Policy(hostile=[])), "skip")

    def test_idle_policy_is_gated_too(self):
        # idle sends nothing, so it only polls for events: the calm cadence.
        m = calm_mem(last_poll_tick=20, calm_poll_interval=7)
        self.assertEqual(choose_call(world(tick=22), m, Policy(kind="idle")), "skip")
        self.assertEqual(choose_call(world(tick=27), m, Policy(kind="idle")), "tick")


class FakeServer:
    """A server whose tick moves one per paced window, with nothing in sight."""

    def __init__(self, windows: int, stop: threading.Event, events_at: dict[int, list[dict]] | None = None):
        self.tick_now = 100
        self.windows = windows
        self.stop = stop
        self.events_at = events_at or {}
        self.calls: list[tuple[int, str]] = []
        self.sent: list[tuple[int, list[dict] | None]] = []

    def wait(self, not_before: float = 0.0) -> None:
        self.windows -= 1
        if self.windows < 0:
            self.stop.set()
        self.tick_now += 1

    def world(self, cid):
        return {"tick_rate_hz": 10}

    def tick(self, cid, intents):
        self.calls.append((self.tick_now, "tick"))
        self.sent.append((self.tick_now, intents))
        ev = self.events_at.get(self.tick_now)
        r = {"tick": self.tick_now, "window_remaining_ms": 0, "queue_id": "q"}
        if ev:
            r["events_by_tick"] = [{"tick": self.tick_now, "events": ev}]
        return r

    def entities(self, cid, map_id, *rect):
        self.calls.append((self.tick_now, "entities"))
        return {"tick": self.tick_now}


class RunnerCadenceTest(unittest.TestCase):
    """Runs Runner.run itself: only the fake server's clock moves."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.object(config, "STATE_DIR", Path(tmp.name))
        patch.start()
        self.addCleanup(patch.stop)

    def run_windows(self, pol: Policy, windows: int, events_at=None) -> FakeServer:
        stop = threading.Event()
        server = FakeServer(windows, stop, events_at)
        cfg = CharacterConfig("T", "default", "test", "sandbox", pol, Path("t.toml"))
        r = runner.Runner(cfg, server, 1, stop, out=lambda _: None)
        w = WorldModel(character_id=1, map_id=7, pos=(0, 0), perception=25, tick=100)
        for y in range(-3, 4):
            for x in range(-3, 40):
                w.view.tiles[(x, y)] = "dirt"
        w.terrain_center, w.terrain_map, w.entities_tick = (0, 0), 7, 100
        r.world, r.mem = w, Memory(need_self=False, need_position=False)
        with mock.patch.object(runner.Pacer, "wait_next_window", lambda _self, nb=0.0: server.wait(nb)):
            r.run()
        return server

    def test_calm_with_nothing_to_do_keeps_polling(self):
        # The deadlock: a skip sends nothing, so only the local window count
        # can bring the next calm poll due.
        s = self.run_windows(Policy(goals=["hold"], entity_refresh=1000), 40)
        polls = [t for t, call in s.calls if call == "tick"]
        self.assertGreaterEqual(len(polls), 4)
        gaps = [b - a for a, b in zip(polls, polls[1:])]
        self.assertTrue(all(4 <= g <= 10 for g in gaps), gaps)

    def test_spare_windows_refresh_entities(self):
        s = self.run_windows(Policy(goals=["hold"], entity_refresh=3), 30)
        self.assertTrue(any(call == "entities" for _, call in s.calls))
        self.assertTrue(all(b[0] > a[0] for a, b in zip(s.calls, s.calls[1:])), "one call per window")

    def test_walking_polls_on_the_calm_cadence(self):
        # A paced Step/Wait queue covers many ticks, so calm polls stay 4–10 apart.
        s = self.run_windows(Policy(goals=["goto"], goto=(30, 0), pickup=False, entity_refresh=1000), 30)
        polls = [t for t, call in s.calls if call == "tick"]
        self.assertGreaterEqual(len(polls), 3)
        gaps = [b - a for a, b in zip(polls, polls[1:])]
        self.assertTrue(all(4 <= g <= 10 for g in gaps), gaps)

    def test_walking_polls_before_the_queue_runs_out(self):
        # A paced Step/Wait queue covers several ticks, so the calm gap opens,
        # but the next poll never lands after the queue has run out.
        s = self.run_windows(Policy(goals=["goto"], goto=(30, 0), pickup=False, entity_refresh=1000), 12)
        (first, queue), (second, _) = s.sent[0], s.sent[1]
        self.assertEqual(first, 101)
        self.assertGreater(len(queue), 1)
        self.assertGreater(second, first + 1, "no window is spent re-polling a running queue")
        self.assertLessEqual(second, first + len(queue))

    def test_damage_switches_to_every_tick(self):
        hit = [{"tick": 0, "kind": "Damaged", "source_kind": "npc", "amount": 1}]
        s = self.run_windows(Policy(goals=["hold"], entity_refresh=1000), 20, events_at={101: hit, 103: hit})
        self.assertEqual(s.calls[:5], [(101, "tick"), (102, "entities"), (103, "tick"), (104, "entities"), (105, "tick")])
        self.assertNotEqual(s.calls[5][0], 106, "calm again after a poll with no damage")


if __name__ == "__main__":
    unittest.main()
