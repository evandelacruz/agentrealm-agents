"""A region's gem-cache oddity is a gather target for Lorraine, priced like any other (A172)."""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agentrealm_adk import config
from agentrealm_adk.config import CharacterConfig, Policy
from agentrealm_adk import gem_caches
from agentrealm_adk.break_memory import record_attempt
from agentrealm_adk.break_results import note_break_use, resolve_pending_break
from agentrealm_adk.knowledge_base import KnowledgeBase
from agentrealm_adk.memory import Memory
from agentrealm_adk.oddities import ODDITIES_SHOWN, RETURN_SECONDS
from agentrealm_adk.world import Entity
from ladder import act
from ladder.ask import Planner, node_state
from ladder.goals import Reading
from ladder.play import Decision, LadderRunner
from tests.test_ladder_act import ctx, first_step, nearer
from tests.test_ladder_goals import GATHER, MAP, world
from tests.test_starter_agent import FakeClient

ROCK = (7, 5)  # two steps off where she stands, (5, 5)
OP = {"op": "gather_gems", "count": 20}
HZ = 10


def field_with_a_rock(**kw):
    """Grass all round her, one rock alone in it (an oddity, A134), and the pocket knife armed."""
    w = world(**{"armed_code": "pocket_knife", **kw})
    for x in range(16):
        for y in range(12):
            w.view.tiles[(x, y)] = "grass"
    w.view.tiles[ROCK] = "rock"
    w.perception = 5
    return w


def kb() -> KnowledgeBase:
    return KnowledgeBase.empty("sandbox")


def use_on(p) -> dict:
    return {"verb": "Use", "target": {"kind": "block", "x": p[0], "y": p[1]}}


