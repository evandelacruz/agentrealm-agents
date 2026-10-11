"""A104: an op that stalls moves below the next op that can make progress,
and stays there, remembered with why, until a reason to retry comes."""

from __future__ import annotations

import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from agentrealm_agent.directives import PARAM_DEFAULTS
from agentrealm_agent.item_table import InventorySupply
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.memory import Memory
from agentrealm_agent.plan import PLAN_STALL_SECONDS, Plan
from agentrealm_agent import runner as runner_module
from agentrealm_agent.runner import Runner
from agentrealm_agent.stalled import STALLED_KEPT, below_progress, break_cells, find, forget, lapse, remember, retry_basis
from agentrealm_agent.states.dispatch import dispatch
from agentrealm_agent.strategist import system_prompt
from agentrealm_agent.world import WorldModel
from tests.test_strategist import FakeLLM, fake_runner, make, round_trip, state_of
from tests.test_travel import ctx_for, grid, travel_plan

TRAVEL = {"op": "travel", "to": "entrance", "x": 40, "y": 2, "why": "the level"}
BREAK = {"op": "break_block", "x": 5, "y": 5, "capability": "smash", "why": "a rock in the way"}
GATHER = {"op": "gather_gems", "count": 20, "why": "gems"}
EQUIP = {"op": "equip", "why": "arm the best"}


def stalled_on(*ops: dict) -> Memory:
    """Memory with each of ``ops`` remembered as stalled, with nothing held and nothing known."""
    m = Memory()
    for op in ops:
        remember(m.stalled_ops, op, "no progress", 100, retry_basis(WorldModel(character_id=1), None))
    return m


class BelowProgressTest(unittest.TestCase):
    def test_stalled_ops_on_top_move_just_below_the_first_that_can_progress(self):
        stalled = lambda op: op["op"] in ("travel", "break_block")
        order, moved, above = below_progress([TRAVEL, BREAK, GATHER, EQUIP], stalled)
        self.assertEqual(order, [GATHER, TRAVEL, BREAK, EQUIP])
        self.assertEqual((moved, above), ([TRAVEL, BREAK], GATHER))

    def test_a_stalled_op_already_below_progress_stays(self):
        order, moved, _ = below_progress([GATHER, TRAVEL], lambda op: op is TRAVEL)
        self.assertEqual((order, moved), ([GATHER, TRAVEL], []))

    def test_with_nothing_that_can_progress_the_stalled_ops_leave(self):
        order, moved, above = below_progress([TRAVEL, BREAK], lambda op: True)
        self.assertEqual((order, moved, above), ([], [TRAVEL, BREAK], None))


