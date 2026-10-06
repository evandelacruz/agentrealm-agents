"""A19: Equip scoring and state."""

import random
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agentrealm_agent import config
from agentrealm_agent.config import CharacterConfig, Policy
from agentrealm_agent.directives import PARAM_DEFAULTS, default_directives
from agentrealm_agent.equip import (
    best_equip_upgrade,
    compare,
    note_equip_result,
    sync_refusals,
    wear_slot,
)
from agentrealm_agent.item_table import FragmentMeta, InventorySupply
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.memory import Memory
from agentrealm_agent.plan import Plan
from agentrealm_agent.runner import Runner
from agentrealm_agent.states import PlayContext, dispatch
from agentrealm_agent.threat import ThreatTable
from agentrealm_agent.world import WorldModel


def equip_plan(**op) -> Plan:
    """A plan whose top op is ``equip`` (Equip runs only for it)."""
    return Plan([{"op": "equip", **op}], dict(PARAM_DEFAULTS))


def ctx(kb: KnowledgeBase | None = None, m: Memory | None = None, plan: Plan | None = None) -> PlayContext:
    return PlayContext(
        m or Memory(),
        Policy(kind="scripted", goals=["hold"]),
        random.Random(0),
        directives=default_directives(),
        knowledge=kb or KnowledgeBase.empty("sandbox"),
        plan=plan if plan is not None else equip_plan(),
    )


def world() -> WorldModel:
    return WorldModel(character_id=1, map_id=7, pos=(0, 0), perception=3)


def snapshot(inventory: dict) -> dict:
    return {"complete": True, "snapshot": {"inventory": inventory}}


def rats(amount: int = 5) -> ThreatTable:
    t = ThreatTable()
    t.record(("npc", "rat"), amount)
    return t


class SlotTest(unittest.TestCase):
    def test_slot_only_from_served_worn(self):
        w = world()
        # Substring look-alikes and unseen codes have no slot.
        for code in ("bronze_mail", "waxed_cloth", "elbow_pad", "legend_map", "bronze_helm"):
            self.assertIsNone(wear_slot(code, w))
        w.apply_observation(snapshot({"worn": {"body": {"id": 3, "supply_subtype_code": "bronze_mail"}}}))
        self.assertEqual(wear_slot("bronze_mail", w), "body")
        self.assertIsNone(wear_slot("small_potion", w))
        self.assertIsNone(wear_slot("bronze_sword", w))
        # Kept for the run once taken off.
        w.apply_observation(snapshot({"worn": {}, "held": [{"id": 3, "supply_subtype_code": "bronze_mail"}]}))
        self.assertEqual(wear_slot("bronze_mail", w), "body")

    def test_unknown_slot_triggers_learn_wear(self):
        w = world()
        w.held_supplies = [InventorySupply(8, "iron_mail")]
        items = {"iron_mail": {"gem_price": 60}}
        up = best_equip_upgrade(w, items, w.threat, Memory())
        assert up is not None
        self.assertEqual((up.learn_slot, up.supply_id, up.code), (True, 8, "iron_mail"))

    def test_only_sourced_weapons_are_armed(self):
        w = world()
        w.armed_code = "pocket_knife"
        w.held_supplies = [InventorySupply(5, "waxed_axe"), InventorySupply(6, "torch")]
        items = {"waxed_axe": {"gem_price": 40}, "torch": {"gem_price": 10}}
        up = best_equip_upgrade(w, items, w.threat, Memory())
        assert up is not None
        self.assertEqual((up.learn_slot, up.code), (True, "waxed_axe"))

    def test_fragments_and_consumables_skipped(self):
        w = world()
        w.armed_code = "pocket_knife"
        frag = FragmentMeta(composes_into="bronze_sword", piece_count=2, slot=0, missing_slots=(1,))
        w.held_supplies = [InventorySupply(5, "bronze_sword", frag), InventorySupply(6, "small_potion")]
        items = {"bronze_sword": {"gem_price": 15}, "small_potion": {"gem_price": 10}}
        self.assertIsNone(best_equip_upgrade(w, items, w.threat, Memory()))