class CacheTargetTest(unittest.TestCase):
    """Gather prices the oddity's expected cache per step against grass."""

    def test_with_a_knife_gather_takes_the_oddity_over_grass(self):
        w, k = field_with_a_rock(), kb()
        a = act.act(OP, ctx(w, k))
        self.assertNotIn(use_on((5, 6)), a.intents)  # not the grass in reach
        self.assertTrue(nearer(w, a, ROCK), first_step(a))
        w.pos = (6, 5)
        c = ctx(w, k)
        a = act.act(OP, c)
        self.assertEqual(a.intents[-1], use_on(ROCK))
        self.assertEqual(c.mem.break_pending, (MAP, ROCK, "cut"))

    def test_every_target_is_ranked_by_its_net(self):
        """A170: the oddity's expected cache beats a gem five steps off, which beats the grass."""
        w, k = field_with_a_rock(), kb()
        w.entities = [Entity("supply", 9, (5, 10), "gem")]  # five steps off, grass and the rock nearer
        cells = [t.cell for t in act.gather_targets(ctx(w, k))]
        self.assertEqual(cells[:2], [ROCK, (5, 10)])

    def test_its_value_is_learned_from_breaks_with_the_reference_as_prior(self):
        w, k = field_with_a_rock(), kb()
        self.assertAlmostEqual(gem_caches.expected_cache(k, MAP, ROCK), 22 / 3)
        gem_caches.oddities_here(w, k)
        w.entities = [Entity("supply", 70, ROCK, "gem_cache_10")]
        gem_caches.note_caches(w, k)
        gem_caches.note_caches(w, k)  # the same cache seen again is one drop
        self.assertEqual(gem_caches.drops(k, MAP, ROCK), [10])
        self.assertAlmostEqual(gem_caches.expected_cache(k, MAP, ROCK), (10 + 22 / 3) / 2)

    def test_a_break_that_dropped_nothing_counts_as_a_zero(self):
        w, k = field_with_a_rock(pos=(6, 5)), kb()
        gem_caches.oddities_here(w, k)
        w.tick = 100
        record_attempt(k, map_id=MAP, pos=ROCK, capability="cut", result="opened", tick=100)
        gem_caches.note_caches(w, k)
        self.assertEqual(gem_caches.drops(k, MAP, ROCK), [])  # the cache may still come
        w.tick = 100 + gem_caches.CACHE_SEEN_TICKS
        gem_caches.note_caches(w, k)
        gem_caches.note_caches(w, k)  # settled once
        self.assertEqual(gem_caches.drops(k, MAP, ROCK), [0])
        self.assertAlmostEqual(gem_caches.expected_cache(k, MAP, ROCK), 22 / 6)

    def test_a_cache_seen_after_a_break_settles_it_with_no_zero(self):
        w, k = field_with_a_rock(pos=(6, 5)), kb()
        gem_caches.oddities_here(w, k)
        record_attempt(k, map_id=MAP, pos=ROCK, capability="cut", result="opened", tick=100)
        w.tick = 101
        w.entities = [Entity("supply", 70, ROCK, "gem_cache_5")]
        gem_caches.note_caches(w, k)
        w.entities, w.tick = [], 100 + gem_caches.CACHE_SEEN_TICKS  # taken
        gem_caches.note_caches(w, k)
        self.assertEqual(gem_caches.drops(k, MAP, ROCK), [5])

    def test_a_tool_breaks_with_no_swing_cooldown(self):
        w, k = field_with_a_rock(armed_code="matches", pos=(6, 5)), kb()
        record_attempt(k, map_id=MAP, pos=ROCK, capability="cut", result="applied_no_effect", tick=1)
        c = ctx(w, k)
        c.mem.last_use_tick = w.tick - 1  # a swing just went out
        a = act.act(OP, c)
        self.assertEqual(a.intents, [use_on(ROCK)])
        self.assertEqual(c.mem.break_pending, (MAP, ROCK, "burn"))

    def test_not_while_its_regrow_timer_runs(self):
        w, k = field_with_a_rock(), kb()
        gem_caches.oddities_here(w, k)  # seen and remembered
        w.tick = 100
        record_attempt(k, map_id=MAP, pos=ROCK, capability="cut", result="opened", tick=100)
        w.view.tiles[ROCK] = "grass"  # broken: the cell opened
        self.assertEqual(gem_caches.cache_targets(w, k, HZ), [])
        w.pos = (14, 5)  # out of sight: its timer says when it is back
        self.assertEqual(gem_caches.cache_targets(w, k, HZ), [])
        w.tick = 100 + RETURN_SECONDS * HZ
        self.assertEqual([t.pos for t in gem_caches.cache_targets(w, k, HZ)], [ROCK])
        w.pos = (6, 5)  # in sight and not grown back yet
        self.assertEqual(gem_caches.cache_targets(w, k, HZ), [])

    def test_not_while_a_cache_lies_on_it(self):
        w, k = field_with_a_rock(), kb()
        w.entities = [Entity("supply", 70, ROCK, "gem_cache_7")]
        self.assertEqual(gem_caches.cache_targets(w, k, HZ), [])
        a = act.act(OP, ctx(w, k))
        self.assertNotIn(use_on(ROCK), a.intents or [])  # the cache is taken, the rock not broken

    def test_one_needing_a_capability_she_lacks_is_not_walked_to_and_the_planner_is_told(self):
        w, k = field_with_a_rock(), kb()
        record_attempt(k, map_id=MAP, pos=ROCK, capability="cut", result="applied_no_effect", tick=1)
        gem_caches.oddities_here(w, k)
        self.assertEqual(gem_caches.cache_targets(w, k, HZ), [])
        a = act.act(OP, ctx(w, k))
        self.assertEqual(a.intents[-1]["verb"], "Use")
        self.assertNotEqual(a.intents[-1], use_on(ROCK))
        self.assertNotIn("Step", [i["verb"] for i in a.intents])  # grass in reach, no walk
        line = node_state(GATHER, Reading(w, k))["gem_caches_out_of_reach"]
        self.assertIn("rock at 7,5", line)
        self.assertIn("one of blast, burn, chop, smash", line)

    def test_the_planner_is_told_only_the_nearest_few(self):
        w, k = field_with_a_rock(), kb()
        k.extra["oddities"] = {f"{MAP},5,{y}": {"block": "rock", "seen": 0} for y in range(30, 10, -1)}
        for y in range(11, 31):  # the knife failed on each
            record_attempt(k, map_id=MAP, pos=(5, y), capability="cut", result="applied_no_effect", tick=1)
        lines = gem_caches.needs_lines(w, k)
        self.assertEqual(len(lines), ODDITIES_SHOWN)
        self.assertIn("rock at 5,11", lines[0])

    def test_one_opened_before_needs_that_capability(self):
        w, k = field_with_a_rock(armed_code="mallet"), kb()
        record_attempt(k, map_id=MAP, pos=ROCK, capability="blast", result="opened", tick=1)
        gem_caches.oddities_here(w, k)
        self.assertEqual(gem_caches.cache_targets(w, k, HZ), [])
        self.assertIn("needs blast", gem_caches.needs_lines(w, k)[0])


class BreakResultTest(unittest.TestCase):
    """A break's result is settled into break memory on every agent's loop."""

    def test_no_effect_marks_the_pair_failed_and_a_change_marks_it_opened(self):
        w, k, m = field_with_a_rock(), kb(), Memory()
        m.break_pending = (MAP, ROCK, "cut")
        note_break_use(k, m, w, use_on(ROCK), {"outcome": "applied_no_effect", "tick": 5}, ROCK, [use_on(ROCK)])
        self.assertIsNone(m.break_pending)
        self.assertEqual(k.breaks[f"{MAP},7,5,cut"]["result"], "applied_no_effect")
        m.break_pending = (MAP, ROCK, "smash")
        w.changed_blocks = [(MAP, ROCK)]
        resolve_pending_break(k, m, w)
        self.assertIsNone(m.break_pending)
        self.assertEqual(k.breaks[f"{MAP},7,5,smash"]["result"], "opened")


