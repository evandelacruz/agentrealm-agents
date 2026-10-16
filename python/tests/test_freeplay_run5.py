"""Free-play run 5 offline: memory snapshots, and the ``buy`` op (A64, A21).

1. A held-queue probe leaves memory as it was; that it files no refusal is
   ``tests/test_heal_refusal.py`` (``FiledOnceTest``).
2. A ``buy`` op was done once one of its item was held, so with 2 potions held
   each ``buy small_potion`` finished at once. It now buys one more.
"""

from __future__ import annotations

import unittest

from agentrealm_adk.directives import PARAM_DEFAULTS
from agentrealm_adk.item_table import InventorySupply
from agentrealm_adk.memory import Memory
from agentrealm_agent.plan import Plan, goal_done
from agentrealm_adk.world import WorldModel, ZoneFact

POTIONS = [InventorySupply(1, "pocket_knife"), InventorySupply(4, "small_potion"), InventorySupply(5, "small_potion")]


def hurt_world() -> WorldModel:
    """Map 7, all dirt, 7/20 health (low, A96), a knife armed and two potions held."""
    w = WorldModel(character_id=9, map_id=7, pos=(3, 3), perception=6, health=7, max_health=20)
    for x in range(8):
        for y in range(8):
            w.view.tiles[(x, y)] = "dirt"
    w.terrain_center, w.terrain_map = (3, 3), 7
    w.record_respawn_anchor(7, (0, 0))
    w.zones[7] = {(0, 0): ZoneFact(safe=True), (3, 3): ZoneFact(safe=False)}
    w.armed_code = "pocket_knife"
    w.held_supplies = list(POTIONS)
    return w


class MemorySnapshotTest(unittest.TestCase):
    def test_memory_snapshot_restores_named_fields_only(self):
        m = Memory(goal="safe", self_use=4)
        saved = m.snapshot()
        m.goal, m.self_use = "explore", 5
        m.restore(saved, ("self_use",))
        self.assertEqual((m.goal, m.self_use), ("explore", 4))
        m.restore(saved)
        self.assertEqual(m, saved)

    def test_a_snapshot_shares_the_search_caches(self):
        # A corridor search is search work, not a decision: never copied (cost).
        m = Memory()
        m.corridors["goto"] = object()
        saved = m.snapshot()
        self.assertIs(saved.corridors, m.corridors)
        self.assertIsNot(saved.nav, m.nav)


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

    def test_one_picked_up_for_free_finishes_the_buy(self):
        # One more held is what the buy is for: a free one saves the gems.
        w, plan = hurt_world(), buy_plan()
        plan.advance(w)
        w.held_supplies.append(InventorySupply(8, "small_potion"))  # Pickup took one off the ground
        plan.advance(w)
        self.assertIsNone(plan.current())

    def test_another_item_picked_up_does_not_finish_it(self):
        w, plan = hurt_world(), buy_plan("torch")
        plan.advance(w)
        w.held_supplies.append(InventorySupply(8, "small_potion"))
        plan.advance(w)
        self.assertEqual(plan.current(), {"op": "buy", "code": "torch"})

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
