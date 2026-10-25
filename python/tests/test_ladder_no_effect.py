"""An act that changes nothing is not repeated: its target's value drops and gather moves on (A173)."""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agentrealm_adk import config
from agentrealm_adk.break_memory import (
    NOT_BY_THESE,
    REGROWING,
    REGROWTH_TICKS,
    UNBREAKABLE,
    lift_no_yield,
    no_yield,
    no_yield_cells,
    record_attempt,
)
from agentrealm_adk.break_results import no_yield_why, note_no_effect_strike
from agentrealm_adk.config import CharacterConfig, Policy
from agentrealm_adk.gem_yield import record_cut
from agentrealm_adk.knowledge_base import KnowledgeBase
from agentrealm_adk.memory import Memory
from agentrealm_adk.value import struck_value
from agentrealm_agent.states import gather_outcome
from ladder import act, play
from ladder.act import Act
from ladder.ask import Planner
from ladder.play import NO_EFFECT_REPEATS, LadderRunner
from tests.test_gem_yield import field_world
from tests.test_ladder_act import ctx, nearer, open_ground
from tests.test_ladder_goals import MAP, world
from tests.test_starter_agent import FakeClient

GATHER = {"op": "gather_gems", "count": 30}
HERE, NEXT = (5, 5), (9, 5)  # the grass she stands on, and the next grass along


def kb() -> KnowledgeBase:
    return KnowledgeBase.empty("sandbox")


def two_grass(**kw):
    """Dirt round her, grass on her own cell and four steps off; a sword armed."""
    w = open_ground(world(**kw))
    w.view.tiles[HERE] = w.view.tiles[NEXT] = "grass"
    return w


def use_on(p) -> dict:
    return {"verb": "Use", "target": {"kind": "block", "x": p[0], "y": p[1]}}


class NoteTest(unittest.TestCase):
    """What a strike that changed nothing says about its cell (``break_results.no_yield_why``)."""

    def test_a_cutter_on_grass_that_does_not_cut_notes_it_unbreakable_for_good(self):
        k = kb()
        note = no_yield_why(k, MAP, HERE, "grass", "pocket_knife", 100)
        self.assertEqual((note.why, note.until), (UNBREAKABLE, None))
        note_no_effect_strike(k, two_grass(), HERE, "grass", "pocket_knife", 100)
        self.assertIsNotNone(no_yield(k, MAP, HERE, 100 + 10**6))

    def test_a_cell_cut_lately_is_regrowing_until_its_timer(self):
        k = kb()
        record_cut(k, MAP, HERE, "grass", 100, False)
        note = no_yield_why(k, MAP, HERE, "grass", "sword", 110)
        self.assertEqual((note.why, note.until), (REGROWING, 100 + REGROWTH_TICKS))
        record_attempt(k, map_id=MAP, pos=NEXT, capability="chop", result="opened", tick=200, code="sword")
        self.assertEqual(no_yield_why(k, MAP, NEXT, "tree", "sword", 210).why, REGROWING)

    def test_a_capability_its_type_does_not_name_is_noted_for_it_only(self):
        k, w = kb(), two_grass()
        w.view.tiles[NEXT] = "tree"
        note = note_no_effect_strike(k, w, NEXT, "tree", "pocket_knife", 100)
        self.assertEqual((note.why, note.capabilities), (NOT_BY_THESE, ("cut",)))
        self.assertIsNotNone(no_yield(k, MAP, NEXT, 100, {"cut"}))
        self.assertIsNone(no_yield(k, MAP, NEXT, 100, {"cut", "chop"}), "a sword may still fell it")

    def test_a_strike_with_nothing_that_breaks_says_nothing(self):
        self.assertIsNone(no_yield_why(kb(), MAP, HERE, "grass", "small_potion", 100))
        self.assertIsNone(no_yield_why(kb(), MAP, HERE, "grass", None, 100))

    def test_a_block_changed_on_the_cell_lifts_its_note(self):
        k = kb()
        note_no_effect_strike(k, two_grass(), HERE, "grass", "sword", 100)
        lift_no_yield(k, [(MAP, NEXT)])
        self.assertEqual(no_yield_cells(k, MAP, 100), {HERE})
        lift_no_yield(k, [(MAP, HERE)])
        self.assertEqual(no_yield_cells(k, MAP, 100), set())

    def test_the_value_of_a_noted_cell_is_nothing_until_it_lapses(self):
        k = kb()
        record_cut(k, MAP, HERE, "grass", 100, False)
        note_no_effect_strike(k, two_grass(), HERE, "grass", "sword", 110)
        self.assertEqual(struck_value(0.2, k, MAP, HERE, 110), 0.0)
        self.assertEqual(struck_value(0.2, k, MAP, HERE, 100 + REGROWTH_TICKS), 0.2)


