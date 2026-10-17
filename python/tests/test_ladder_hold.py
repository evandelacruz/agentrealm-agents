"""Lorraine never holds in silence (A168): a goal with no step re-targets, blocks, or asks."""

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agentrealm_adk import config
from agentrealm_adk.config import CharacterConfig, Policy
from agentrealm_adk.navigation import stuck
from agentrealm_adk.world import Entity
from ladder import act, play, reflexes
from ladder.act import Act
from ladder.ask import Planner
from ladder.play import STALL_ROUNDS, LadderRunner
from tests.test_ladder_act import ctx, nearer, open_ground, short
from tests.test_ladder_ask import FakeLLM, Clock
from tests.test_ladder_goals import MAP, geared, hostile, knowledge, world
from tests.test_starter_agent import FakeClient

GATHER = {"op": "gather_gems", "count": 30}
GEM = (12, 5)


def gem_beside_two_hostiles(w):
    """One gem taken; the next lies with two rats 1 and 3 cells from it."""
    w.gems = 1
    w.entities.append(Entity("supply", 9, GEM, "gem"))
    hostile(w, 50, (13, 5))
    hostile(w, 51, (12, 8))
    return w


def no_step_to(cell):
    """``guided_step`` with no step toward ``cell``: the walk's hostile-ground hold, as the live run's."""
    real = act.guided_step

    def step(m, w, goal, target, *a, **kw):
        return None if target == cell else real(m, w, goal, target, *a, **kw)

    return mock.patch.object(act, "guided_step", side_effect=step)


class GatherTargetTest(unittest.TestCase):
    """Gather takes the next-best target with a step, hostile ground last (A168)."""

    def test_the_live_repro_re_targets_within_one_round(self):
        w = gem_beside_two_hostiles(open_ground(world(health=8)))
        w.view.tiles[(2, 5)] = "grass"
        a = act.act(GATHER, ctx(w))
        self.assertTrue(a.intents)
        self.assertTrue(nearer(w, a, (2, 5)))  # the grass clear of them, not the gem beside them

    def test_with_only_the_gem_beside_them_she_still_sends_or_is_over(self):
        w = gem_beside_two_hostiles(open_ground(world(health=8)))
        a = act.act(GATHER, ctx(w))
        self.assertTrue(a.intents or a.over)

    def test_a_target_beside_hostiles_is_taken_last_when_her_margin_covers_it(self):
        w = gem_beside_two_hostiles(open_ground(geared(health=10)))
        targets = act.gather_targets(ctx(w))
        self.assertEqual(targets[0].cell, GEM)  # the only one, and covered
        self.assertEqual({e.id for e in targets[0].foes}, {50, 51})
        w.view.tiles[(2, 5)] = "grass"
        self.assertEqual([t.cell for t in act.gather_targets(ctx(w))], [(2, 5), GEM])

    def test_a_target_with_no_step_is_given_up_and_the_next_taken(self):
        w = open_ground(world())
        w.gems = 1
        w.entities.append(Entity("supply", 9, GEM, "gem"))
        w.view.tiles[(2, 5)] = "grass"
        c = ctx(w)
        with no_step_to(GEM):
            a = act.act(GATHER, c)
        self.assertTrue(nearer(w, a, (2, 5)))
        self.assertTrue(stuck.backed_off(c.mem, act.GATHER, MAP, GEM, w.tick))
        self.assertNotIn(GEM, [t.cell for t in act.gather_targets(c)])

    def test_with_no_target_that_has_a_step_gather_is_over_with_why(self):
        w = open_ground(world())
        w.entities.append(Entity("supply", 9, GEM, "gem"))
        op = {**GATHER, "x": 5, "y": 5, "radius": 1}  # no unseen ground to explore there
        with no_step_to(GEM):
            a = act.act(op, ctx(w))
        self.assertTrue(a.over)
        self.assertIn("no gather target has a step", a.why)

    def test_a_walk_with_no_step_is_over_and_its_target_given_up(self):
        w = open_ground(world())
        c = ctx(w)
        with no_step_to((9, 5)):
            a = act.act({"op": "travel", "to": "point", "x": 9, "y": 5}, c)
        self.assertTrue(a.over)
        self.assertTrue(stuck.backed_off(c.mem, "lorraine:travel:point", MAP, (9, 5), w.tick))


