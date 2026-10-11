"""A108: one Use-self path for held consumables, the ``use_item`` op, a bigger
chest applied once held, and an ``equip`` that applies nothing is dropped."""

import random
import unittest

from agentrealm_agent.config import Policy
from agentrealm_agent.directives import PARAM_DEFAULTS, default_directives
from agentrealm_agent.healing import HealRefusal, heal_situation
from agentrealm_agent.item_table import DEFAULT_CARRY_CAPACITY, InventorySupply
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.memory import Memory
from agentrealm_agent.pack import reserved_supplies
from agentrealm_agent.plan import Plan, collect_rejections, validate_goal_op
from agentrealm_agent.states import PlayContext, dispatch
from agentrealm_agent.states.dispatch import PROBE_STATES
from agentrealm_agent.states.use_item import chest_to_apply
from agentrealm_agent.supplies import chest_upgrade, used_on_self
from agentrealm_agent.world import Entity, WorldModel

USE_SELF = {"verb": "Use", "target": {"kind": "self"}}


def world() -> WorldModel:
    w = WorldModel(character_id=1, map_id=7, pos=(0, 0), perception=3)
    w.health, w.max_health = 10, 10
    w.armed_code, w.armed_id = "bronze_sword", 2
    return w


def ctx(plan: Plan | None = None, m: Memory | None = None) -> PlayContext:
    return PlayContext(
        m or Memory(),
        Policy(kind="scripted", goals=[]),
        random.Random(0),
        directives=default_directives(),
        knowledge=KnowledgeBase.empty("sandbox"),
        plan=plan if plan is not None else Plan([], dict(PARAM_DEFAULTS)),
    )


def plan(*ops: dict) -> Plan:
    return Plan(list(ops), dict(PARAM_DEFAULTS))


def failures(c: PlayContext) -> list[dict]:
    return [s for s in c.memory.strategist_signals if s.get("trigger") == "goal_failed"]


class SelfUseRuleTest(unittest.TestCase):
    def test_potions_and_chests_are_used_on_yourself(self):
        for code in ("potion", "large_potion", "middle_chest", "red_chest"):
            self.assertTrue(used_on_self(code), code)
        # Food is eaten on pickup, weapons and tools are armed, a scroll is aimed.
        for code in ("apple", "bronze_sword", "raft", "torch", "teleport_scroll", "fake_thing", None):
            self.assertFalse(used_on_self(code), code)

    def test_a_chest_is_an_upgrade_only_when_bigger_than_the_pack(self):
        self.assertTrue(chest_upgrade("middle_chest", DEFAULT_CARRY_CAPACITY))
        self.assertFalse(chest_upgrade("middle_chest", 30))
        self.assertFalse(chest_upgrade("blue_chest", DEFAULT_CARRY_CAPACITY))
        self.assertFalse(chest_upgrade("potion", DEFAULT_CARRY_CAPACITY))


class ApplyChestTest(unittest.TestCase):
    def test_a_bigger_chest_held_is_armed_and_used_on_yourself(self):
        w = world()
        w.held_supplies = [InventorySupply(9, "middle_chest")]
        c = ctx()
        out = dispatch(w, c)
        self.assertEqual(out.state, "UseItem")
        self.assertEqual(out.intents[0], {"verb": "Arm", "supply_id": 9})
        self.assertEqual(out.intents[-1], USE_SELF)
        self.assertEqual(c.memory.heal_drink, 9)
        self.assertEqual(c.memory.heal_rearm, "bronze_sword", "the weapon goes back after, as after a drink")

    def test_the_biggest_chest_goes_first(self):
        w = world()
        w.held_supplies = [InventorySupply(9, "middle_chest"), InventorySupply(11, "red_chest")]
        self.assertEqual(chest_to_apply(w, Memory()).code, "red_chest")

    def test_no_chest_is_applied_once_the_pack_has_its_slots_or_it_is_the_starting_size(self):
        w = world()
        w.held_supplies = [InventorySupply(9, "middle_chest"), InventorySupply(10, "blue_chest")]
        w.carry_capacity = 30
        self.assertIsNone(chest_to_apply(w, Memory()))

    def test_a_chest_whose_use_did_nothing_is_not_resent_into_the_same_situation(self):
        w = world()
        w.held_supplies = [InventorySupply(9, "middle_chest")]
        m = Memory()
        m.heal_refusals[("use", 9)] = HealRefusal("bad_target", "hold", 5, heal_situation(w))
        self.assertIsNone(chest_to_apply(w, m))

    def test_not_with_a_hostile_in_range(self):
        w = world()
        w.held_supplies = [InventorySupply(9, "middle_chest")]
        w.entities = [Entity("npc", 40, (1, 0), "rat")]
        w.hostile_types.add(("npc", "rat"))
        self.assertNotEqual(dispatch(w, ctx()).state, "UseItem")

    def test_left_out_of_the_held_queue_probe(self):
        self.assertNotIn("UseItem", [s.name for s in PROBE_STATES])


