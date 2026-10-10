"""Free-play run 5 offline: the held-queue probe and Heal's tries, and the ``buy`` op (A24, A64, A21).

1. The held-queue probe ran Heal, whose drink counted a try before anything
   was sent; the drink was not sent, and 6 probes wrote both potions off. It
   died at 2/10 holding both. The probe now leaves memory as it was, and a try
   counts only when the server rejects a drink (``Runner._note_heal_refused``).
2. A ``buy`` op was done once one of its item was held, so with 2 potions held
   each ``buy small_potion`` finished at once. It now buys one more.
"""

from __future__ import annotations

import threading
import unittest
from pathlib import Path

from agentrealm_agent.config import CharacterConfig, Policy
from agentrealm_agent.directives import PARAM_DEFAULTS
from agentrealm_agent.healing import HEAL_MAX_TRIES, carried_heal
from agentrealm_agent.item_table import InventorySupply
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.memory import Memory
from agentrealm_agent.plan import Plan, goal_done
from agentrealm_agent.runner import Runner
from agentrealm_agent.world import WorldModel, ZoneFact

POTIONS = [InventorySupply(1, "pocket_knife"), InventorySupply(4, "small_potion"), InventorySupply(5, "small_potion")]
DRINK = [{"verb": "Arm", "supply_id": 4}, {"verb": "Use", "target": {"kind": "self"}}]


def hurt_world() -> WorldModel:
    """Map 7, all dirt, 7/10 health, a knife armed and two potions held."""
    w = WorldModel(character_id=9, map_id=7, pos=(3, 3), perception=6, health=7, max_health=10)
    for x in range(8):
        for y in range(8):
            w.view.tiles[(x, y)] = "dirt"
    w.terrain_center, w.terrain_map = (3, 3), 7
    w.record_respawn_anchor(7, (0, 0))
    w.zones[7] = {(0, 0): ZoneFact(safe=True), (3, 3): ZoneFact(safe=False)}
    w.armed_code = "pocket_knife"
    w.held_supplies = list(POTIONS)
    return w


def make_runner(test: unittest.TestCase) -> Runner:
    cfg = CharacterConfig("T", "sandbox", Policy(kind="scripted", goals=[]), Path("t.toml"))
    r = Runner(cfg, None, 1, threading.Event(), out=lambda _: None, knowledge=KnowledgeBase.empty("sandbox"))
    test.addCleanup(r.trace.close)
    r.world = hurt_world()
    r.mem = Memory(need_self=False, need_position=False)
    return r


class ProbeLeavesMemoryTest(unittest.TestCase):
    def test_probes_spend_no_drink_tries(self):
        r = make_runner(self)
        # A walk queue is held; Heal picks a drink in each probe, which is not sent.
        r.mem.held_queue = {"queue_id": "q", "next_index": 0}
        r.mem.pending_intents = [{"verb": "Step", "direction": "north"}] * 5
        before = r.mem.snapshot()
        for _ in range(2 * HEAL_MAX_TRIES):
            self.assertIsNone(r.reflex_while_held())
        self.assertEqual(r.mem.heal_tries, {})
        self.assertEqual(r.mem, before, "a probe whose answer is not sent leaves memory as it was")
        self.assertIsNotNone(carried_heal(r.world, r.mem), "both potions are still drinkable")

    def test_memory_snapshot_restores_named_fields_only(self):
        m = Memory(goal="safe", heal_tries={("use", 4): 1})
        saved = m.snapshot()
        m.goal, m.heal_tries[("use", 4)] = "explore", 2
        m.restore(saved, ("heal_tries",))
        self.assertEqual((m.goal, m.heal_tries), ("explore", {("use", 4): 1}))
        m.restore(saved)
        self.assertEqual(m, saved)


