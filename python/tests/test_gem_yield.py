"""A63: gem yield per region, learned from the agent's own cuts. Fake maps and cells only."""

import random
import threading
import unittest
from types import SimpleNamespace

from agentrealm_agent import gem_yield
from agentrealm_agent.config import Policy
from agentrealm_agent.directives import PARAM_DEFAULTS, Directives
from agentrealm_agent.gem_yield import BARREN_MIN_CUTS, GEM_WINDOW_TICKS, GemYieldTracker, record_cut
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.memory import Memory
from agentrealm_agent.plan import Plan, validate_goal_op
from agentrealm_agent.runner import Runner
from agentrealm_agent.states import gather_outcome
from agentrealm_agent.strategist import build_prompt
from agentrealm_agent.world import Entity, WorldModel
from agentrealm_agent.zone_discovery import apply_zone

MAP = 7


def kb() -> KnowledgeBase:
    return KnowledgeBase.empty("fake-world")


def world(at=(1, 1), gems=0) -> WorldModel:
    w = WorldModel(character_id=1, map_id=MAP, pos=at, perception=5)
    w.alive, w.gems, w.tick = True, gems, 100
    for x in range(6):
        for y in range(6):
            w.view.tiles[(x, y)] = "grass"
    w.terrain_center, w.terrain_map = at, MAP
    return w


def cuts(k: KnowledgeBase) -> list[dict]:
    return k.extra["gem_yield"][str(MAP)]["cuts"]


