"""A63: gem yield per region, learned from the agent's own cuts. Fake maps and cells only."""

import random
import threading
import unittest
from types import SimpleNamespace

from agentrealm_agent import gem_yield
from agentrealm_agent.config import Policy
from agentrealm_agent.directives import PARAM_DEFAULTS, Directives
from agentrealm_agent.gem_yield import BARREN_MIN_CUTS, GEM_WINDOW_TICKS, GemYieldTracker, record_cut
from agentrealm_agent.item_table import InventorySupply
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.memory import Memory
from agentrealm_agent.plan import Plan, validate_goal_op
from agentrealm_agent.runner import Runner
from agentrealm_agent.states import gather_outcome
from agentrealm_agent.states import gather as gather_mod
from agentrealm_agent.strategist import build_prompt
from agentrealm_agent.world import Entity, WorldModel

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


class NoEffectCutTest(unittest.TestCase):
    """A63 run 2: 168 ``applied_no_effect`` cuts of one cell, none learned."""

    def runner(self) -> Runner:
        r = Runner.__new__(Runner)
        r.world, r.mem, r.knowledge, r.acceptance = world(), Memory(), kb(), None
        r.gem_cuts, r._applied_uses, r._applied_take_codes, r._loadout_verbs = GemYieldTracker(), [], [], []
        return r

    def test_runner_holds_the_cell_out_without_filing_a_cut(self):
        r = self.runner()
        r.mem.pending = {"verb": "Use", "target": {"kind": "block", "x": 1, "y": 1}}
        r.on_result({"outcome": "applied_no_effect", "tick": 100}, 0)
        self.assertEqual(r.gem_cuts.pending, [])
        self.assertEqual(gem_yield.exhausted_cells(r.knowledge, MAP, 101, r.gem_cuts), {(1, 1)})
        r.world.tick = 100 + GEM_WINDOW_TICKS + 1
        r.gem_cuts.update(r.world, r.knowledge)
        self.assertNotIn("gem_yield", r.knowledge.extra, "never filed: no false miss toward barren")
        self.assertEqual(r.gem_cuts.run_counts(), {"cuts": 0, "no_effect_cuts": 1, "gems_gained": 0})

    def test_a_break_probe_that_misses_is_not_a_no_effect_cut(self):
        """Review on #126: Break's capability misses (A28) are not ground that does not cut."""
        r = self.runner()
        r.mem.break_pending = (MAP, (1, 1), "smash")
        r.mem.pending = {"verb": "Use", "target": {"kind": "block", "x": 1, "y": 1}}
        r.on_result({"outcome": "applied_no_effect", "tick": 100}, 0)
        self.assertEqual((r.gem_cuts.no_effect, r.gem_cuts.no_effect_cuts), ([], 0))

    def test_a_cut_made_with_a_potion_marks_nothing(self):
        """Free-play run 3: cuts with a potion left armed marked ground uncuttable."""
        r = self.runner()
        r.world.armed_code = "small_potion"
        for x in range(gem_yield.NO_EFFECT_ZONE_CUTS):
            r.mem.pending = {"verb": "Use", "target": {"kind": "block", "x": x, "y": 1}}
            r.on_result({"outcome": "applied_no_effect", "tick": 100}, 0)
        self.assertEqual((r.gem_cuts.no_effect, r.gem_cuts.no_effect_cuts), ([], 0))
        self.assertEqual(r.gem_cuts.uncuttable(r.world), (set(), set()))

    def test_the_item_armed_in_the_same_queue_is_what_cut(self):
        # [Arm, Wait…, Use]: the observation has not shown the Arm yet.
        r = self.runner()
        r.world.armed_code = "pocket_knife"
        r.world.held_supplies = [InventorySupply(4, "small_potion")]
        r.mem.pending_intents = [{"verb": "Arm", "supply_id": 4}, {"verb": "Wait"},
                                 {"verb": "Use", "target": {"kind": "block", "x": 1, "y": 1}}]
        r.on_result({"outcome": "applied_no_effect", "tick": 100}, 2)
        self.assertEqual(r.gem_cuts.no_effect_cuts, 0, "cut with the potion")
        r.world.armed_code = "small_potion"
        r.world.held_supplies = [InventorySupply(1, "pocket_knife")]
        r.mem.pending_intents = [{"verb": "Arm", "supply_id": 1}, {"verb": "Wait"},
                                 {"verb": "Use", "target": {"kind": "block", "x": 1, "y": 1}}]
        r.on_result({"outcome": "applied_no_effect", "tick": 100}, 2)
        self.assertEqual(r.gem_cuts.no_effect_cuts, 1, "cut with the knife")

    def test_an_arm_applied_in_an_earlier_response_is_read_from_armed(self):
        # The observation since moved the potion from held to armed.
        r = self.runner()
        r.world.armed_code = "small_potion"
        r.world.held_supplies = [InventorySupply(1, "pocket_knife")]
        r.mem.pending_intents = [{"verb": "Arm", "supply_id": 4}, {"verb": "Wait"},
                                 {"verb": "Use", "target": {"kind": "block", "x": 1, "y": 1}}]
        r.on_result({"outcome": "applied_no_effect", "tick": 100}, 2)
        self.assertEqual(r.gem_cuts.no_effect_cuts, 0)

    def test_a_cut_with_an_item_not_known_to_cut_or_not_still_counts(self):
        # Only a known miss is ruled out: an unsourced code may cut.
        r = self.runner()
        r.world.armed_code = "fake_cleaver"
        r.mem.pending = {"verb": "Use", "target": {"kind": "block", "x": 1, "y": 1}}
        r.on_result({"outcome": "applied_no_effect", "tick": 100}, 0)
        self.assertEqual(r.gem_cuts.no_effect_cuts, 1)

    def test_the_hold_lapses_after_regrow_ticks(self):
        w, t = world(), GemYieldTracker()
        t.note_no_effect(w, (1, 1), "grass", 100)
        self.assertEqual(t.pending_cells(MAP, 100 + gem_yield.REGROW_TICKS - 1), {(1, 1)})
        self.assertEqual(t.pending_cells(MAP, 100 + gem_yield.REGROW_TICKS), set())
        self.assertEqual(t.pending_cells(MAP + 1, 100), set())

    def test_only_grass_is_held(self):
        w, t = world(), GemYieldTracker()
        t.note_no_effect(w, (1, 1), "rock", 100)  # break memory owns other blocks (A28)
        t.note_no_effect(w, (1, 1), "bush", 100)  # bushes drop berries, not gems (A81)
        self.assertEqual((t.no_effect, t.no_effect_cuts), ([], 0))

    def test_gather_moves_on_and_says_cuts_have_no_effect(self):
        w, t, m = field_world(), GemYieldTracker(), Memory()
        op = {"op": "gather_gems", "count": 5}
        t.note_no_effect(w, (1, 1), "grass", w.tick)
        out = gather_outcome(w, m, Policy(on_hostile="ignore"), op=op, gem_cuts=t)
        self.assertEqual(out.intents[0]["verb"], "SetPosition")
        self.assertNotEqual(m.gather_target[1], (1, 1))
        self.assertEqual(m.gather_status, gather_mod.NO_EFFECT)
        t.note_cut(w, m.gather_target[1], "grass", w.tick)  # a cut that took effect
        gather_outcome(w, m, Policy(on_hostile="ignore"), op=op, gem_cuts=t)
        self.assertEqual(m.gather_status, "walking to grass")

    def test_no_effect_status_is_local_and_lapses(self):
        w, t, m = field_world(), GemYieldTracker(), Memory()
        op = {"op": "gather_gems", "count": 5}
        t.note_no_effect(w, (1, 1), "grass", w.tick)
        w.view.tiles[(40, 1)] = "grass"
        w.pos = (40, 1)  # another region
        gather_outcome(w, m, Policy(on_hostile="ignore"), op=op, gem_cuts=t)
        self.assertEqual(m.gather_status, gather_mod.CUTTING)
        w.pos, w.tick = (1, 2), w.tick + gem_yield.REGROW_TICKS
        gather_outcome(w, m, Policy(on_hostile="ignore"), op=op, gem_cuts=t)
        self.assertEqual(m.gather_status, gather_mod.CUTTING)
        w.tick += gem_yield.NO_EFFECT_TTL
        self.assertEqual(t.uncuttable(w), (set(), set()))
        self.assertEqual(t.no_effect, [], "forgotten after NO_EFFECT_TTL")

    def test_the_store_is_capped_oldest_first(self):
        w, t = world(), GemYieldTracker()
        for i in range(gem_yield.MAX_NO_EFFECT + 5):
            t.note_no_effect(w, (i, 0), "grass", w.tick)
        self.assertEqual(len(t.no_effect), gem_yield.MAX_NO_EFFECT)
        self.assertNotIn((MAP, (0, 0), w.tick), t.no_effect)

    def test_repeat_cuts_on_one_safe_cell_mark_its_zone(self):
        """Review on #126: three no-effect cuts count, even on one cell (run 2 cut one cell 168 times)."""
        w, t = world(), GemYieldTracker()
        w.view.safe |= {(1, 1), (2, 1)}
        for i in range(gem_yield.NO_EFFECT_ZONE_CUTS):
            t.note_no_effect(w, (1, 1), "grass", w.tick + i * gem_yield.REGROW_TICKS)
        w.tick += (gem_yield.NO_EFFECT_ZONE_CUTS - 1) * gem_yield.REGROW_TICKS
        self.assertEqual(t.uncuttable(w), (set(), {(1, 1), (2, 1)}))

    def test_no_effect_cuts_in_one_field_region_mark_it_uncuttable(self):
        w, t = world(), GemYieldTracker()
        for x in range(gem_yield.NO_EFFECT_ZONE_CUTS - 1):
            t.note_no_effect(w, (x, 0), "grass", w.tick)
        self.assertEqual(t.uncuttable(w), (set(), set()))
        t.note_no_effect(w, (4, 4), "grass", w.tick)
        self.assertEqual(t.uncuttable(w), ({(0, 0)}, set()), "a region, not barren: nothing is filed")
        self.assertEqual(gem_yield.barren_regions(kb(), MAP), set())

    def test_gems_gained_counts_rises_not_spending(self):
        w, t = world(gems=3), GemYieldTracker()
        t.update(w, None)
        w.gems = 5
        t.update(w, None)
        w.gems = 1  # bought something
        t.update(w, None)
        w.gems = 2
        t.update(w, None)
        self.assertEqual(t.gems_gained, 3)

    def test_state_shows_this_runs_cuts_and_gems(self):
        t = GemYieldTracker(cuts=4, no_effect_cuts=2, gems_gained=1)
        messages = build_prompt(
            triggers=[],
            w=world(),
            plan=Plan([{"op": "gather_gems", "count": 5}], dict(PARAM_DEFAULTS)),
            directives=Directives(params=dict(PARAM_DEFAULTS)),
            knowledge=kb(),
            gather_run=t.run_counts(),
        )
        self.assertIn('gather_run={"cuts": 4, "gems_gained": 1, "no_effect_cuts": 2}', messages[1]["content"])


