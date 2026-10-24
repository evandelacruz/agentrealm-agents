"""A region's gem-cache oddity is a gather target for Lorraine, priced like any other (A172)."""

import unittest

from agentrealm_adk import gem_caches
from agentrealm_adk.break_memory import record_attempt
from agentrealm_adk.break_results import note_break_use, resolve_pending_break
from agentrealm_adk.knowledge_base import KnowledgeBase
from agentrealm_adk.memory import Memory
from agentrealm_adk.oddities import RETURN_SECONDS
from agentrealm_adk.world import Entity
from ladder import act
from ladder.ask import node_state
from ladder.goals import Reading
from tests.test_ladder_act import ctx, first_step, nearer
from tests.test_ladder_goals import GATHER, MAP, world

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

    def test_its_value_is_learned_from_breaks_with_the_reference_as_prior(self):
        w, k = field_with_a_rock(), kb()
        self.assertAlmostEqual(gem_caches.expected_cache(k, MAP, ROCK), 22 / 3)
        gem_caches.oddities_here(w, k)
        w.entities = [Entity("supply", 70, ROCK, "gem_cache_10")]
        gem_caches.note_caches(w, k)
        gem_caches.note_caches(w, k)  # the same cache seen again is one drop
        self.assertEqual(gem_caches.drops(k, MAP, ROCK), [10])
        self.assertAlmostEqual(gem_caches.expected_cache(k, MAP, ROCK), (10 + 22 / 3) / 2)

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


if __name__ == "__main__":
    unittest.main()