class AttributionTest(unittest.TestCase):
    def test_gem_that_appears_beside_the_cut_is_credited(self):
        k, w, t = kb(), world(), GemYieldTracker()
        t.note_cut(w, (2, 2), "bush", 100)
        w.tick, w.entities = 102, [Entity("supply", 50, (3, 2), "gem")]
        t.update(w, k)
        self.assertEqual(cuts(k), [{"x": 2, "y": 2, "block": "bush", "tick": 100, "gem": True}])
        self.assertEqual(t.pending, [])

    def test_gem_already_in_view_far_or_priced_is_not_credited(self):
        k, w, t = kb(), world(), GemYieldTracker()
        w.entities = [Entity("supply", 40, (2, 3), "gem")]
        t.note_cut(w, (2, 2), "grass", 100)
        w.entities += [Entity("supply", 41, (5, 5), "gem"), Entity("supply", 42, (2, 1), "gem", gem_price=3)]
        w.tick = 101
        t.update(w, k)
        self.assertEqual(len(t.pending), 1)  # still inside its window
        w.tick = 100 + GEM_WINDOW_TICKS + 1
        t.update(w, k)
        self.assertFalse(cuts(k)[0]["gem"])

    def test_counter_rise_without_a_take_is_credited_once(self):
        k, w, t = kb(), world(gems=4), GemYieldTracker()
        t.note_cut(w, (1, 1), "grass", 100)
        t.note_cut(w, (1, 2), "grass", 101)
        w.tick, w.gems = 102, 5
        t.update(w, k)
        w.tick = 110
        t.update(w, k)
        self.assertEqual([c["gem"] for c in cuts(k)], [True, False])

    def test_counter_rise_after_a_take_is_not_the_cut(self):
        k, w, t = kb(), world(gems=4), GemYieldTracker()
        t.note_cut(w, (1, 1), "grass", 100)
        t.note_take()
        w.tick, w.gems = 110, 5
        t.update(w, k)
        self.assertFalse(cuts(k)[0]["gem"])

    def test_gem_left_on_the_ground_credits_only_one_cut(self):
        k, w, t = kb(), world(), GemYieldTracker()
        t.note_cut(w, (2, 2), "grass", 100)
        t.note_cut(w, (3, 2), "grass", 101)
        w.tick, w.entities = 102, [Entity("supply", 50, (3, 3), "gem")]
        t.update(w, k)
        w.tick = 103
        t.update(w, k)
        w.tick = 110
        t.update(w, k)
        self.assertEqual([c["gem"] for c in cuts(k)], [True, False])
        self.assertEqual(gem_yield.regions(k, MAP)["0,0"]["gems"], 1)

    def test_ground_gem_credit_leaves_the_counter_to_other_cuts(self):
        k, w, t = kb(), world(gems=4), GemYieldTracker()
        t.note_cut(w, (1, 1), "grass", 100)
        t.note_cut(w, (30, 30), "grass", 100)
        w.tick, w.entities = 101, [Entity("supply", 50, (2, 1), "gem")]
        t.update(w, k)
        w.tick, w.gems = 102, 5
        t.update(w, k)
        w.tick = 110
        t.update(w, k)
        self.assertEqual([c["gem"] for c in cuts(k)], [True, True])

    def test_ground_gem_picked_up_is_not_another_cuts_rise(self):
        k, w, t = kb(), world(gems=4), GemYieldTracker()
        t.note_cut(w, (1, 1), "grass", 100)
        t.note_cut(w, (30, 30), "grass", 100)
        w.tick, w.entities = 101, [Entity("supply", 50, (2, 1), "gem")]
        t.update(w, k)
        w.tick, w.gems, w.entities = 102, 5, []  # walked onto it: gone from view, counter up
        t.update(w, k)
        w.tick = 110
        t.update(w, k)
        self.assertEqual([c["gem"] for c in cuts(k)], [True, False])

    def test_ground_gem_gone_without_a_counter_rise_moves_no_baseline(self):
        k, w, t = kb(), world(gems=4), GemYieldTracker()
        t.update(w, k)  # the counter as of the last round trip
        t.note_cut(w, (1, 1), "grass", 100)
        t.note_cut(w, (30, 30), "grass", 100)
        w.tick, w.entities = 101, [Entity("supply", 50, (2, 1), "gem")]
        t.update(w, k)
        w.tick, w.entities = 102, []  # someone else took it: no rise for us
        t.update(w, k)
        w.tick, w.gems = 103, 5  # then our own gem from the second cut
        t.update(w, k)
        self.assertEqual([c["gem"] for c in cuts(k)], [True, True])

    def test_take_earlier_in_the_response_is_not_the_cut(self):
        k, w, t = kb(), world(gems=4), GemYieldTracker()
        t.note_cut(w, (1, 1), "grass", 100, took=True)
        w.tick, w.gems = 110, 5
        t.update(w, k)
        self.assertFalse(cuts(k)[0]["gem"])

    def test_map_change_drops_pending_cuts(self):
        k, w, t = kb(), world(), GemYieldTracker()
        t.note_cut(w, (1, 1), "grass", 100)
        w.map_id, w.tick = MAP + 1, 101
        t.update(w, k)
        self.assertEqual(t.pending, [])
        self.assertNotIn("gem_yield", k.extra)

    def test_death_drops_pending_cuts(self):
        k, w, t = kb(), world(), GemYieldTracker()
        t.note_cut(w, (1, 1), "grass", 100)
        w.alive = False
        t.update(w, k)
        self.assertEqual(t.pending, [])
        self.assertNotIn("gem_yield", k.extra)

    def test_runner_records_an_applied_use_on_a_block(self):
        r = Runner.__new__(Runner)
        r.world, r.mem, r.knowledge, r.acceptance = world(), Memory(), kb(), None
        r.gem_cuts, r._applied_uses, r._applied_take_codes, r._loadout_verbs = GemYieldTracker(), [], [], []
        r.mem.pending = {"verb": "Use", "target": {"kind": "block", "x": 2, "y": 1}}
        r.on_result({"outcome": "applied", "tick": 100}, 0)
        self.assertEqual([(c.pos, c.block, c.took) for c in r.gem_cuts.pending], [((2, 1), "grass", False)])
        r._applied_take_codes = ["berry"]  # a food Take earlier in this response: not a gem rise
        r.mem.pending = {"verb": "Use", "target": {"kind": "block", "x": 2, "y": 2}}
        r.on_result({"outcome": "applied", "tick": 100}, 0)
        self.assertFalse(r.gem_cuts.pending[-1].took)
        r._applied_take_codes.append("gem")  # a gem Take earlier in this response
        r.mem.pending = {"verb": "Use", "target": {"kind": "block", "x": 3, "y": 2}}
        r.on_result({"outcome": "applied", "tick": 100}, 0)
        self.assertTrue(r.gem_cuts.pending[-1].took)

    def test_only_a_gem_take_in_the_window_blocks_the_counter_credit(self):
        r = Runner.__new__(Runner)
        r.world, r.mem, r.knowledge, r.acceptance = world(gems=4), Memory(), kb(), None
        r.gem_cuts, r._applied_uses, r._applied_take_codes, r._loadout_verbs = GemYieldTracker(), [], [], []
        r.gem_cuts.note_cut(r.world, (1, 1), "grass", 100)
        r.world.entities = [Entity("supply", 60, (1, 2), "berry")]
        r.mem.pending = {"verb": "Take", "supply_id": 60}
        r.on_result({"outcome": "applied", "tick": 101}, 0)
        self.assertFalse(r.gem_cuts.pending[0].took)
        r.world.tick, r.world.gems = 102, 5
        r.gem_cuts.update(r.world, r.knowledge)
        self.assertTrue(cuts(r.knowledge)[0]["gem"])


