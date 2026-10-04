"""The loop's handling of round-trip results, events, and failures, with a fake server."""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agentrealm_agent import config
from agentrealm_agent.brain import Memory
from agentrealm_agent.client import ApiError, Client
from agentrealm_agent.config import CharacterConfig, Policy
from agentrealm_agent.runner import Pacer, Runner
from agentrealm_agent.world import WorldModel


class FakeClient:
    """Answers tick submits from a script and records what was sent.

    Each submit gets queue_id qN, N counting submits from 1, as the front
    answers with the request's id (docs/API.md Round Trip).
    """

    def __init__(self, ticks: list[dict]):
        self.ticks = list(ticks)
        self.sent: list[list[dict] | None] = []

    def tick(self, cid, intents):
        self.sent.append(intents)
        r = dict(self.ticks.pop(0))
        if intents is not None:
            r["queue_id"] = f"q{len(self.sent)}"
        return r


def rejected(queue_id: str, code: str, category: str, tick: int = 1) -> dict:
    return {"tick": tick, "queue_id": queue_id, "index": 0, "outcome": "rejected",
            "rejection": {"category": category, "code": code, "retryability": "transient"}}


class RunnerTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.object(config, "STATE_DIR", Path(tmp.name))
        patch.start()
        self.addCleanup(patch.stop)

    def runner(self, client, pol: Policy) -> Runner:
        cfg = CharacterConfig("T", "default", "test", "sandbox", pol, Path("t.toml"))
        r = Runner(cfg, client, 1, threading.Event(), out=lambda _: None)
        self.addCleanup(r.trace.close)
        w = WorldModel(character_id=1, map_id=7, pos=(0, 0), perception=3)
        for y in range(2):
            for x in range(5):
                w.view.tiles[(x, y)] = "dirt"
        r.world, r.mem = w, Memory(need_self=False, need_position=False)
        return r

    def test_rejected_step_rolls_back_and_is_not_resubmitted(self):
        # Reflex 1 (PLAN.md): a rejected SetPosition does
        # not enter the block, so the local model must not keep us there.
        pol = Policy(goals=["goto"], goto=(4, 0), pickup=False)
        fake = FakeClient([
            {"tick": 10, "window_remaining_ms": 0},
            {"tick": 11, "window_remaining_ms": 0,
             "intent_results": [rejected("q1", "block_occupied", "occupied", 10)]},
            {"tick": 13, "window_remaining_ms": 0,
             "intent_results": [rejected("q2", "beyond_movement_range", "range", 11)]},
        ])
        r = self.runner(fake, pol)
        r.tick()
        self.assertEqual(fake.sent[0], [{"verb": "SetPosition", "x": 1, "y": 0}], "a one-entry queue")
        self.assertEqual(r.world.pos, (1, 0), "assumed applied")

        r.tick()  # planned from (1, 0) before the rejection was known
        self.assertEqual(r.world.pos, (0, 0), "rolled back to before the rejected step")
        self.assertTrue(r.mem.need_position)

        r.world.apply_position({"map_id": 7, "x": 0, "y": 0})  # the forced position read
        r.mem.need_position, r.mem.undo = False, None
        r.tick()
        self.assertNotEqual(fake.sent[2], [{"verb": "SetPosition", "x": 1, "y": 0}])

    def test_a_result_for_another_queue_is_not_applied(self):
        # docs/API.md Intent Results: a result names its queue_id and index, so
        # one for a queue other than the pending intent's is not its result.
        fake = FakeClient([
            {"tick": 10, "window_remaining_ms": 0},
            {"tick": 11, "window_remaining_ms": 0,
             "intent_results": [rejected("q0", "block_occupied", "occupied", 9)]},
        ])
        r = self.runner(fake, Policy(goals=["goto"], goto=(4, 0), pickup=False))
        r.tick()
        r.tick()
        self.assertFalse(r.mem.need_position)
        self.assertEqual(r.world.pos, (2, 0))

    def test_nothing_to_do_leaves_the_queue_as_it_is(self):
        # docs/API.md Intent Queue: a request without intents leaves the held
        # queue; an empty list would clear it.
        fake = FakeClient([{"tick": 12, "window_remaining_ms": 0}])
        r = self.runner(fake, Policy(goals=["hold"]))
        r.tick()
        self.assertEqual(fake.sent, [None])

    def test_queue_events_about_us_carry_no_subject(self):
        # docs/API.md, Events: Attacked, Damaged, and Died on a queue happen to
        # the owner and never carry subject_id.
        fake = FakeClient([{"tick": 12, "window_remaining_ms": 0, "events_by_tick": [
            {"tick": 11, "events": [{"tick": 11, "kind": "Damaged", "source_kind": "npc", "source_id": 4, "amount": 3},
                                    {"tick": 11, "kind": "Died", "cause": "npc"}]}]}])
        r = self.runner(fake, Policy(goals=["hold"]))
        r.tick()
        self.assertTrue(r.mem.alarm)
        self.assertTrue(r.mem.need_self and r.mem.need_position)
        self.assertIsNone(r.world.pos)
        self.assertEqual(r.world.recent_damage, [(11, 3)])

    def test_a_step_sent_as_we_die_is_not_assumed(self):
        # Died forgets the position; the step sent that round trip has no
        # position to land from, so the model stays unplaced until re-read.
        fake = FakeClient([{"tick": 12, "window_remaining_ms": 0, "events_by_tick": [
            {"tick": 11, "events": [{"tick": 11, "kind": "Died", "cause": "npc"}]}]}])
        r = self.runner(fake, Policy(goals=["goto"], goto=(4, 0), pickup=False))
        r.tick()
        self.assertIsNotNone(fake.sent[0])
        self.assertIsNone(r.world.pos)
        self.assertIsNone(r.world.map_id)