class LoopTest(unittest.TestCase):
    """Her loop settles a break and files the cache it drops (``LadderRunner.apply``)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.object(config, "STATE_DIR", Path(tmp.name))
        patch.start()
        self.addCleanup(patch.stop)
        cfg = CharacterConfig("T", "sandbox", Policy(), Path("t.toml"))
        self.r = LadderRunner(cfg, FakeClient(), 42, threading.Event(), Planner.off(), knowledge=kb(), out=lambda *_: None)
        self.addCleanup(self.r.trace.close)
        self.r.world = self.w = field_with_a_rock(pos=(6, 5))
        gem_caches.oddities_here(self.w, self.r.knowledge)

    def sent(self, intents: list[dict]) -> dict:
        d = Decision(intents, "break", "gather_gems")
        self.r.mem.break_pending = (MAP, ROCK, "cut")
        self.r.apply({"tick": self.w.tick, "queue_id": 7}, d)
        return {"tick": self.w.tick, "queue_id": 7}

    def test_a_break_that_opens_files_it_and_its_cache(self):
        r, w = self.r, self.w
        base = self.sent([use_on(ROCK)])
        r.apply({**base, "intent_results": [{"queue_id": 7, "index": 0, "outcome": "applied", "tick": w.tick}],
                 "events_by_tick": [{"tick": w.tick, "events": [
                     {"kind": "BlockChanged", "map_id": MAP, "x": ROCK[0], "y": ROCK[1], "block_type": "grass"}]}]},
                Decision(None, "poll", "gather_gems"))
        self.assertIsNone(r.mem.break_pending)
        self.assertEqual(r.knowledge.breaks[f"{MAP},7,5,cut"]["result"], "opened")
        w.entities = [Entity("supply", 70, ROCK, "gem_cache_7")]
        r.apply({"tick": w.tick}, Decision(None, "poll", "gather_gems"))
        self.assertEqual(gem_caches.drops(r.knowledge, MAP, ROCK), [7])

    def test_a_rejection_ahead_of_the_break_drops_it_unfiled(self):
        r, w = self.r, self.w
        base = self.sent([{"verb": "Wait", "ticks": 2}, use_on(ROCK)])
        r.apply({**base, "intent_results": [{"queue_id": 7, "index": 0, "outcome": "rejected", "tick": w.tick,
                                             "rejection": {"code": "busy"}}]}, Decision(None, "poll", "gather_gems"))
        self.assertIsNone(r.mem.break_pending)  # a later change there is not hers
        self.assertNotIn(f"{MAP},7,5,cut", r.knowledge.breaks)


    def test_a_queue_replaced_as_its_break_opens_still_files_it(self):
        r, w = self.r, self.w
        self.sent([{"verb": "Wait", "ticks": 2}, use_on(ROCK)])
        results = [{"queue_id": 7, "index": i, "outcome": "applied", "tick": w.tick} for i in (0, 1)]
        changed = [{"tick": w.tick, "events": [
            {"kind": "BlockChanged", "map_id": MAP, "x": ROCK[0], "y": ROCK[1], "block_type": "grass"}]}]
        r.apply({"tick": w.tick, "queue_id": 8, "intent_results": results, "events_by_tick": changed},
                Decision([{"verb": "Step", "direction": "W"}], "flee", "flee"))
        self.assertIsNone(r.mem.break_pending)
        self.assertEqual(r.knowledge.breaks[f"{MAP},7,5,cut"]["result"], "opened")

    def test_a_queue_replaced_after_its_break_ran_keeps_it_for_the_change_to_come(self):
        r, w = self.r, self.w
        self.sent([use_on(ROCK)])
        r.apply({"tick": w.tick, "queue_id": 8,
                 "intent_results": [{"queue_id": 7, "index": 0, "outcome": "applied", "tick": w.tick}]},
                Decision([{"verb": "Step", "direction": "W"}], "flee", "flee"))
        self.assertEqual(r.mem.break_pending, (MAP, ROCK, "cut"))

    def test_a_queue_that_sends_the_break_again_keeps_it_though_the_old_one_was_refused(self):
        r, w = self.r, self.w
        self.sent([use_on(ROCK)])
        refused = [{"queue_id": 7, "index": 0, "outcome": "rejected", "tick": w.tick, "rejection": {"code": "attack_cooldown"}}]
        r.apply({"tick": w.tick, "queue_id": 8, "intent_results": refused},
                Decision([{"verb": "Wait", "ticks": 1}, use_on(ROCK)], "break", "gather_gems"))
        self.assertEqual(r.mem.break_pending, (MAP, ROCK, "cut"))
        w.tick += 2
        r.apply({"tick": w.tick, "events_by_tick": [{"tick": w.tick, "events": [
            {"kind": "BlockChanged", "map_id": MAP, "x": ROCK[0], "y": ROCK[1], "block_type": "grass"}]}]},
            Decision(None, "poll", "gather_gems"))
        self.assertEqual(r.knowledge.breaks[f"{MAP},7,5,cut"]["result"], "opened")

    def test_a_queue_replaced_before_the_break_ran_drops_it(self):
        r, w = self.r, self.w
        self.sent([{"verb": "Wait", "ticks": 2}, use_on(ROCK)])
        r.apply({"tick": w.tick, "queue_id": 8}, Decision([{"verb": "Step", "direction": "W"}], "flee", "flee"))
        self.assertIsNone(r.mem.break_pending)


if __name__ == "__main__":
    unittest.main()