class DrinkTriesCountRejectionsTest(unittest.TestCase):
    def sent(self, r: Runner, queue: list[dict]) -> None:
        r.mem.pending_intents, r.mem.pending_next_index = [dict(i) for i in queue], 0

    def test_a_rejected_use_counts_one_try(self):
        r = make_runner(self)
        self.sent(r, DRINK)
        r.on_result({"outcome": "applied"}, 0)
        r.on_result({"outcome": "rejected", "rejection": {"code": "cooldown"}}, 1)
        self.assertEqual(r.mem.heal_tries, {("use", 4): 1})

    def test_a_rejected_arm_counts_against_the_drink(self):
        r = make_runner(self)
        self.sent(r, [DRINK[0], {"verb": "Wait"}, DRINK[1]])
        r.on_result({"outcome": "rejected", "rejection": {"code": "not_held"}}, 0)
        self.assertEqual(r.mem.heal_tries, {("use", 4): 1})

    def test_an_applied_drink_counts_nothing(self):
        r = make_runner(self)
        self.sent(r, DRINK)
        r.on_result({"outcome": "applied"}, 0)
        r.on_result({"outcome": "applied"}, 1)
        self.assertEqual(r.mem.heal_tries, {})

    def test_a_rejected_weapon_arm_is_no_drink(self):
        r = make_runner(self)
        self.sent(r, [{"verb": "Arm", "supply_id": 1}])
        r.on_result({"outcome": "rejected", "rejection": {"code": "x"}}, 0)
        self.assertEqual(r.mem.heal_tries, {})

    def test_three_rejections_write_a_potion_off_and_the_next_is_drunk(self):
        r = make_runner(self)
        for _ in range(HEAL_MAX_TRIES):
            self.sent(r, DRINK)
            r.on_result({"outcome": "rejected", "rejection": {"code": "x"}}, 1)
        self.assertEqual(carried_heal(r.world, r.mem).id, 5)


def buy_plan(code: str = "small_potion") -> Plan:
    return Plan([{"op": "buy", "code": code}], dict(PARAM_DEFAULTS))


class BuyOneMoreTest(unittest.TestCase):
    def test_a_buy_with_two_held_is_not_done_until_a_third(self):
        w, plan = hurt_world(), buy_plan()
        plan.advance(w)
        self.assertEqual(plan.current(), {"op": "buy", "code": "small_potion"}, "2 held: the buy still owes one")
        w.held_supplies.append(InventorySupply(6, "small_potion"))
        self.assertTrue(goal_done(plan.current(), w, plan))
        plan.advance(w)
        self.assertIsNone(plan.current())

    def test_a_death_lowers_the_bar(self):
        w, plan = hurt_world(), buy_plan()
        plan.advance(w)
        w.held_supplies = []  # died: both potions dropped
        plan.advance(w)
        w.held_supplies = [InventorySupply(9, "small_potion")]
        plan.advance(w)
        self.assertIsNone(plan.current(), "one bought after the death is the buy")

    def test_a_buy_not_yet_on_top_is_not_done(self):
        w, plan = hurt_world(), buy_plan()
        self.assertFalse(goal_done(plan.current(), w, plan))

    def test_a_probe_leaves_the_bar_as_it_was(self):
        w, plan = hurt_world(), buy_plan()
        saved = plan.snapshot()
        plan.advance(w)
        plan.restore(saved)
        self.assertIsNone(plan.held_before)

    def test_a_second_buy_buys_a_second(self):
        w = hurt_world()
        plan = Plan([{"op": "buy", "code": "small_potion"}, {"op": "buy", "code": "small_potion"}], dict(PARAM_DEFAULTS))
        plan.advance(w)
        w.held_supplies.append(InventorySupply(6, "small_potion"))
        plan.advance(w)
        self.assertEqual(plan.index, 1, "the first buy is done, the second owes one more")
        w.held_supplies.append(InventorySupply(7, "small_potion"))
        plan.advance(w)
        self.assertIsNone(plan.current())


if __name__ == "__main__":
    unittest.main()