class RegionSummaryTest(unittest.TestCase):
    def test_regions_count_cuts_gems_and_last_tick(self):
        k = kb()
        record_cut(k, MAP, (1, 1), "grass", 10, False)
        record_cut(k, MAP, (3, 4), "grass", 20, True)
        record_cut(k, MAP, (4, 4), "bush", 25, False)  # bushes drop berries, not gems (A81): no region count
        record_cut(k, MAP, (17, 1), "grass", 30, False)
        record_cut(k, MAP, (2, 2), "rock", 40, False)  # not a gather block: no region count
        regions = gem_yield.regions(k, MAP)
        self.assertEqual(regions["0,0"], {"cuts": 2, "gems": 1, "last_tick": 20})
        self.assertEqual(regions["1,0"], {"cuts": 1, "gems": 0, "last_tick": 30})
        self.assertEqual(len(cuts(k)), 5)

    def test_barren_needs_the_threshold_and_no_gem(self):
        k = kb()
        for i in range(BARREN_MIN_CUTS - 1):
            record_cut(k, MAP, (1, 1), "grass", i, False)
        self.assertFalse((0, 0) in gem_yield.barren_regions(k, MAP))
        record_cut(k, MAP, (2, 2), "grass", 99, False)
        self.assertTrue((0, 0) in gem_yield.barren_regions(k, MAP))
        record_cut(k, MAP, (2, 2), "grass", 100, True)
        self.assertFalse((0, 0) in gem_yield.barren_regions(k, MAP))

    def test_summary_lists_best_at_any_distance_and_barren_nearby(self):
        # A63 run 3: past 3 regions the one productive region vanished from
        # the planner's view; best regions now show at any distance, with it.
        k = kb()
        for i in range(BARREN_MIN_CUTS):
            record_cut(k, MAP, (1, 1), "grass", i, False)
            record_cut(k, MAP, (300, 300), "grass", i, False)  # barren, too far to be nearby
        for i, gem in enumerate([True, False, False, False]):
            record_cut(k, MAP, (17, 1), "grass", i, gem)
        record_cut(k, MAP, (33, 1), "grass", 1, True)
        record_cut(k, MAP, (200, 200), "grass", 1, True)  # far, still listed
        s = gem_yield.summary(world(at=(2, 2)), k)
        self.assertEqual(s["here"], {"x": 0, "y": 0, "cuts": BARREN_MIN_CUTS, "gems": 0})
        self.assertEqual(
            [(b["x"], b["yield"], b["distance"]) for b in s["best"]],
            [(32, 1.0, 30), (192, 1.0, 190), (16, 0.25, 14)],
        )
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
        record_cut(k, MAP, (1, 1), "grass", 1, True)
        messages = build_prompt(
            triggers=[],
            w=world(),
            plan=Plan([], dict(PARAM_DEFAULTS)),
            directives=Directives(params=dict(PARAM_DEFAULTS)),
            knowledge=k,
        )
        state = messages[1]["content"].split("State:\n", 1)[1].split("\n\n", 1)[0]
        self.assertIn('gem_yield={"barren": [], "best": [{"cuts": 1, "distance": 0, "gems": 1, "x": 0, "y": 0, "yield": 1.0}]', state)
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

    def test_prompt_says_x_y_names_a_region_gather_walks_to(self):
        system = build_prompt(
            triggers=[], w=world(), plan=Plan([], dict(PARAM_DEFAULTS)), directives=Directives(params=dict(PARAM_DEFAULTS)), knowledge=kb()
        )[0]["content"]
        self.assertIn("A gather_gems x, y names a target region", system)
        self.assertIn("Gather walks there and cuts only there", system)
        self.assertIn("never re-send an otherwise unchanged gather_gems just to change x, y", system)

    def test_state_without_knowledge_has_empty_gem_yield(self):
        kb_fake = SimpleNamespace(lock=threading.Lock(), clues=[], extra={}, entrances={}, items={}, maps={})
        messages = build_prompt(
            triggers=[], w=world(), plan=Plan([], dict(PARAM_DEFAULTS)), directives=Directives(params=dict(PARAM_DEFAULTS)), knowledge=kb_fake
        )
        self.assertIn('"best": []', messages[1]["content"])