class PacedServer:
    """A server whose tick moves one per paced window; stops after `windows`."""

    def __init__(self, windows: int, stop: threading.Event):
        self.now, self.windows, self.stop = 100, windows, stop
        self.calls: list[tuple[int, str, list[dict] | None]] = []

    def window(self, _not_before: float = 0.0) -> None:
        self.windows -= 1
        if self.windows < 0:
            self.stop.set()
        self.now += 1

    def world(self, cid):
        return {"tick_rate_hz": 10}

    def tick(self, cid, intents):
        self.calls.append((self.now, "tick", intents))
        r = {"tick": self.now, "window_remaining_ms": 0}
        if intents is not None:
            r["queue_id"] = f"q{len(self.calls)}"
        return r

    def entities(self, cid, map_id, *rect):
        self.calls.append((self.now, "entities", None))
        return {"tick": self.now}


class CalmCadenceTest(unittest.TestCase):
    """Runs Runner.run itself: only the fake server's clock moves (M6)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.object(config, "STATE_DIR", Path(tmp.name))
        patch.start()
        self.addCleanup(patch.stop)

    def run_windows(self, pol: Policy, windows: int) -> PacedServer:
        stop = threading.Event()
        server = PacedServer(windows, stop)
        cfg = CharacterConfig("T", "default", "test", "sandbox", pol, Path("t.toml"))
        r = Runner(cfg, server, 1, stop, out=lambda _: None)
        w = WorldModel(character_id=1, map_id=7, pos=(0, 0), perception=25, tick=100)
        for x in range(0, 12):
            w.view.tiles[(x, 0)] = "dirt"
        w.terrain_center, w.terrain_map, w.entities_tick = (0, 0), 7, 100
        r.world, r.mem = w, Memory(need_self=False, need_position=False)
        with mock.patch.object(Pacer, "wait_next_window", lambda _self, nb=0.0: server.window(nb)):
            r.run()
        return server

    def test_calm_with_nothing_to_do_keeps_polling(self):
        # A wait sends nothing, so no response moves the model's tick: the gap
        # must be counted in windows or the agent never polls again.
        s = self.run_windows(Policy(goals=["hold"], entity_refresh=5), 40)
        polls = [t for t, call, _ in s.calls if call == "tick"]
        self.assertGreaterEqual(len(polls), 4, s.calls)
        # entity_refresh windows of waiting, plus the entity read that falls due.
        self.assertTrue(all(4 <= b - a <= 10 for a, b in zip(polls, polls[1:])), polls)
        self.assertTrue(any(b - a > 1 for a, b in zip(polls, polls[1:])), "calm spaces its polls")

    def test_calm_walk_steps_every_window(self):
        # Waiting after a one-intent queue would stand the character still
        # until the calm gap is up.
        s = self.run_windows(Policy(goals=["goto"], goto=(10, 0), pickup=False, entity_refresh=5), 10)
        # Every window spends a call: a step, or the entity read that falls due.
        spent = [t for t, _, _ in s.calls]
        self.assertEqual(spent, list(range(101, 101 + len(spent))), s.calls)
        steps = [intents for _, call, intents in s.calls if call == "tick" and intents]
        self.assertEqual(steps[-1], [{"verb": "SetPosition", "x": 10, "y": 0}])


class NetworkTest(unittest.TestCase):
    def test_network_failure_is_retried_not_fatal(self):
        # A refused or reset connection is retryable like a 503: it must not end
        # the character's loop.
        with self.assertRaises(ApiError) as cm:
            Client("http://127.0.0.1:9", "k", timeout=1).self_(1)
        self.assertTrue(cm.exception.network)
        stop = threading.Event()
        stop.set()
        fake = FakeClient([])
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(config, "STATE_DIR", Path(tmp)):
            cfg = CharacterConfig("T", "default", "test", "sandbox", Policy(), Path("t.toml"))
            r = Runner(cfg, fake, 1, stop, out=lambda _: None)
            retry_at = r.on_error("tick", cm.exception)
            r.trace.close()
        self.assertGreater(retry_at, 0)


if __name__ == "__main__":
    unittest.main()