class UseItemOpTest(unittest.TestCase):
    def test_validator_takes_only_a_consumable_used_on_yourself(self):
        self.assertIsNotNone(validate_goal_op({"op": "use_item", "code": "middle_chest"}))
        self.assertIsNotNone(validate_goal_op({"op": "use_item", "code": "potion"}))
        for code in ("bronze_sword", "raft"):
            op, why = collect_rejections(lambda: validate_goal_op({"op": "use_item", "code": code}))
            self.assertIsNone(op, code)
            self.assertTrue(any("used on yourself" in r for r in why), why)
        self.assertIsNone(validate_goal_op({"op": "use_item"}))

    def test_uses_the_named_item_then_is_done_once_it_is_used_up(self):
        w = world()
        w.health = 6
        w.held_supplies = [InventorySupply(9, "potion")]
        c = ctx(plan({"op": "use_item", "code": "potion"}))
        out = dispatch(w, c)
        self.assertEqual(out.state, "UseItem")
        self.assertEqual(out.intents[-1], USE_SELF)
        self.assertEqual(c.plan.used_id, 9)
        # Drunk: the potion is gone, the weapon still armed.
        w.held_supplies, c.memory.heal_rearm = [], None
        dispatch(w, c)
        self.assertIsNone(c.plan.current())
        done = [s for s in c.memory.strategist_signals if s.get("trigger") == "goal_done"]
        self.assertEqual(done[-1]["op"]["code"], "potion")

    def test_dropped_with_why_when_not_held(self):
        c = ctx(plan({"op": "use_item", "code": "middle_chest"}))
        dispatch(world(), c)
        self.assertIsNone(c.plan.current())
        self.assertIn("not held", failures(c)[-1]["reason"])

    def test_dropped_with_why_when_its_use_did_nothing(self):
        w = world()
        w.held_supplies = [InventorySupply(9, "potion")]
        m = Memory()
        m.heal_refusals[("use", 9)] = HealRefusal("bad_target", "forget", 5, ())
        c = ctx(plan({"op": "use_item", "code": "potion"}), m)
        dispatch(w, c)
        self.assertIsNone(c.plan.current())
        self.assertIn("refused or did nothing", failures(c)[-1]["reason"])

    def test_its_item_is_reserved_from_drops(self):
        w = world()
        w.held_supplies = [InventorySupply(9, "middle_chest")]
        held = reserved_supplies(w, [{"op": "use_item", "code": "middle_chest"}], None)
        self.assertEqual([s.id for s in held], [9])

    def test_progress_survives_a_probe_snapshot_and_resets_for_a_new_head(self):
        p = plan({"op": "use_item", "code": "potion"}, {"op": "wait", "seconds": 1, "why": "t"})
        p.used_id = 9
        saved = p.snapshot()
        p.used_id = None
        p.restore(saved)
        self.assertEqual(p.used_id, 9)
        p.finish_current("t")
        self.assertIsNone(p.used_id)


class EquipAppliesNothingTest(unittest.TestCase):
    def test_equip_of_a_chest_is_dropped_not_done(self):
        w = world()
        w.held_supplies = [InventorySupply(9, "middle_chest")]
        w.carry_capacity = 30  # no chest to apply, so Equip runs
        c = ctx(plan({"op": "equip", "code": "middle_chest"}))
        dispatch(w, c)
        self.assertIsNone(c.plan.current())
        self.assertIn("use_item", failures(c)[-1]["reason"])
        self.assertFalse([s for s in c.memory.strategist_signals if s.get("trigger") == "goal_done"])

    def test_equip_of_an_item_not_held_is_dropped(self):
        c = ctx(plan({"op": "equip", "code": "iron_mail"}))
        dispatch(world(), c)
        self.assertIn("not held", failures(c)[-1]["reason"])

    def test_equip_of_an_item_already_on_is_done(self):
        c = ctx(plan({"op": "equip", "code": "bronze_sword"}))
        dispatch(world(), c)
        self.assertIsNone(c.plan.current())
        self.assertFalse(failures(c))


if __name__ == "__main__":
    unittest.main()