class ScoringTest(unittest.TestCase):
    def test_measured_hits_on_shared_types_only(self):
        items = {
            "bronze_mail": {"damage_saved": {"rat": 3, "bat": 9}, "gem_price": 20},
            "iron_mail": {"damage_saved": {"rat": 4}, "gem_price": 60},
        }
        t = rats(5)
        t.record(("npc", "bat"), 2)
        self.assertEqual(compare("iron_mail", "bronze_mail", items, t, "damage_saved"), (20, 15))

    def test_falls_back_to_price_not_mixed_units(self):
        # Measured hits on one side only: compare prices, never hits against a price.
        items = {"bronze_mail": {"damage_saved": {"rat": 3}, "gem_price": 20}, "iron_mail": {"gem_price": 60}}
        self.assertEqual(compare("iron_mail", "bronze_mail", items, rats(), "damage_saved"), (60, 20))

    def test_incomparable_is_none(self):
        items = {"bronze_mail": {"damage_saved": {"rat": 3}}, "iron_mail": {"gem_price": 60}}
        self.assertIsNone(compare("iron_mail", "bronze_mail", items, rats(), "damage_saved"))

    def test_starting_kit_counts_as_free(self):
        items = {"bronze_sword": {"gem_price": 15}}
        self.assertEqual(compare("bronze_sword", "pocket_knife", items, ThreatTable(), "weapon_damage"), (15, 0))


class UpgradeTest(unittest.TestCase):
    def test_arms_bought_sword_over_knife(self):
        w = world()
        w.armed_code = "pocket_knife"
        w.held_supplies = [InventorySupply(5, "bronze_sword")]
        up = best_equip_upgrade(w, {"bronze_sword": {"gem_price": 15}}, w.threat, Memory())
        assert up is not None
        self.assertEqual((up.slot, up.supply_id, up.remove_first), ("armed", 5, False))

    def test_swaps_body_armor_on_clear_gain(self):
        m = Memory()
        w = world()
        w.worn_slots = {"bronze_mail": "body", "iron_mail": "body"}
        w.worn_codes = {"body": "bronze_mail"}
        w.held_supplies = [InventorySupply(8, "iron_mail")]
        items = {"bronze_mail": {"gem_price": 20}, "iron_mail": {"gem_price": 60}}
        up = best_equip_upgrade(w, items, w.threat, m)
        assert up is not None
        self.assertEqual((up.slot, up.supply_id, up.remove_first), ("body", 8, True))

    def test_no_swap_on_tie_or_noise(self):
        m = Memory()
        w = world()
        w.worn_slots = {"bronze_mail": "body", "iron_mail": "body"}
        w.worn_codes = {"body": "bronze_mail"}
        w.held_supplies = [InventorySupply(8, "iron_mail")]
        t = rats(5)
        for saved in (4, 5, 2):  # tie, one rolled point better, worse
            items = {"bronze_mail": {"damage_saved": {"rat": 4}}, "iron_mail": {"damage_saved": {"rat": saved}}}
            self.assertIsNone(best_equip_upgrade(w, items, t, m), saved)
        # Equal prices, and a small re-price, keep what is worn.
        for price in (20, 24):
            items = {"bronze_mail": {"gem_price": 20}, "iron_mail": {"gem_price": price}}
            self.assertIsNone(best_equip_upgrade(w, items, t, m), price)

    def test_no_swap_back_after_upgrade(self):
        w = world()
        w.armed_code = "bronze_sword"
        w.held_supplies = [InventorySupply(4, "pocket_knife")]
        items = {"bronze_sword": {"gem_price": 15}}
        self.assertIsNone(best_equip_upgrade(w, items, w.threat, Memory()))

    def test_leaves_deliberately_armed_tool(self):
        w = world()
        w.armed_code = "torch"  # Solve or Break armed it
        w.held_supplies = [InventorySupply(5, "bronze_sword")]
        items = {"bronze_sword": {"gem_price": 15}, "torch": {"gem_price": 10}}
        self.assertIsNone(best_equip_upgrade(w, items, w.threat, Memory()))

    def test_leaves_armed_slot_while_solve_or_break_borrows_it(self):
        w = world()
        w.armed_code = "pocket_knife"
        w.held_supplies = [InventorySupply(5, "bronze_sword")]
        items = {"bronze_sword": {"gem_price": 15}}
        self.assertIsNone(best_equip_upgrade(w, items, w.threat, Memory(), armed_owned=True))
        kb = KnowledgeBase.empty("sandbox")
        kb.items.update(items)
        for attr in ("solve_rearm", "break_rearm"):
            c = ctx(kb)
            setattr(c.memory, attr, "bronze_mallet")
            self.assertNotEqual(dispatch(w, c).state, "Equip", attr)
        # Heal (priority 1) puts its weapon back before Equip looks (A24).
        w.held_supplies = [InventorySupply(5, "bronze_sword"), InventorySupply(6, "bronze_mallet")]
        c = ctx(kb)
        c.memory.heal_rearm = "bronze_mallet"
        out = dispatch(w, c)
        self.assertEqual((out.state, out.intents), ("Heal", [{"verb": "Arm", "supply_id": 6}]))