class LoopHoldTest(unittest.TestCase):
    """The loop never ends a round empty twice the same way, and blocks a stalled goal."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.object(config, "STATE_DIR", Path(tmp.name))
        patch.start()
        self.addCleanup(patch.stop)

    def runner(self, w, planner=None) -> LadderRunner:
        cfg = CharacterConfig("T", "sandbox", Policy(), Path("t.toml"))
        r = LadderRunner(cfg, FakeClient(), 42, threading.Event(), planner or Planner.off(),
                         knowledge=knowledge(door_need="fords", sells={"raft": 30}), out=lambda *_: None)
        self.addCleanup(r.trace.close)
        r.world = w
        return r

    def gathering(self, planner=None) -> LadderRunner:
        w = open_ground(geared())
        r = self.runner(w, planner)
        r.decide()
        self.assertEqual(r.turn.goal.key, "gather")
        return r

    def test_every_target_out_of_reach_blocks_the_gather_and_asks_the_planner(self):
        fake = FakeLLM('{"op": {"op": "gather_gems", "count": 30}}', '{"cannot": "no ground", "until": "held"}')
        r = self.gathering(Planner(client=fake, clock=Clock(), tokens_per_min=10**9))
        if r.call is not None:
            r.call.wait()
        r.planner.clock.now += 60  # the call budget has room again
        with mock.patch.object(act, "guided_step", return_value=None):
            for _ in range(STALL_ROUNDS):
                r.decide()
                if r.call is not None:
                    r.call.wait()
            self.assertIn("gather", r.blocks)
        self.assertIn("every op for it is over", fake.messages[-1][-1]["content"])
        self.assertEqual(len(fake.messages), 2)  # asked once about the blocked gather, saying why
        self.assertIn("every way down is blocked", fake.messages[-1][-1]["content"])
        self.assertEqual(r.blocks["gather"].reason, "no ground")  # its answer stands

    def test_the_same_empty_round_twice_ends_the_op(self):
        r = self.gathering()
        with mock.patch.object(play, "act", return_value=Act(None, "nothing to do here")):
            first = r.decide()
            self.assertTrue(first.stalled)
            r.decide()
        self.assertIn("gather", r.blocks)  # its op over, the tree's op the same: nothing left to work it
        self.assertIn("nothing to do here, again", r.blocks["gather"].reason)

    def test_the_stall_guard_blocks_after_stall_rounds_and_asks_once_saying_why(self):
        r = self.gathering()
        whys = iter(f"nothing sent, try {i}" for i in range(100))
        with mock.patch.object(play, "act", side_effect=lambda op, c: Act(None, next(whys))):
            for i in range(STALL_ROUNDS - 1):
                self.assertTrue(r.decide().stalled)
            self.assertNotIn("gather", r.blocks)
            r.decide()
        self.assertIn("gather", r.blocks)
        self.assertIn("stalled", r.blocks["gather"].reason)
        self.assertIn("stalled", r.note)
        start = mock.Mock(return_value=None)
        with mock.patch.object(r.planner, "start", start):
            r.decide()
        self.assertEqual(start.call_count, 1)  # asked once, with why the gather was blocked
        self.assertIn("stalled", start.call_args.args[2])

    def test_with_every_way_down_blocked_an_op_for_the_blocked_node_lifts_it(self):
        fake = FakeLLM('{"op": {"op": "gather_gems", "count": 30, "x": 2, "y": 5}}')
        r = self.gathering(Planner(client=fake, clock=Clock(), tokens_per_min=10**9))
        if r.call is not None:
            r.call.wait()
            r.decide()
        fake.replies.append('{"op": {"op": "gather_gems", "count": 30, "x": 2, "y": 5}}')
        r.blocks.mark("gather", "stalled: test", r.world.tick)
        r.own_blocks.add("gather")
        r.planner.clock.now += 60  # the call budget has room again
        r.goal_op()
        if r.call is not None:
            r.call.wait()
            r.goal_op()
        self.assertIn("every way down is blocked", fake.messages[-1][-1]["content"])
        self.assertIn("goal: gather", fake.messages[-1][-1]["content"])
        self.assertNotIn("gather", r.blocks)
        self.assertEqual(r.op, {"op": "gather_gems", "count": 30, "x": 2, "y": 5})

    def test_a_round_that_sends_ends_the_run_of_stalls(self):
        r = self.gathering()
        with mock.patch.object(play, "act", side_effect=lambda op, c: Act(None, f"nothing {r.world.tick}")):
            for _ in range(STALL_ROUNDS - 1):
                r.world.tick += 1
                r.decide()
        self.assertEqual(r.stalls, STALL_ROUNDS - 1)
        r.decide()  # the tree's own gather sends a walk
        self.assertEqual(r.stalls, 0)

    def test_a_tick_record_names_the_goal_the_op_the_act_and_the_reflex(self):
        r = self.gathering()
        r.tick()
        short(r.world)
        r.mem.pending_intents = None
        r.tick()
        r.trace.close()
        lines = [json.loads(x) for x in Path(r.cfg.trace_path(42)).read_text().splitlines()]
        ticks = [x for x in lines if x["call"] == "tick"]
        for rec in ticks:
            for key in ("goal", "op", "act", "reflex"):
                self.assertIn(key, rec)
            self.assertEqual(set(rec["act"]), {"why", "sent", "stalled", "stalls"})
        self.assertEqual(ticks[0]["goal"], "gather")
        self.assertEqual(ticks[0]["op"]["op"], "gather_gems")
        self.assertIsNone(ticks[0]["reflex"])
        self.assertIn(ticks[-1]["reflex"], [f.__name__ for f in reflexes.REFLEXES])


if __name__ == "__main__":
    unittest.main()