def field_world() -> WorldModel:
    """Standing on grass outside any known safe zone."""
    return world(at=(1, 1))


class GatherSkipsBarrenTest(unittest.TestCase):
    def setUp(self):
        self.k = kb()
        for i in range(BARREN_MIN_CUTS):
            # Long ago, so the cells have grown back: only the barren mark keeps them.
            record_cut(self.k, MAP, (5, 5), "grass", -gem_yield.REGROW_TICKS - i, False)

    def test_barren_grass_is_not_cut(self):
        out = gather_outcome(field_world(), Memory(), Policy(on_hostile="ignore"), knowledge=self.k, op={"op": "gather_gems", "count": 5})
        self.assertNotEqual(out.reason, "cut grass")

    def test_op_naming_the_region_cuts_it_anyway(self):
        op = {"op": "gather_gems", "count": 5, "x": 3, "y": 3}
        out = gather_outcome(field_world(), Memory(), Policy(on_hostile="ignore"), knowledge=self.k, op=op)
        self.assertEqual(out.reason, "cut grass (region 0,0)", "works in the region it names")

    def test_a_cell_cut_before_it_grew_back_is_not_cut_again(self):
        k = kb()
        record_cut(k, MAP, (1, 1), "grass", 90, False)
        m = Memory()
        out = gather_outcome(field_world(), m, Policy(on_hostile="ignore"), knowledge=k, op={"op": "gather_gems", "count": 5})
        self.assertEqual(out.intents[0]["verb"], "SetPosition")
        self.assertNotEqual(m.gather_target[1], (1, 1))

    def test_a_cut_still_in_its_gem_window_is_not_cut_again(self):
        """Exhausted from the moment of the cut, not only once filed: a stale
        read still showing grass must not file the same cell twice."""
        w, k, tracker = field_world(), kb(), GemYieldTracker()
        op = {"op": "gather_gems", "count": 5}
        out = gather_outcome(w, Memory(), Policy(on_hostile="ignore"), knowledge=k, op=op, gem_cuts=tracker)
        self.assertEqual(out.reason, "cut grass")
        tracker.note_cut(w, (1, 1), "grass", w.tick)
        w.tick += 1
        tracker.update(w, k)  # inside GEM_WINDOW_TICKS: pending, not filed
        self.assertEqual(gem_yield.regions(k, MAP), {})
        m = Memory()
        out = gather_outcome(w, m, Policy(on_hostile="ignore"), knowledge=k, op=op, gem_cuts=tracker)
        self.assertEqual(out.intents[0]["verb"], "SetPosition")
        self.assertNotEqual(m.gather_target[1], (1, 1))

    def test_pending_cells_are_of_this_map_only(self):
        w, tracker = field_world(), GemYieldTracker()
        tracker.note_cut(w, (1, 1), "grass", w.tick)
        self.assertEqual(gem_yield.exhausted_cells(None, MAP, w.tick, tracker), {(1, 1)})
        self.assertEqual(gem_yield.exhausted_cells(None, MAP + 1, w.tick, tracker), set())

    def test_unsampled_region_is_still_cut(self):
        out = gather_outcome(field_world(), Memory(), Policy(on_hostile="ignore"), knowledge=kb(), op={"op": "gather_gems", "count": 5})
        self.assertEqual(out.reason, "cut grass")

    def test_op_validates_region_coordinates(self):
        self.assertIsNotNone(validate_goal_op({"op": "gather_gems", "count": 3, "x": 1, "y": 2}))
        self.assertIsNone(validate_goal_op({"op": "gather_gems", "count": 3, "x": "a", "y": 2}))