class RegionSummaryTest(unittest.TestCase):
    def test_regions_count_cuts_gems_and_last_tick(self):
        k = kb()
        record_cut(k, MAP, (1, 1), "grass", 10, False)
        record_cut(k, MAP, (3, 4), "bush", 20, True)
        record_cut(k, MAP, (17, 1), "grass", 30, False)
        record_cut(k, MAP, (2, 2), "rock", 40, False)  # not a gather block: no region count
        regions = gem_yield.regions(k, MAP)
        self.assertEqual(regions["0,0"], {"cuts": 2, "gems": 1, "last_tick": 20})
        self.assertEqual(regions["1,0"], {"cuts": 1, "gems": 0, "last_tick": 30})
        self.assertEqual(len(cuts(k)), 4)

    def test_barren_needs_the_threshold_and_no_gem(self):
        k = kb()
        for i in range(BARREN_MIN_CUTS - 1):
            record_cut(k, MAP, (1, 1), "grass", i, False)
        self.assertFalse((0, 0) in gem_yield.barren_regions(k, MAP))
        record_cut(k, MAP, (2, 2), "grass", 99, False)
        self.assertTrue((0, 0) in gem_yield.barren_regions(k, MAP))
        record_cut(k, MAP, (2, 2), "grass", 100, True)
        self.assertFalse((0, 0) in gem_yield.barren_regions(k, MAP))

    def test_summary_lists_best_and_barren_nearby(self):
        k = kb()
        for i in range(BARREN_MIN_CUTS):
            record_cut(k, MAP, (1, 1), "grass", i, False)
        for i, gem in enumerate([True, False, False, False]):
            record_cut(k, MAP, (17, 1), "grass", i, gem)
        record_cut(k, MAP, (33, 1), "bush", 1, True)
        record_cut(k, MAP, (200, 200), "bush", 1, True)  # too far to be nearby
        s = gem_yield.summary(world(at=(2, 2)), k)
        self.assertEqual(s["here"], {"x": 0, "y": 0, "cuts": BARREN_MIN_CUTS, "gems": 0})
        self.assertEqual([(b["x"], b["yield"]) for b in s["best"]], [(32, 1.0), (16, 0.25)])
        self.assertEqual(s["barren"], [{"x": 0, "y": 0, "cuts": BARREN_MIN_CUTS}])

    def test_summary_names_here_only_once_cut_there(self):
        """A63 run 1: an unsampled ``here`` invited the planner to name it on every call."""
        k = kb()
        record_cut(k, MAP, (17, 1), "grass", 1, True)
        self.assertNotIn("here", gem_yield.summary(world(at=(2, 2)), k))
        record_cut(k, MAP, (2, 2), "grass", 2, False)
        self.assertEqual(gem_yield.summary(world(at=(2, 2)), k)["here"], {"x": 0, "y": 0, "cuts": 1, "gems": 0})

    def test_exhausted_cells_are_the_ones_cut_before_regrowth(self):
        k = kb()
        record_cut(k, MAP, (1, 1), "grass", 100, False)
        record_cut(k, MAP, (2, 2), "bush", 100 + gem_yield.REGROW_TICKS, True)
        self.assertEqual(gem_yield.exhausted_cells(k, MAP, 100 + gem_yield.REGROW_TICKS), {(2, 2)})
        self.assertEqual(gem_yield.exhausted_cells(k, MAP, 101), {(1, 1), (2, 2)})
        self.assertEqual(gem_yield.exhausted_cells(None, MAP, 101), set())

    def test_state_includes_gem_yield(self):
        k = kb()
        record_cut(k, MAP, (1, 1), "bush", 1, True)
        messages = build_prompt(
            triggers=[],
            w=world(),
            plan=Plan([], dict(PARAM_DEFAULTS)),
            directives=Directives(params=dict(PARAM_DEFAULTS)),
            knowledge=k,
        )
        state = messages[1]["content"].split("State:\n", 1)[1].split("\n\n", 1)[0]
        self.assertIn('gem_yield={"barren": [], "best": [{"cuts": 1, "gems": 1, "x": 0, "y": 0, "yield": 1.0}]', state)
        self.assertIn("gem_yield", messages[0]["content"])

    def test_state_shows_gather_status_while_gather_gems_is_on_top(self):
        def state(goals, status):
            messages = build_prompt(
                triggers=[],
                w=world(),
                plan=Plan(goals, dict(PARAM_DEFAULTS)),
                directives=Directives(params=dict(PARAM_DEFAULTS)),
                knowledge=kb(),
                gather_status=status,
            )
            return messages[1]["content"].split("State:\n", 1)[1].split("\n\n", 1)[0]

        gather = [{"op": "gather_gems", "count": 5}]
        self.assertIn('gather_status="no cuttable cell in view"', state(gather, "no cuttable cell in view"))
        self.assertNotIn("gather_status", state(gather, ""))
        self.assertNotIn("gather_status", state([{"op": "buy", "code": "torch"}], "cutting"))

    def test_prompt_says_x_y_does_not_move_the_character(self):
        system = build_prompt(
            triggers=[], w=world(), plan=Plan([], dict(PARAM_DEFAULTS)), directives=Directives(params=dict(PARAM_DEFAULTS)), knowledge=kb()
        )[0]["content"]
        self.assertIn("it does not move the character", system)
        self.assertIn("never re-send an otherwise unchanged gather_gems just to change x, y", system)

    def test_state_without_knowledge_has_empty_gem_yield(self):
        kb_fake = SimpleNamespace(lock=threading.Lock(), clues=[], extra={})
        messages = build_prompt(
            triggers=[], w=world(), plan=Plan([], dict(PARAM_DEFAULTS)), directives=Directives(params=dict(PARAM_DEFAULTS)), knowledge=kb_fake
        )
        self.assertIn('"best": []', messages[1]["content"])