class DemoteTest(unittest.TestCase):
    def test_the_stalled_head_moves_below_the_next_op_that_can_progress(self):
        m = stalled_on(TRAVEL, BREAK)
        plan = Plan([dict(TRAVEL), dict(BREAK), dict(GATHER), dict(EQUIP)], dict(PARAM_DEFAULTS))
        plan.stalled_since_tick = 5
        plan.demote_current("Travel: no progress for 30s", m)
        self.assertEqual([op["op"] for op in plan.ops_left()], ["gather_gems", "travel", "break_block", "equip"])
        self.assertIsNone(plan.stalled_since_tick, "the new head's stall clock starts fresh")
        signal = m.strategist_signals[-1]
        self.assertEqual((signal["trigger"], signal["now_below"]["op"]), ("goal_stalled", "gather_gems"))

    def test_with_nothing_below_that_can_progress_it_is_dropped(self):
        m = stalled_on(TRAVEL, BREAK)
        plan = Plan([dict(TRAVEL), dict(BREAK)], dict(PARAM_DEFAULTS))
        plan.demote_current("Travel: no progress for 30s", m)
        self.assertEqual([op["op"] for op in plan.ops_left()], ["break_block"])
        self.assertEqual(m.strategist_signals[-1]["trigger"], "goal_failed")

    def test_a_directives_op_moves_only_among_the_directives_ops(self):
        m = stalled_on(TRAVEL)
        plan = Plan([dict(TRAVEL), dict(EQUIP), dict(GATHER)], dict(PARAM_DEFAULTS), directive_end=2)
        plan.demote_current("stalled", m)
        self.assertEqual([op["op"] for op in plan.ops_left()], ["equip", "travel", "gather_gems"])
        self.assertEqual(plan.directive_end, 2, "both pinned ops stay on top")
        alone = Plan([dict(TRAVEL), dict(GATHER)], dict(PARAM_DEFAULTS), directive_end=1)
        alone.demote_current("stalled", m)
        self.assertEqual([op["op"] for op in alone.ops_left()], ["gather_gems"], "no pinned op below: dropped as before")

    def test_a_stall_in_play_is_remembered_and_moves_the_op_below(self):
        w = grid(["....."], at=(0, 0))
        plan = travel_plan(["travel:shop", "travel:point:3:0"])
        m = Memory()
        ctx = ctx_for(m, KnowledgeBase.empty("sandbox"), plan)
        dispatch(w, ctx)
        w.tick += PLAN_STALL_SECONDS * plan.tick_hz
        dispatch(w, ctx)
        self.assertEqual([op["to"] for op in plan.ops_left()], ["point", "shop"])
        record = find(m.stalled_ops, {"op": "travel", "to": "shop", "x": 0, "y": 0})
        self.assertIsNotNone(record)
        self.assertIn("no progress", record.why)

    def test_an_op_that_finishes_is_forgotten(self):
        m = stalled_on(GATHER)
        plan = Plan([dict(GATHER)], dict(PARAM_DEFAULTS))
        w = WorldModel(character_id=1, map_id=1, pos=(0, 0), gems=25)
        plan.advance(w, m)
        self.assertIsNone(find(m.stalled_ops, GATHER))


class AdvanceTest(unittest.TestCase):
    def test_a_stalled_op_does_not_come_back_on_top_when_the_op_above_ends(self):
        m = stalled_on(TRAVEL)
        plan = Plan([dict(GATHER), dict(TRAVEL), dict(EQUIP)], dict(PARAM_DEFAULTS))
        plan.advance(WorldModel(character_id=1, map_id=1, pos=(0, 0), gems=25), m)
        self.assertEqual([op["op"] for op in plan.ops_left()], ["equip", "travel"])
        self.assertEqual(m.strategist_signals[-1]["trigger"], "goal_stalled")

    def test_with_nothing_below_it_is_dropped(self):
        m = stalled_on(TRAVEL)
        plan = Plan([dict(GATHER), dict(TRAVEL)], dict(PARAM_DEFAULTS))
        plan.advance(WorldModel(character_id=1, map_id=1, pos=(0, 0), gems=25), m)
        self.assertEqual(plan.ops_left(), [])
        self.assertEqual(m.strategist_signals[-1]["trigger"], "goal_failed")

    def test_once_lapsed_it_comes_back_on_top(self):
        m = stalled_on(TRAVEL)
        w = WorldModel(character_id=1, map_id=1, pos=(0, 0), gems=25)
        w.held_supplies = [InventorySupply(id=1, code="iron_key")]
        lapse(m.stalled_ops, retry_basis(w, None))
        plan = Plan([dict(GATHER), dict(TRAVEL), dict(EQUIP)], dict(PARAM_DEFAULTS))
        plan.advance(w, m)
        self.assertEqual(plan.current()["op"], "travel")


class MapScopeTest(unittest.TestCase):
    """A stalled op's record is its map's: an op's x, y name a cell there (A111)."""

    def on_maps(self, *maps):
        m = Memory()
        for map_id in maps:
            remember(m.stalled_ops, BREAK, f"no way on map {map_id}", 100, retry_basis(WorldModel(character_id=1), None), map_id)
        return m

    def test_find_reads_only_this_maps_record(self):
        m = self.on_maps(1)
        self.assertIsNotNone(find(m.stalled_ops, BREAK, 1))
        self.assertIsNone(find(m.stalled_ops, BREAK, 2))
        self.assertEqual(find(self.on_maps(1, 2).stalled_ops, BREAK, 2).why, "no way on map 2")

    def test_finishing_on_one_map_keeps_the_other_maps_record(self):
        m = self.on_maps(1, 2)
        forget(m.stalled_ops, BREAK, 2)
        self.assertEqual([r.map_id for r in m.stalled_ops], [1])
        self.assertEqual(break_cells(m.stalled_ops, 1), {(5, 5)})

    def test_advance_on_another_map_neither_holds_nor_forgets_it(self):
        m = self.on_maps(1)
        plan = Plan([dict(GATHER), dict(BREAK), dict(EQUIP)], dict(PARAM_DEFAULTS))
        plan.advance(WorldModel(character_id=1, map_id=2, pos=(0, 0), gems=25), m)
        self.assertEqual(plan.current()["op"], "break_block", "map 1's stall is no reason on map 2")
        self.assertEqual(len(m.stalled_ops), 1)