class DerivedThresholdTest(unittest.TestCase):
    """A81: barren, poor and good are judged against the map's measured yield,
    the manual's 20% until our cuts outweigh it."""

    def test_the_prior_is_the_manuals_lowest_grass_rate(self):
        self.assertAlmostEqual(gem_yield.expected_yield({}), 0.20)
        self.assertEqual(gem_yield.barren_min_cuts(0.20), 15)  # 0.8**15 < 4%
        self.assertEqual(BARREN_MIN_CUTS, 15)

    def test_every_cut_counts_whatever_it_dropped(self):
        """Review on #172: counting only regions that dropped a gem skewed the rate high."""
        rate = gem_yield.expected_yield({"0,0": {"cuts": 40, "gems": 0}, "1,0": {"cuts": 20, "gems": 6}})
        self.assertAlmostEqual(rate, (6 + 0.20 * gem_yield.PRIOR_CUTS) / (60 + gem_yield.PRIOR_CUTS))

    def test_barren_is_held_to_the_manuals_rate_not_the_maps(self):
        """Review on #172: a rich map must not mark unlucky ground barren early."""
        k = kb()
        for i in range(60):
            record_cut(k, MAP, (17, 1), "grass", i, True)  # a region paying every cut
        for i in range(BARREN_MIN_CUTS - 1):
            record_cut(k, MAP, (1, 1), "grass", i, False)
        self.assertEqual(gem_yield.barren_regions(k, MAP), set())
        record_cut(k, MAP, (1, 1), "grass", 99, False)
        self.assertEqual(gem_yield.barren_regions(k, MAP), {(0, 0)}, "and its own misses do not put it off")

    def test_poor_is_half_the_maps_rate(self):
        k = kb()
        for i in range(20):
            record_cut(k, MAP, (1, 1), "grass", i, i < 3)  # 15%: fair at the prior
        self.assertEqual(gem_yield.poor_regions(k, MAP), set())
        for i in range(80):
            record_cut(k, MAP, (17, 1), "grass", i, i % 2 == 0)  # a region at 50%
        self.assertEqual(gem_yield.poor_regions(k, MAP), {(0, 0)}, "15% is under half of what this map pays")


class StoreVersionTest(unittest.TestCase):
    """Review on #172: totals saved before A81 counted bush cuts at the old
    rates; read now they would mark ground barren that Gather never revisits."""

    def old_store(self) -> KnowledgeBase:
        k = kb()
        k.extra["gem_yield"] = {str(MAP): {
            "cuts": [{"x": 1, "y": 1, "block": "bush", "tick": 5, "gem": False}],
            "regions": {"0,0": {"cuts": 25, "gems": 0, "last_tick": 5}},
        }}
        return k

    def test_an_old_store_reads_as_empty(self):
        k = self.old_store()
        self.assertEqual(gem_yield.regions(k, MAP), {})
        self.assertEqual(gem_yield.barren_regions(k, MAP), set())
        self.assertEqual(gem_yield.exhausted_cells(k, MAP, 6), set())

    def test_the_next_cut_replaces_it(self):
        k = self.old_store()
        record_cut(k, MAP, (2, 2), "grass", 10, True)
        self.assertEqual(k.extra["gem_yield"]["version"], gem_yield.VERSION)
        self.assertEqual(gem_yield.regions(k, MAP), {"0,0": {"cuts": 1, "gems": 1, "last_tick": 10}})


if __name__ == "__main__":
    unittest.main()
