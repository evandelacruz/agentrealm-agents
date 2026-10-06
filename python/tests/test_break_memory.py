"""Break memory and capability rules (A28)."""

import random
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agentrealm_agent import config
from agentrealm_agent.break_memory import (
    attempt_failed,
    attempt_open,
    break_key,
    capabilities_for_code,
    held_capabilities,
    nominate_on_path,
    pick_supply_for_capability,
    record_attempt,
)
from agentrealm_agent.config import CharacterConfig, Policy
from agentrealm_agent.directives import PARAM_DEFAULTS
from agentrealm_agent.item_table import InventorySupply
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.memory import Memory
from agentrealm_agent.navigation import stuck as nav_stuck
from agentrealm_agent.pathing import grid_params
from agentrealm_agent.plan import PLAN_STALL_SECONDS, Plan
from agentrealm_agent.runner import Runner
from agentrealm_agent.states import PlayContext, dispatch
from agentrealm_agent.states.break_state import BreakState
from agentrealm_agent.states.escape import EscapeState
from agentrealm_agent.states.intents import arm, use_block
from agentrealm_agent.world import Entity, WorldModel
from agentrealm_agent.zone_discovery import apply_zone


class BreakMemoryTest(unittest.TestCase):
    def test_break_key_shape(self):
        self.assertEqual(break_key(12, (10, 20), "cut"), "12,10,20,cut")

    def test_capabilities_from_manual_classes(self):
        self.assertEqual(capabilities_for_code("bronze_sword"), frozenset({"cut", "chop"}))
        self.assertEqual(capabilities_for_code("pocket_knife"), frozenset({"cut"}))
        self.assertEqual(capabilities_for_code("bronze_mallet"), frozenset({"smash"}))
        self.assertEqual(capabilities_for_code("matches"), frozenset({"burn"}))

    def test_unsourced_code_carries_nothing(self):
        # No substring guessing: a code GAME_NOTES does not name has no class.
        for code in ("iron_sword", "big_bomb", "matchbox", "torch_holder"):
            self.assertEqual(capabilities_for_code(code), frozenset(), code)

    def test_capability_learned_from_an_opened_break(self):
        kb = KnowledgeBase.empty("sandbox")
        record_attempt(kb, map_id=1, pos=(3, 4), capability="cut", result="opened", code="iron_sword")
        record_attempt(kb, map_id=1, pos=(5, 4), capability="chop", result="applied_no_effect", code="iron_sword")
        self.assertEqual(capabilities_for_code("iron_sword", kb), frozenset({"cut"}))
        self.assertEqual(capabilities_for_code("iron_mallet", kb), frozenset())

    def test_failed_pair_is_remembered(self):
        kb = KnowledgeBase.empty("sandbox")
        record_attempt(kb, map_id=1, pos=(3, 4), capability="cut", result="applied_no_effect")
        self.assertTrue(attempt_failed(kb, 1, (3, 4), "cut"))
        self.assertFalse(attempt_failed(kb, 1, (3, 4), "burn"))


def _sword_world(rows: list[str], at) -> WorldModel:
    glyph = {".": "dirt", "b": "bush", "#": "wall", "~": "lava"}
    w = WorldModel(character_id=1, map_id=7, pos=at, perception=5)
    for y, row in enumerate(rows):
        for x, g in enumerate(row):
            w.view.tiles[(x, y)] = glyph[g]
    w.terrain_center, w.terrain_map = at, 7
    w.held_supplies = [InventorySupply(5, "bronze_sword")]
    return w