class RunnerExpireTest(unittest.TestCase):
    def runner(self, m: Memory) -> SimpleNamespace:
        return SimpleNamespace(mem=m, world=WorldModel(character_id=1, map_id=1, pos=(0, 0)), knowledge=None, log=mock.MagicMock())

    def test_a_reason_to_retry_forgets_the_record_and_logs_why(self):
        r = self.runner(stalled_on(BREAK))
        r.world.held_supplies = [InventorySupply(id=1, code="bronze_mallet")]
        Runner.expire_stalled_ops(r)
        self.assertEqual(r.mem.stalled_ops, [])
        record = r.log.call_args.args[2]["stalled"]
        self.assertEqual((record["event"], record["because"]), ("lapsed", "now holds bronze_mallet"))

    def test_nothing_new_keeps_it_and_logs_nothing(self):
        r = self.runner(stalled_on(BREAK))
        Runner.expire_stalled_ops(r)
        self.assertEqual(len(r.mem.stalled_ops), 1)
        r.log.assert_not_called()

    def test_with_nothing_remembered_it_reads_nothing(self):
        r = self.runner(Memory())
        with mock.patch.object(runner_module, "retry_basis") as basis:
            Runner.expire_stalled_ops(r)
        basis.assert_not_called()


class RetryTest(unittest.TestCase):
    def setUp(self):
        self.w = WorldModel(character_id=1, map_id=1, pos=(0, 0))
        self.kb = SimpleNamespace(lock=threading.Lock(), clues=[], entrances={}, maps={})
        self.records = []
        remember(self.records, BREAK, "no progress", 100, retry_basis(self.w, self.kb))

    def test_nothing_new_keeps_the_record(self):
        self.assertEqual(lapse(self.records, retry_basis(self.w, self.kb)), [])
        self.assertEqual(len(self.records), 1)

    def test_a_new_item_held_lapses_it(self):
        self.w.held_supplies = [InventorySupply(id=1, code="bronze_mallet")]
        lapsed = lapse(self.records, retry_basis(self.w, self.kb))
        self.assertEqual([why for _, why in lapsed], ["now holds bronze_mallet"])
        self.assertEqual(self.records, [])

    def test_a_new_clue_lapses_it(self):
        self.kb.clues.append({"kind": "sign", "text": "the rock crumbles to fire"})
        self.assertEqual([why for _, why in lapse(self.records, retry_basis(self.w, self.kb))], ["a new clue"])

    def test_a_new_way_known_lapses_it(self):
        self.kb.maps["1"] = {"doors": [{"x": 3, "y": 0, "to_map_id": 2, "to_x": 0, "to_y": 0}]}
        self.assertEqual([why for _, why in lapse(self.records, retry_basis(self.w, self.kb))], ["a new way known"])

    def test_only_the_newest_records_are_kept(self):
        basis = retry_basis(self.w, self.kb)
        for x in range(STALLED_KEPT + 3):
            remember(self.records, {"op": "travel", "to": "point", "x": x, "y": 0}, "no progress", x, basis)
        self.assertEqual(len(self.records), STALLED_KEPT)
        self.assertIsNone(find(self.records, BREAK), "the oldest went first")
        self.assertIsNotNone(find(self.records, {"op": "travel", "to": "point", "x": STALLED_KEPT + 2, "y": 0}))

    def test_losing_an_item_is_no_reason(self):
        self.w.held_supplies = [InventorySupply(id=1, code="small_potion")]
        records = []
        remember(records, BREAK, "no progress", 100, retry_basis(self.w, self.kb))
        self.w.held_supplies = []
        self.assertEqual(lapse(records, retry_basis(self.w, self.kb)), [])