class RefusalTest(unittest.TestCase):
    def setUp(self):
        self.w = world()
        self.w.armed_code = "pocket_knife"
        self.w.held_supplies = [InventorySupply(5, "bronze_sword")]
        self.kb = KnowledgeBase.empty("sandbox")
        self.kb.items["bronze_sword"] = {"gem_price": 15}

    def test_refused_arm_not_resent_until_inventory_changes(self):
        c = ctx(self.kb)
        out = dispatch(self.w, c)
        self.assertEqual(out.state, "Equip")
        note_equip_result(c.memory, self.w, out.intents[0], rejected=True)
        sync_refusals(c.memory, self.w)
        for _ in range(3):
            self.assertNotEqual(dispatch(self.w, c).state, "Equip")
        self.w.held_supplies = [InventorySupply(5, "bronze_sword"), InventorySupply(9, "apple")]
        sync_refusals(c.memory, self.w)
        c.plan = equip_plan()  # the refusal left nothing to equip, so that op finished
        out = dispatch(self.w, c)
        self.assertEqual(out.state, "Equip")
        self.assertEqual(out.intents, [{"verb": "Arm", "supply_id": 5}])

    def test_refused_wear_and_remove(self):
        m = Memory()
        w = world()
        w.worn_slots = {"bronze_mail": "body", "iron_mail": "body"}
        w.worn_codes = {"body": "bronze_mail"}
        w.held_supplies = [InventorySupply(8, "iron_mail")]
        items = {"bronze_mail": {"gem_price": 20}, "iron_mail": {"gem_price": 60}}
        note_equip_result(m, w, {"verb": "Wear", "supply_id": 8}, rejected=True)
        sync_refusals(m, w)
        self.assertIsNone(best_equip_upgrade(w, items, w.threat, m))
        m2 = Memory()
        note_equip_result(m2, w, {"verb": "Remove", "slot": "body"}, rejected=True)
        sync_refusals(m2, w)
        self.assertIsNone(best_equip_upgrade(w, items, w.threat, m2))
        w.worn_codes = {}
        w.held_supplies = [InventorySupply(7, "bronze_mail"), InventorySupply(8, "iron_mail")]
        sync_refusals(m2, w)
        self.assertIsNotNone(best_equip_upgrade(w, items, w.threat, m2))

    def test_other_verbs_ignored(self):
        m = Memory()
        note_equip_result(m, self.w, {"verb": "Drop", "supply_id": 5}, rejected=True)
        self.assertEqual(m.equip_refused, set())