class NominateOnPathTest(unittest.TestCase):
    def test_bush_off_the_route_is_not_nominated(self):
        # A bush right behind us is closer than the one in the way to the goal.
        w = _sword_world(["b.b.."], at=(1, 0))
        choice = nominate_on_path(w, None, (1, 0), (4, 0))
        self.assertIsNotNone(choice)
        self.assertEqual(choice.pos, (2, 0))

    def test_nothing_on_the_route_nominates_nothing(self):
        w = _sword_world(["b....", "....."], at=(1, 0))
        self.assertIsNone(nominate_on_path(w, None, (1, 0), (4, 0)))

    def test_worn_only_tool_nominates_nothing(self):
        # Break can only arm held supplies; a capable code that is only worn has nothing to arm.
        w = _sword_world([".b..."], at=(0, 0))
        w.held_supplies = []
        w.worn_codes = {"body": "bronze_sword"}
        self.assertEqual(held_capabilities(w), set())
        self.assertIsNone(nominate_on_path(w, None, (0, 0), (4, 0)))
        w.armed_code = "bronze_sword"
        self.assertEqual(held_capabilities(w), {"cut", "chop"})
        choice = nominate_on_path(w, None, (0, 0), (4, 0))
        self.assertIsNotNone(choice, "an armed tool still counts")
        self.assertEqual(choice.supply.code, "bronze_sword")

    def test_failed_pair_on_the_route_is_skipped(self):
        kb = KnowledgeBase.empty("sandbox")
        for cap in ("cut", "chop"):
            record_attempt(kb, map_id=7, pos=(2, 0), capability=cap, result="failed")
        w = _sword_world([".bb.."], at=(0, 0))
        self.assertEqual(nominate_on_path(w, kb, (0, 0), (4, 0)).pos, (1, 0))
        self.assertIsNone(nominate_on_path(w, kb, (1, 0), (4, 0)), "only (2, 0) is ahead, and it failed")


class EscapeNeverBreaksTest(unittest.TestCase):
    def test_boxed_in_by_bushes_is_not_escape(self):
        w = _sword_world(["bbb", "b.b", "bbb"], at=(1, 1))
        c = PlayContext(Memory(), Policy(kind="scripted", goals=["hold"], pickup=False), random.Random(0))
        self.assertFalse(EscapeState().guard(w, c))

    def test_hazard_boxed_in_by_bushes_sends_no_use(self):
        w = _sword_world(["bbb", "b~b", "bbb"], at=(1, 1))
        c = PlayContext(Memory(), Policy(kind="scripted", goals=["hold"], pickup=False), random.Random(0))
        self.assertTrue(EscapeState().guard(w, c))
        out = EscapeState().act(w, c)
        self.assertFalse(any(i.get("verb") in ("Use", "Arm") for i in out.intents or []), out)


class RunnerBreakResultTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.object(config, "STATE_DIR", Path(tmp.name))
        patch.start()
        self.addCleanup(patch.stop)
        cfg = CharacterConfig("T", "sandbox", Policy(goals=["hold"]), Path("t.toml"))
        self.kb = KnowledgeBase.empty("sandbox")
        r = Runner(cfg, None, 1, threading.Event(), out=lambda _: None, knowledge=self.kb)
        self.addCleanup(r.trace.close)
        w = _sword_world(["..b.."], at=(1, 0))
        r.world, r.mem = w, Memory(need_self=False, need_position=False)
        r.mem.pending_intents = [arm(5), use_block((2, 0))]
        r.mem.break_pending = (7, (2, 0), "cut")
        self.r = r

    def rejected(self, index: int, code: str) -> bool:
        res = {"tick": 3, "outcome": "rejected", "rejection": {"category": "state", "code": code}}
        return self.r.on_result(res, index)

    def test_rejected_use_clears_pending_and_marks_the_pair_failed(self):
        self.assertTrue(self.rejected(1, "target_out_of_range"))
        self.assertIsNone(self.r.mem.break_pending)
        self.assertTrue(attempt_failed(self.kb, 7, (2, 0), "cut"))

    def test_rejected_arm_marks_the_pair_failed(self):
        self.assertTrue(self.rejected(0, "supply_not_held"))
        self.assertIsNone(self.r.mem.break_pending)
        self.assertTrue(attempt_failed(self.kb, 7, (2, 0), "cut"))

    def test_cooldown_rejection_clears_pending_without_marking_failed(self):
        self.assertTrue(self.rejected(1, "attack_cooldown"))
        self.assertIsNone(self.r.mem.break_pending)
        self.assertFalse(attempt_failed(self.kb, 7, (2, 0), "cut"))

    def test_later_rejection_keeps_an_applied_break_pending(self):
        r = self.r
        self.assertFalse(r.on_result({"tick": 3, "outcome": "applied"}, 1))
        r.mem.pending_intents = [{"verb": "Step", "direction": "right"}]
        self.assertTrue(self.rejected(0, "not_traversable"))
        self.assertEqual(r.mem.break_pending, (7, (2, 0), "cut"), "its BlockChanged may still come")

    def test_applied_no_effect_marks_the_pair_failed(self):
        self.assertFalse(self.r.on_result({"tick": 3, "outcome": "applied_no_effect"}, 1))
        self.assertIsNone(self.r.mem.break_pending)
        self.assertTrue(attempt_failed(self.kb, 7, (2, 0), "cut"))

    def test_opened_block_records_and_resets_the_attempt(self):
        r, w, m = self.r, self.r.world, self.r.mem
        att = nav_stuck.track(m, w, "goto", (4, 0))
        att.level = nav_stuck.BREAK
        att.break_x, att.break_y, att.break_cap = 2, 0, "cut"
        self.assertFalse(r.on_result({"tick": 3, "outcome": "applied"}, 1))
        self.assertEqual(m.break_pending, (7, (2, 0), "cut"), "applied: wait for BlockChanged")
        w.view.tiles[(2, 0)] = "dirt"
        w.changed_blocks = [(7, (2, 0))]
        r._resolve_pending_break()
        self.assertIsNone(m.break_pending)
        self.assertTrue(attempt_open(self.kb, 7, (2, 0), "cut"))
        self.assertEqual(self.kb.breaks[break_key(7, (2, 0), "cut")]["block_after"], "dirt")
        self.assertEqual(att.level, nav_stuck.WALK)
        self.assertIsNone(nav_stuck.break_target(att))

    def test_opened_break_files_the_capability_on_the_item_row_for_tool_choice(self):
        # A46 through the real path: a break with an unlisted subtype armed
        # opens its block, the item row learns the capability, and Break's
        # tool choice uses it after the breaks row is overwritten.
        r, w, m = self.r, self.r.world, self.r.mem
        w.held_supplies = [InventorySupply(6, "iron_axe")]
        w.armed_code = "iron_axe"
        m.break_pending = (7, (2, 0), "chop")
        self.assertIsNone(pick_supply_for_capability(w, "chop", self.kb), "unlisted code: no class yet")
        self.assertFalse(r.on_result({"tick": 3, "outcome": "applied"}, 1))
        w.view.tiles[(2, 0)] = "dirt"
        w.changed_blocks = [(7, (2, 0))]
        r._resolve_pending_break()
        self.assertEqual(self.kb.items, {"iron_axe": {"capabilities": ["chop"]}})
        # The block grows back and a later try fails: the item keeps what it proved.
        record_attempt(self.kb, map_id=7, pos=(2, 0), capability="chop", result="applied_no_effect")
        self.assertEqual(pick_supply_for_capability(w, "chop", self.kb), InventorySupply(6, "iron_axe"))
        self.assertIsNone(pick_supply_for_capability(w, "cut", self.kb))

    def test_failed_break_files_no_capability(self):
        self.assertFalse(self.r.on_result({"tick": 3, "outcome": "applied_no_effect"}, 1))
        self.assertEqual(self.kb.items, {})

    def test_death_drops_pending_so_a_later_change_is_not_ours(self):
        r, w = self.r, self.r.world
        w.alive = False
        r._resolve_pending_break()
        self.assertIsNone(r.mem.break_pending)
        w.alive = True
        w.view.tiles[(2, 0)] = "dirt"
        w.changed_blocks = [(7, (2, 0))]
        r._resolve_pending_break()
        self.assertFalse(attempt_open(self.kb, 7, (2, 0), "cut"))