class PlannerReplyTest(unittest.TestCase):
    def test_a_resent_stalled_op_is_kept_below_and_said_why(self):
        s, r = make(FakeLLM({"goals": [TRAVEL, GATHER]})), fake_runner()
        r.mem = stalled_on(TRAVEL)
        round_trip(s, r)
        self.assertEqual([op["op"] for op in r.plan.ops_left()], ["gather_gems", "travel"])
        self.assertEqual(len(s.rejected), 1)
        self.assertIn("moved below", s.rejected[0])
        self.assertIn("no progress", s.rejected[0])

    def test_the_trace_records_what_was_kept_below(self):
        s, r = make(FakeLLM({"goals": [TRAVEL, GATHER]})), fake_runner()
        r.mem = stalled_on(TRAVEL)
        round_trip(s, r)
        applied = [c.args[2]["strategist"] for c in r.log.call_args_list if c.args[2]["strategist"]["event"] == "applied"]
        self.assertEqual(applied[-1]["stalled_kept_below"], [TRAVEL])

    def test_a_stall_while_the_call_is_out_keeps_the_reply_from_putting_it_back(self):
        # The planner was shown the travel on top; it stalls and moves below
        # while the call is out, and the reply sends the stack it was shown.
        s, r = make(FakeLLM({"goals": [TRAVEL, GATHER]})), fake_runner()
        r.plan = Plan([dict(TRAVEL), dict(GATHER)], dict(PARAM_DEFAULTS))
        s.on_window(r)  # sends, with the travel on top
        remember(r.mem.stalled_ops, TRAVEL, "Travel: no progress for 30s", 300, retry_basis(r.world, None))
        r.plan.demote_current("Travel: no progress for 30s", r.mem)
        s.serve_one(timeout=0)
        s.on_window(r)
        self.assertEqual([op["op"] for op in r.plan.ops_left()], ["gather_gems", "travel"])
        pending = (s.in_flight or []) + s.inbox
        self.assertIn("goal_stalled", [t["trigger"] for t in pending], "the planner hears of the stall next")

    def test_a_reply_of_only_stalled_ops_leaves_them_out(self):
        s, r = make(FakeLLM({"goals": [TRAVEL, BREAK]})), fake_runner()
        r.mem = stalled_on(TRAVEL, BREAK)
        round_trip(s, r)
        self.assertEqual(r.plan.ops_left(), [])
        self.assertTrue(all("left out" in why for why in s.rejected))

    def test_once_lapsed_it_goes_back_on_top(self):
        s, r = make(FakeLLM({"goals": [TRAVEL, GATHER]})), fake_runner()
        r.mem = stalled_on(TRAVEL)
        r.world.held_supplies = [InventorySupply(id=1, code="iron_key")]
        lapse(r.mem.stalled_ops, retry_basis(r.world, None))
        round_trip(s, r)
        self.assertEqual(r.plan.current()["op"], "travel")
        self.assertEqual(s.rejected, [])

    def test_state_lists_the_stalled_ops_with_why(self):
        m = stalled_on(BREAK)
        state = state_of(WorldModel(character_id=1, map_id=1, pos=(0, 0)), Plan([], dict(PARAM_DEFAULTS)), stalled_ops=m.stalled_ops)
        self.assertIn('stalled_ops=[{"at_tick": 100, "op": {"capability": "smash", "op": "break_block", "x": 5, "y": 5}, "why": "no progress"}]', state)
        self.assertNotIn("stalled_ops=", state_of(WorldModel(character_id=1), Plan([], dict(PARAM_DEFAULTS))))

    def test_the_contract_explains_the_rule(self):
        system = system_prompt()
        self.assertIn("stalled_ops", system)
        self.assertIn(f"no progress for {PLAN_STALL_SECONDS} s is moved just below", system)


if __name__ == "__main__":
    unittest.main()
