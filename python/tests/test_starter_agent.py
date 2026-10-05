"""A52: starter agent decide order and explore/flee behavior."""

import io
import random
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from agentrealm_agent import config
from agentrealm_agent.client import ApiError
from agentrealm_agent.config import CharacterConfig, Policy
from agentrealm_agent.world import Entity
from starter_agent import (
    StarterDecision,
    StarterMemory,
    StarterRunner,
    apply_tick,
    choose_call,
    create,
    decide,
    main,
    run,
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


    def test_boxed_in_holds(self):
        w = world(["###", "#.#", "###"], at=(1, 1))
        w.entities = [Entity("npc", 1, (1, 0))]
        d = decide(w, StarterMemory(), random.Random(0))
        self.assertEqual((d.mode, d.intents), ("Flee", None))


class StarterWalkTest(unittest.TestCase):
    def test_walk_step_moves_toward_frontier(self):
        w = world([".....", ".....", "....."], at=(1, 1))
        m = StarterMemory()
        step = walk_step(w, m, random.Random(0))
        self.assertIsNotNone(step)
        self.assertNotEqual(step, w.pos)

    def test_walks_around_u_shaped_wall(self):
        # The only frontier tile is (8,1), behind a wall at x=4 with a gap in row 5.
        w = world(
            [
                "#########",
                "#...#....",
                "#...#...#",
                "#...#...#",
                "#...#...#",
                "#.......#",
                "#########",
            ],
            at=(3, 1),
        )
        m, rng = StarterMemory(), random.Random(0)
        seen = []
        for _ in range(12):
            w.pos = walk_step(w, m, rng)
            seen.append(w.pos)
            if w.pos == (8, 1):
                break
        self.assertEqual(w.pos, (8, 1))
        self.assertIn((4, 5), seen)

    def test_follows_saved_path(self):
        w = world([".....", ".....", "....."], at=(1, 1))
        m = StarterMemory(path=[(2, 1), (3, 1)])
        self.assertEqual(walk_step(w, m, random.Random(0)), (2, 1))
        self.assertEqual(m.path, [(3, 1)])

    def test_drops_path_whose_next_step_is_blocked(self):
        w = world(["..#..", ".....", "....."], at=(1, 1))
        m = StarterMemory(path=[(2, 0), (3, 0)])
        step = walk_step(w, m, random.Random(0))
        self.assertNotEqual(step, (2, 0))
        self.assertNotIn((3, 0), m.path)


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

    def test_rejected_intent_rereads_position(self):
        w = world(["...", "...", "..."], at=(1, 1))
        m = StarterMemory(need_self=False, need_position=False, path=[(2, 2)])
        apply_tick(w, m, {"tick": 5, "intent_results": [{"outcome": "rejected"}]})
        self.assertTrue(m.need_position)
        self.assertEqual(m.path, [])
        self.assertEqual(choose_call(w, m), "position")


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


class FakeClient:
    def __init__(self, world_error=None, create_error=None):
        self.world_error, self.create_error = world_error, create_error
        self.ticks = []

    def world(self, cid):
        if self.world_error:
            raise self.world_error
        return {"code": "sandbox", "tick_rate_hz": 1}

    def tick(self, cid, intents, snapshot_version=None):
        self.ticks.append(intents)
        return {"tick": 9}

    def create_character(self, world, name, avatar, model_agent):
        if self.create_error:
            raise self.create_error
        return {"id": 42}


class StarterCommandTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.object(config, "STATE_DIR", Path(tmp.name))
        patch.start()
        self.addCleanup(patch.stop)
        self.cfg = CharacterConfig("T", "default", "x", "sandbox", Policy(), Path("t.toml"))
        self.out = io.StringIO()

    def quiet(self, fn, *args):
        with redirect_stdout(self.out), redirect_stderr(self.out):
            return fn(*args)

    def test_create_saves_id_then_skips(self):
        self.assertEqual(self.quiet(create, FakeClient(), [self.cfg]), 0)
        self.assertEqual(config.load_state(self.cfg)["character_id"], 42)
        self.assertEqual(self.quiet(create, FakeClient(create_error=ApiError(500, "x")), [self.cfg]), 0)
        self.assertIn("already created", self.out.getvalue())

    def test_create_failure_exits_nonzero(self):
        self.assertEqual(self.quiet(create, FakeClient(create_error=ApiError(400, "bad")), [self.cfg]), 1)
        self.assertIsNone(config.load_state(self.cfg))

    def test_run_needs_create_first(self):
        self.assertEqual(self.quiet(run, FakeClient(), [self.cfg]), 2)

    def test_run_exits_nonzero_when_a_character_stops_on_error(self):
        config.save_state(self.cfg, {"character_id": 42, "world": "sandbox"})
        self.assertEqual(self.quiet(run, FakeClient(world_error=ApiError(401, "unauthorized")), [self.cfg]), 1)

    def test_main_needs_api_key_and_readable_file(self):
        with mock.patch.dict("os.environ", {"AGENTREALM_API_KEY": ""}):
            self.assertEqual(self.quiet(main, ["run", "characters/starter.toml"]), 2)
            self.assertEqual(self.quiet(main, ["--api-key", "k", "run", "missing.toml"]), 2)

    def test_step_tick_sends_decision_and_applies_response(self):
        client = FakeClient()
        r = StarterRunner(self.cfg, client, 42, threading.Event(), out=lambda _: None)
        self.addCleanup(r.trace.close)
        r.world = world([".....", ".....", "....."], at=(1, 1))
        r.mem.need_self = r.mem.need_position = False
        r.step("tick")
        self.assertEqual(client.ticks[0][0]["verb"], "SetPosition")
        self.assertEqual(r.world.tick, 9)