class BreakFromGuidedWalkTest(unittest.TestCase):
    def test_stuck_chest_walk_reaches_break(self):
        # Recover's walk to the chest hits step 2: Recover yields, Break swings.
        w = _sword_world(["....b."], at=(5, 0))
        w.apply_events(
            [{"tick": 5, "events": [{"kind": "Died", "cause": "killed", "chest_id": 80, "map_id": 7, "x": 0, "y": 0}]}]
        )
        w.map_id, w.pos = 7, (5, 0)
        apply_zone(w, 7, 1, 0, {"safe": True, "brightness": 1})
        m = Memory()
        att = nav_stuck.track(m, w, "chest", (1, 0))
        att.level = nav_stuck.BREAK
        att.break_x, att.break_y, att.break_cap = 4, 0, "cut"
        c = PlayContext(m, Policy(kind="scripted", goals=["hold"], pickup=True), random.Random(0))
        out = dispatch(w, c)
        self.assertIs(nav_stuck.active(m, w), att)
        self.assertIn("Recover: chest walk stuck: break", out.yielded)
        self.assertEqual(out.state, "Break", out.reason)
        self.assertEqual(out.intents, [arm(5), use_block((4, 0))])
        self.assertEqual(c.memory.break_pending, (7, (4, 0), "cut"))

    def test_break_on_a_failed_target_renominates_or_escalates(self):
        kb = KnowledgeBase.empty("sandbox")
        w = _sword_world(["....b."], at=(5, 0))
        m = Memory()
        att = nav_stuck.track(m, w, "goto", (0, 0))
        att.level = nav_stuck.BREAK
        att.break_x, att.break_y, att.break_cap = 4, 0, "cut"
        for cap in ("cut", "chop"):
            record_attempt(kb, map_id=7, pos=(4, 0), capability=cap, result="failed")
        c = PlayContext(m, Policy(kind="scripted", goals=["hold"], pickup=False), random.Random(0), knowledge=kb)
        out = BreakState().act(w, c)
        self.assertIsNone(out.intents, "no Use on a pair already refused")
        self.assertEqual(att.level, nav_stuck.REVEAL, "step 2 has nothing left: on to step 3")


class BreakPricingTest(unittest.TestCase):
    """Breakables are passable in the grid only at break time (A15 step 2)."""

    def setUp(self):
        self.w = _sword_world([".b..."], at=(0, 0))
        self.m = Memory()
        self.att = nav_stuck.track(self.m, self.w, "goto", (4, 0))
        self.policy = Policy(kind="scripted", goals=["goto"])

    def params(self, **kw):
        return grid_params(self.policy, set(), set(), m=self.m, w=self.w, **kw)

    def test_below_break_level_the_bush_stays_impassable(self):
        for level in (nav_stuck.WALK, nav_stuck.CAUTIOUS):
            self.att.level = level
            p = self.params()
            self.assertEqual(p.break_costs, {}, level)
            self.assertEqual(p.break_nominated, set(), level)

    def test_after_break_level_the_bush_stays_impassable(self):
        for level in (nav_stuck.REVEAL, nav_stuck.REVEALED, nav_stuck.ALT_ROUTE):
            self.att.level = level
            self.assertEqual(self.params().break_costs, {}, level)

    def test_at_break_level_the_route_bush_is_priced(self):
        self.att.level = nav_stuck.BREAK
        p = self.params()
        self.assertEqual(set(p.break_costs), {(1, 0)})
        self.assertEqual(p.break_nominated, {(1, 0)})

    def test_break_walking_to_its_target_prices_that_cell(self):
        self.assertEqual(set(self.params(break_goal=(1, 0)).break_costs), {(1, 0)})

    def test_off_route_breakables_are_not_priced(self):
        self.w = _sword_world([".b...", "b...."], at=(0, 0))
        self.att = nav_stuck.track(self.m, self.w, "goto", (4, 0))
        self.att.level = nav_stuck.BREAK
        self.assertEqual(set(self.params().break_costs), {(1, 0)})


class BreakRearmTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.object(config, "STATE_DIR", Path(tmp.name))
        patch.start()
        self.addCleanup(patch.stop)
        cfg = CharacterConfig("T", "sandbox", Policy(goals=["hold"]), Path("t.toml"))
        self.kb = KnowledgeBase.empty("sandbox")
        r = Runner(cfg, None, 1, threading.Event(), out=lambda _: None, knowledge=self.kb)
        self.addCleanup(r.trace.close)
        w = _sword_world(["..b.."], at=(1, 0))
        w.held_supplies = [InventorySupply(5, "bronze_mallet"), InventorySupply(9, "bronze_sword")]
        w.armed_code = "bronze_sword"
        r.world, r.mem = w, Memory(need_self=False, need_position=False)
        self.r = r
        self.ctx = PlayContext(
            r.mem, Policy(kind="scripted", goals=["hold"], pickup=False), random.Random(0), knowledge=self.kb
        )
        self.att = nav_stuck.track(r.mem, w, "goto", (4, 0))
        self.att.level = nav_stuck.BREAK
        self.att.break_x, self.att.break_y, self.att.break_cap = 2, 0, "smash"

    def test_previous_weapon_is_rearmed_after_a_successful_break(self):
        r, w, m = self.r, self.r.world, self.r.mem
        out = BreakState().act(w, self.ctx)
        self.assertEqual(out.intents, [arm(5), use_block((2, 0))])
        self.assertEqual(m.break_rearm, "bronze_sword")
        m.pending_intents = list(out.intents)
        w.armed_code = "bronze_mallet"
        self.assertFalse(r.on_result({"tick": 3, "outcome": "applied"}, 1))
        w.view.tiles[(2, 0)] = "dirt"
        w.changed_blocks = [(7, (2, 0))]
        r._resolve_pending_break()
        self.assertEqual(self.att.level, nav_stuck.WALK, "the way is open: walking again")
        self.assertEqual(self.kb.breaks[break_key(7, (2, 0), "smash")]["code"], "bronze_mallet")
        self.assertTrue(BreakState().guard(w, self.ctx), "Break still owes the re-arm")
        out = BreakState().act(w, self.ctx)
        self.assertEqual(out.intents, [arm(9)])
        self.assertIsNone(m.break_rearm)
        self.assertFalse(BreakState().guard(w, self.ctx))

    def test_no_rearm_while_the_break_is_in_flight(self):
        m = self.r.mem
        self.att.level = nav_stuck.WALK
        m.break_rearm, m.break_pending = "bronze_sword", (7, (2, 0), "smash")
        self.assertFalse(BreakState().guard(self.r.world, self.ctx))


    def stalled_plan(self) -> Plan:
        plan = Plan([{"op": "break_block", "x": 2, "y": 0, "capability": "smash"}], dict(PARAM_DEFAULTS))
        plan.stalled_since_tick = 0
        self.ctx.plan = plan
        return plan

    def test_a_use_try_on_the_plan_op_keeps_its_stall(self):
        # A Use that has not opened the block yet is a try, not progress (A34):
        # a refused or ineffective one cannot pin the stack.
        self.att.level = nav_stuck.WALK
        plan = self.stalled_plan()
        out = dispatch(self.r.world, self.ctx)
        self.assertEqual(out.state, "Break")
        self.assertEqual(out.intents, [arm(5), use_block((2, 0))])
        self.assertFalse(out.progress)
        self.assertEqual(plan.stalled_since_tick, 0)
        self.assertEqual(plan.acted, plan.current())  # acted on (A36), just not progress

    def test_walking_to_the_plan_op_clears_its_stall(self):
        self.att.level = nav_stuck.WALK
        w = self.r.world
        w.pos = (0, 0)
        w.view.tiles[(2, 0)] = "dirt"
        w.view.tiles[(4, 0)] = "bush"
        plan = Plan([{"op": "break_block", "x": 4, "y": 0, "capability": "smash"}], dict(PARAM_DEFAULTS))
        plan.stalled_since_tick = 0
        self.ctx.plan = plan
        out = dispatch(w, self.ctx)
        self.assertEqual(out.state, "Break")
        self.assertEqual(out.intents[-1], {"verb": "SetPosition", "x": 1, "y": 0})
        self.assertIsNone(plan.stalled_since_tick)
        self.assertEqual(plan.acted, plan.current())

    def test_a_reflex_keeps_the_stall(self):
        self.att.level = nav_stuck.WALK
        w = self.r.world
        w.entities = [Entity("supply", 77, (0, 0), "bronze_sword")]
        self.ctx.policy = Policy(kind="scripted", goals=["hold"], pickup=True)
        plan = self.stalled_plan()
        out = dispatch(w, self.ctx)
        self.assertEqual(out.state, "Pickup", "the pickup reflex took the round")
        self.assertEqual(plan.stalled_since_tick, 0)
        self.assertIsNone(plan.acted)
        # The Use tries that follow are no progress either, so the stall runs out on time.
        w.entities = []
        w.tick = PLAN_STALL_SECONDS * plan.tick_hz
        with self.assertLogs("agentrealm_agent.plan", "WARNING"):
            dispatch(w, self.ctx)
        self.assertIsNone(plan.current())