class RunnerEquipResultTest(unittest.TestCase):
    """Only a rejected Equip intent is recorded as refused (runner ``on_result``)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.object(config, "STATE_DIR", Path(tmp.name))
        patch.start()
        self.addCleanup(patch.stop)
        cfg = CharacterConfig("T", "sandbox", Policy(goals=["hold"]), Path("t.toml"))
        r = Runner(cfg, None, 1, threading.Event(), out=lambda _: None, knowledge=KnowledgeBase.empty("sandbox"))
        self.addCleanup(r.trace.close)
        w = world()
        w.armed_code = "pocket_knife"
        w.worn_codes = {"body": "bronze_mail"}
        w.worn_slots = {"bronze_mail": "body", "iron_mail": "body"}
        w.held_supplies = [InventorySupply(5, "bronze_sword"), InventorySupply(8, "iron_mail")]
        r.world, r.mem = w, Memory(need_self=False, need_position=False)
        r.mem.state = "Equip"
        r.mem.pending_intents = [{"verb": "Arm", "supply_id": 5}, {"verb": "Remove", "slot": "body"}, {"verb": "Wear", "supply_id": 8}]
        self.r = r

    def test_applied_results_are_not_refusals(self):
        for i in range(3):
            self.assertFalse(self.r.on_result({"tick": 3, "outcome": "applied"}, i))
        self.assertEqual(self.r.mem.equip_refused, set())

    def test_rejected_results_are_refusals(self):
        res = {"tick": 3, "outcome": "rejected", "rejection": {"category": "state", "code": "invalid_target"}}
        intents = list(self.r.mem.pending_intents)
        self.assertTrue(self.r.on_result(res, 0))
        self.r.mem.pending_intents = intents  # a rejection drops the queue; replay the Wear
        self.assertTrue(self.r.on_result(dict(res), 2))
        self.assertEqual(self.r.mem.equip_refused, {("bronze_sword", "armed"), ("iron_mail", "body")})

    def test_applied_remove_clears_refused_remove(self):
        self.r.mem.equip_refused = {(None, "body")}
        self.assertFalse(self.r.on_result({"tick": 3, "outcome": "applied"}, 1))
        self.assertEqual(self.r.mem.equip_refused, set())


    def test_learn_wear_rejection_code_reaches_memory(self):
        w = self.r.world
        w.held_supplies = [InventorySupply(3, "legend_map"), InventorySupply(4, "odd_hat")]
        self.r.mem.pending_intents = [{"verb": "Wear", "supply_id": 3}]
        res = {"tick": 3, "outcome": "rejected", "rejection": {"category": "state", "code": "not_wearable"}}
        self.assertTrue(self.r.on_result(res, 0))
        self.r.mem.pending_intents = [{"verb": "Wear", "supply_id": 4}]
        res = {"tick": 4, "outcome": "rejected", "rejection": {"category": "state", "code": "worn_slot_occupied"}}
        self.assertTrue(self.r.on_result(res, 0))
        self.assertEqual(self.r.mem.equip_not_wearable, {"legend_map"})
        self.assertEqual(self.r.mem.equip_try_refused, {"odd_hat"})


class LearnWearTest(unittest.TestCase):
    """A55: discover worn slot by trying Wear from Equip."""

    def test_dispatch_learn_wear_without_remove(self):
        w = world()
        w.armed_code = "pocket_knife"
        w.held_supplies = [InventorySupply(8, "iron_mail")]
        kb = KnowledgeBase.empty("sandbox")
        kb.items["iron_mail"] = {"gem_price": 60}
        out = dispatch(w, ctx(kb))
        self.assertEqual(out.state, "Equip")
        self.assertEqual(out.intents, [{"verb": "Wear", "supply_id": 8}])

    def test_unpriced_loot_is_tried(self):
        w = world()
        w.held_supplies = [InventorySupply(8, "looted_helm")]
        up = best_equip_upgrade(w, {}, w.threat, Memory())
        assert up is not None
        self.assertEqual((up.learn_slot, up.code), (True, "looted_helm"))

    def test_not_wearable_never_retried(self):
        m = Memory()
        m.equip_not_wearable.add("legend_map")
        w = world()
        w.held_supplies = [InventorySupply(3, "legend_map")]
        self.assertIsNone(best_equip_upgrade(w, {}, w.threat, m))

    def test_transient_try_refusal_cleared_on_inventory_change(self):
        m = Memory()
        w = world()
        w.held_supplies = [InventorySupply(8, "iron_mail")]
        items = {"iron_mail": {"gem_price": 60}}
        note_equip_result(m, w, {"verb": "Wear", "supply_id": 8}, rejected=True, rejection_code="worn_slot_occupied")
        sync_refusals(m, w)
        self.assertIn("iron_mail", m.equip_try_refused)
        self.assertIsNone(best_equip_upgrade(w, items, w.threat, m))
        w.held_supplies = [InventorySupply(8, "iron_mail"), InventorySupply(9, "apple")]
        sync_refusals(m, w)
        up = best_equip_upgrade(w, items, w.threat, m)
        assert up is not None
        self.assertTrue(up.learn_slot)

    def test_not_wearable_from_rejection(self):
        m = Memory()
        w = world()
        w.held_supplies = [InventorySupply(3, "legend_map")]
        note_equip_result(m, w, {"verb": "Wear", "supply_id": 3}, rejected=True, rejection_code="not_wearable")
        self.assertIn("legend_map", m.equip_not_wearable)
        self.assertNotIn("legend_map", m.equip_try_refused)

    def test_learned_slot_then_scores_like_a19(self):
        w = world()
        w.armed_code = "pocket_knife"
        w.held_supplies = [InventorySupply(8, "iron_mail")]
        items = {"bronze_mail": {"gem_price": 20}, "iron_mail": {"gem_price": 60}}
        self.assertTrue(best_equip_upgrade(w, items, w.threat, Memory()).learn_slot)
        w.apply_observation(snapshot({"worn": {"body": {"id": 8, "supply_subtype_code": "iron_mail"}}}))
        w.apply_observation(
            snapshot({"worn": {}, "held": [{"id": 8, "supply_subtype_code": "iron_mail"}]})
        )
        self.assertEqual(wear_slot("iron_mail", w), "body")
        w.worn_codes = {"body": "bronze_mail"}
        w.worn_slots["bronze_mail"] = "body"
        up = best_equip_upgrade(w, items, w.threat, Memory())
        assert up is not None
        self.assertEqual((up.slot, up.code, up.remove_first), ("body", "iron_mail", True))

    def test_weapon_upgrade_before_learn_wear(self):
        w = world()
        w.armed_code = "pocket_knife"
        w.held_supplies = [InventorySupply(5, "bronze_sword"), InventorySupply(8, "iron_mail")]
        items = {"bronze_sword": {"gem_price": 15}, "iron_mail": {"gem_price": 60}}
        up = best_equip_upgrade(w, items, w.threat, Memory())
        assert up is not None
        self.assertEqual(up.slot, "armed")


class DispatchTest(unittest.TestCase):
    def test_equip_before_loot(self):
        w = world()
        w.armed_code = "pocket_knife"
        w.held_supplies = [InventorySupply(5, "bronze_sword")]
        kb = KnowledgeBase.empty("sandbox")
        kb.items["bronze_sword"] = {"gem_price": 15}
        out = dispatch(w, ctx(kb))
        self.assertEqual(out.state, "Equip")
        self.assertEqual(out.intents, [{"verb": "Arm", "supply_id": 5}])

    def test_wear_learned_from_served_worn(self):
        c = ctx()
        w = world()
        # The observation files bronze_mail under body; no dispatch needed.
        w.apply_observation(snapshot({"worn": {"body": {"id": 8, "supply_subtype_code": "bronze_mail"}}}))
        w.apply_observation({"delta": {"inventory": {"worn": {}, "held": [{"id": 8, "supply_subtype_code": "bronze_mail"}]}}})
        out = dispatch(w, c)
        self.assertEqual(out.state, "Equip")
        self.assertEqual(out.intents, [{"verb": "Wear", "supply_id": 8}])

    def test_guard_is_pure(self):
        w = world()
        w.armed_code = "pocket_knife"
        w.held_supplies = [InventorySupply(5, "bronze_sword")]
        kb = KnowledgeBase.empty("sandbox")
        kb.items["bronze_sword"] = {"gem_price": 15}
        c = ctx(kb)
        before = (dict(w.worn_slots), set(c.memory.equip_refused), c.memory.equip_refused_sig)
        self.assertEqual(dispatch(w, c).state, "Equip")
        self.assertEqual((dict(w.worn_slots), set(c.memory.equip_refused), c.memory.equip_refused_sig), before)

    def test_runs_only_for_an_equip_op(self):
        w = world()
        w.armed_code = "pocket_knife"
        w.held_supplies = [InventorySupply(5, "bronze_sword")]
        kb = KnowledgeBase.empty("sandbox")
        kb.items["bronze_sword"] = {"gem_price": 15}
        empty = Plan([], dict(PARAM_DEFAULTS))
        self.assertEqual(dispatch(w, ctx(kb, plan=empty)).state, "Explore")
        travel = Plan([{"op": "travel", "to": "point", "x": 2, "y": 0}], dict(PARAM_DEFAULTS))
        self.assertNotEqual(dispatch(w, ctx(kb, plan=travel)).state, "Equip")

    def test_op_finishes_when_nothing_is_left_to_equip(self):
        w = world()
        w.armed_code = "pocket_knife"
        w.held_supplies = [InventorySupply(5, "bronze_sword")]
        kb = KnowledgeBase.empty("sandbox")
        kb.items["bronze_sword"] = {"gem_price": 15}
        c = ctx(kb)
        self.assertEqual(dispatch(w, c).intents, [{"verb": "Arm", "supply_id": 5}])
        w.armed_code, w.held_supplies = "bronze_sword", []
        out = dispatch(w, c)
        self.assertNotEqual(out.state, "Equip")
        self.assertEqual(out.yielded, ["Equip: nothing to equip"])
        self.assertIsNone(c.plan.current())

    def test_op_with_a_code_finishes_when_that_code_is_not_the_upgrade(self):
        w = world()
        w.armed_code = "pocket_knife"
        w.held_supplies = [InventorySupply(5, "bronze_sword")]
        kb = KnowledgeBase.empty("sandbox")
        kb.items["bronze_sword"] = {"gem_price": 15}
        c = ctx(kb, plan=equip_plan(code="fake_helm"))
        self.assertNotEqual(dispatch(w, c).state, "Equip")
        self.assertIsNone(c.plan.current())
        c = ctx(kb, plan=equip_plan(code="bronze_sword"))
        self.assertEqual(dispatch(w, c).intents, [{"verb": "Arm", "supply_id": 5}])

    def test_not_scripted_or_dead(self):
        w = world()
        w.armed_code = "pocket_knife"
        w.held_supplies = [InventorySupply(5, "bronze_sword")]
        kb = KnowledgeBase.empty("sandbox")
        kb.items["bronze_sword"] = {"gem_price": 15}
        c = ctx(kb)
        c.policy = Policy(kind="wander")
        self.assertNotEqual(dispatch(w, c).state, "Equip")
        w.alive = False
        self.assertNotEqual(dispatch(w, ctx(kb)).state, "Equip")


if __name__ == "__main__":
    unittest.main()