def safe_world() -> WorldModel:
    w = world(at=(1, 1))
    apply_zone(w, MAP, 1, 1, {"safe": True, "brightness": 1})
    return w


class GatherSkipsBarrenTest(unittest.TestCase):
    def setUp(self):
        self.k = kb()
        for i in range(BARREN_MIN_CUTS):
            # Long ago, so the cells have grown back: only the barren mark keeps them.
            record_cut(self.k, MAP, (5, 5), "grass", -gem_yield.REGROW_TICKS - i, False)

    def test_barren_grass_is_not_cut(self):
        out = gather_outcome(safe_world(), Memory(), Policy(on_hostile="ignore"), knowledge=self.k, op={"op": "gather_gems", "count": 5})
        self.assertNotEqual(out.reason, "cut grass")

    def test_op_naming_the_region_cuts_it_anyway(self):
        op = {"op": "gather_gems", "count": 5, "x": 3, "y": 3}
        out = gather_outcome(safe_world(), Memory(), Policy(on_hostile="ignore"), knowledge=self.k, op=op)
        self.assertEqual(out.reason, "cut grass")

    def test_a_cell_cut_before_it_grew_back_is_not_cut_again(self):
        k = kb()
        record_cut(k, MAP, (1, 1), "grass", 90, False)
        m = Memory()
        out = gather_outcome(safe_world(), m, Policy(on_hostile="ignore"), knowledge=k, op={"op": "gather_gems", "count": 5})
        self.assertEqual(out.intents[0]["verb"], "SetPosition")
        self.assertNotEqual(m.gather_target[1], (1, 1))

    def test_unsampled_region_is_still_cut(self):
        out = gather_outcome(safe_world(), Memory(), Policy(on_hostile="ignore"), knowledge=kb(), op={"op": "gather_gems", "count": 5})
        self.assertEqual(out.reason, "cut grass")

    def test_op_validates_region_coordinates(self):
        self.assertIsNotNone(validate_goal_op({"op": "gather_gems", "count": 3, "x": 1, "y": 2}))
        self.assertIsNone(validate_goal_op({"op": "gather_gems", "count": 3, "x": "a", "y": 2}))


if __name__ == "__main__":
    unittest.main()