class GatherTest(unittest.TestCase):
    """Gather reads the note through the value: a noted cell is no target, so it moves on."""

    def test_after_a_cut_with_no_effect_gather_takes_the_next_grass(self):
        w, k = two_grass(), kb()
        a = act.act(GATHER, ctx(w, k))
        self.assertEqual(a.intents[-1], use_on(HERE))
        note_no_effect_strike(k, w, HERE, "grass", "sword", w.tick)
        a = act.act(GATHER, ctx(w, k))
        self.assertNotIn(use_on(HERE), a.intents)
        self.assertTrue(nearer(w, a, NEXT))
        self.assertNotIn(HERE, [t.cell for t in act.gather_targets(ctx(w, k))])

    def test_a_cell_that_regrows_is_a_target_again_after_its_timer(self):
        w, k = two_grass(), kb()
        w.tick = 110
        record_cut(k, MAP, HERE, "grass", 100, False)  # cut, and the terrain read still shows grass
        note_no_effect_strike(k, w, HERE, "grass", "sword", w.tick)
        self.assertNotIn(HERE, [t.cell for t in act.gather_targets(ctx(w, k))])
        w.tick = 100 + REGROWTH_TICKS
        self.assertEqual(act.gather_targets(ctx(w, k))[0].cell, HERE)
        self.assertEqual(act.act(GATHER, ctx(w, k)).intents[-1], use_on(HERE))

    def test_garys_gather_skips_a_noted_cell_too(self):
        w, k, m = field_world(), kb(), Memory()
        note_no_effect_strike(k, w, (1, 1), "grass", "sword", w.tick)
        gather_outcome(w, m, Policy(on_hostile="ignore"), knowledge=k, op={"op": "gather_gems", "count": 5})
        self.assertNotEqual(m.gather_target[1], (1, 1))


class LoopTest(unittest.TestCase):
    """Her loop reads each send against what it was for (``LadderRunner.judge``)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.object(config, "STATE_DIR", Path(tmp.name))
        patch.start()
        self.addCleanup(patch.stop)
        cfg = CharacterConfig("T", "sandbox", Policy(), Path("t.toml"))
        self.r = LadderRunner(cfg, FakeClient(), 42, threading.Event(), Planner.off(), knowledge=kb(), out=lambda *_: None)
        self.addCleanup(self.r.trace.close)
        self.r.world = self.w = two_grass()
        self.r.op = GATHER
        self.queue = 0

    def round(self, outcome="applied_no_effect", changed=None, code="malformed_intent"):
        """One decision, and its queue run to its end with ``outcome`` on each intent."""
        r, w = self.r, self.w
        with mock.patch.object(r, "goal_op", return_value=r.op), mock.patch.object(play, "reflex", return_value=None):
            d = r.decide()
        if not d.intents:
            return d
        self.queue += 1
        q = self.queue
        refused = {"rejection": {"code": code}} if outcome == "rejected" else {}
        results = [{"queue_id": q, "index": i, "outcome": outcome, "tick": w.tick, **refused}
                   for i, x in enumerate(d.intents) if x["verb"] != "Wait"]
        events = [{"tick": w.tick, "events": [{"kind": "BlockChanged", "map_id": MAP, "x": p[0], "y": p[1],
                                                "block_type": "dirt"} for p in changed or []]}]
        r.apply({"tick": w.tick, "queue_id": q, "intent_results": results, "events_by_tick": events,
                 "finished_queue": {"queue_id": q, "length": len(d.intents)}}, d)
        w.tick += 20
        return d

    def test_her_cut_with_no_effect_is_not_sent_again_on_that_cell(self):
        d = self.round()
        self.assertEqual(d.intents[-1], use_on(HERE))
        self.assertEqual(no_yield(self.r.knowledge, MAP, HERE, self.w.tick).why, UNBREAKABLE)
        d = self.round()
        self.assertNotIn(use_on(HERE), d.intents)
        self.assertTrue(any(i["verb"] == "Step" for i in d.intents), d.why)

    def test_a_cut_that_lands_with_no_block_changed_is_noted_too(self):
        self.round(outcome="applied")
        self.assertEqual(no_yield(self.r.knowledge, MAP, HERE, self.w.tick).why, UNBREAKABLE)

    def test_a_cut_that_opens_its_cell_is_not_noted(self):
        self.round(outcome="applied", changed=[HERE])
        self.assertIsNone(no_yield(self.r.knowledge, MAP, HERE, self.w.tick))

    def test_the_same_act_with_no_effect_n_times_ends_the_op(self):
        repeat = Act([use_on(HERE)], "cut (5, 5)")
        with mock.patch.object(play, "act", return_value=repeat):
            for _ in range(NO_EFFECT_REPEATS):
                self.assertEqual(self.round().intents, [use_on(HERE)])
            d = self.round()
        self.assertFalse(d.intents)
        self.assertIn(GATHER, self.r.done)

    def test_an_act_that_changes_something_starts_the_count_again(self):
        repeat = Act([use_on(HERE)], "cut (5, 5)")
        with mock.patch.object(play, "act", return_value=repeat):
            for _ in range(NO_EFFECT_REPEATS - 1):
                self.round()
            self.round(outcome="applied", changed=[HERE])
            for _ in range(NO_EFFECT_REPEATS):
                self.assertEqual(self.round().intents, [use_on(HERE)])
        self.assertNotIn(GATHER, self.r.done)

    def test_a_refusal_that_says_not_yet_is_no_verdict(self):
        repeat = Act([use_on(HERE)], "cut (5, 5)")
        r = self.r
        with mock.patch.object(play, "act", return_value=repeat):
            for _ in range(NO_EFFECT_REPEATS + 2):
                d = self.round(outcome="rejected", code="attack_cooldown")
                self.assertEqual(d.intents, [use_on(HERE)])
        self.assertIsNone(r.no_effect)

    def test_a_walk_is_judged_by_the_stuck_ladder_not_here(self):
        walk = Act([{"verb": "Step", "direction": "right"}], "lorraine:gather → (9, 5)")
        with mock.patch.object(play, "act", return_value=walk):
            for _ in range(NO_EFFECT_REPEATS + 1):
                self.assertTrue(self.round(outcome="rejected").intents)
        self.assertNotIn(GATHER, self.r.done)


if __name__ == "__main__":
    unittest.main()
