"""A154: each supply is in exactly one place, armed, worn or held, and ``Drop``
works on any of them (Manual §11 Where your supplies are)."""

import unittest

from agentrealm_adk.break_memory import held_supplies, pick_supply_for_capability
from agentrealm_adk.executor.supply_refusals import in_hand
from agentrealm_adk.fording import ford_queue
from agentrealm_adk.healing import potion_count
from agentrealm_adk.item_table import InventorySupply, worn_ids_from_inventory
from agentrealm_adk.loot import droppable_supplies
from agentrealm_adk.memory import Memory
from agentrealm_adk.pack import reserved_supplies
from agentrealm_adk.world import WorldModel
from tests.test_fording import SWORD, lake


def world(held=(), armed: tuple[int, str] | None = None) -> WorldModel:
    w = WorldModel(character_id=1)
    w.held_supplies = [InventorySupply(sid, code) for sid, code in held]
    if armed is not None:
        w.armed_id, w.armed_code = armed
    return w


class ArmedIsNeverHeldTest(unittest.TestCase):
    def test_an_armed_potion_counts_beside_a_held_one(self):
        w = world(held=[(4, "small_potion")], armed=(5, "small_potion"))
        self.assertEqual(potion_count(w), 2, "two potions, one in each place")

    def test_an_armed_potion_counts_without_its_id(self):
        w = world()
        w.armed_code = "small_potion"
        self.assertEqual(potion_count(w), 1)

    def test_in_hand_is_held_plus_armed(self):
        w = world(held=[(4, "small_potion")], armed=(5, "small_potion"))
        self.assertEqual(sorted(s.id for s in in_hand(w)), [4, 5])

    def test_break_sees_the_armed_supply_beside_a_held_one_of_its_code(self):
        w = world(held=[(6, "bronze_sword")], armed=(7, "bronze_sword"))
        self.assertEqual([s.id for s in held_supplies(w)], [6, -1], "the armed one is no longer hidden")
        self.assertEqual(pick_supply_for_capability(w, "cut").id, -1, "the armed one wins: no Arm")


class DropAnywhereTest(unittest.TestCase):
    def test_worn_ids_come_from_the_snapshot(self):
        inv = {"worn": {"body": {"id": 12, "supply_subtype_code": "bronze_mail"}, "head": {"supply_subtype_code": "x"}}}
        self.assertEqual(worn_ids_from_inventory(inv), {"body": 12})

    def test_held_armed_and_worn_are_offered(self):
        w = world(held=[(1, "torch")], armed=(2, "bronze_sword"))
        w.worn_codes, w.worn_ids = {"body": "bronze_mail"}, {"body": 3}
        self.assertEqual([(s.id, s.code) for s in droppable_supplies(w)], [(1, "torch"), (2, "bronze_sword"), (3, "bronze_mail")])

    def test_the_kit_and_a_refused_drop_are_not(self):
        w = world(held=[(1, "torch")], armed=(2, "pocket_knife"))
        w.worn_codes, w.worn_ids = {"body": "bronze_mail"}, {"body": 3}
        w.undroppable.add(3)
        self.assertEqual([s.id for s in droppable_supplies(w)], [1])

    def test_the_plan_reserves_an_armed_supply(self):
        w = world(held=[(1, "torch")], armed=(2, "bronze_sword"))
        reserved = reserved_supplies(w, [{"op": "fight_boss", "x": 0, "y": 0, "armed": "bronze_sword"}], None)
        self.assertEqual([s.id for s in reserved], [2], "the armed sword is kept for the boss")

    def test_a_drop_of_the_worn_fording_supply_on_water_is_cut(self):
        w = lake(at=(3, 1), held=("bronze_sword",), armed="bronze_sword")
        w.worn_codes, w.worn_ids = {"accessory": "raft"}, {"accessory": 21}  # a worn fording supply
        out, why = ford_queue(w, Memory(), [{"verb": "Drop", "supply_id": 21}])
        self.assertEqual(out, [])
        self.assertIn("would strand", why)

    def test_a_drop_of_the_worn_fording_supply_ashore_is_kept(self):
        w = lake(at=(0, 1), held=("bronze_sword",), armed="bronze_sword")
        w.worn_codes, w.worn_ids = {"accessory": "raft"}, {"accessory": 21}  # a worn fording supply
        queue = [{"verb": "Drop", "supply_id": 21}]
        self.assertEqual(ford_queue(w, Memory(), queue), (queue, ""))

    def test_a_drop_of_the_armed_sword_on_water_with_sandals_worn_is_kept(self):
        w = lake(at=(3, 1), held=(), armed="bronze_sword")
        w.worn_codes, w.worn_ids = {"accessory": "raft"}, {"accessory": 21}  # a worn fording supply
        queue = [{"verb": "Drop", "supply_id": SWORD}]
        self.assertEqual(ford_queue(w, Memory(), queue), (queue, ""))


if __name__ == "__main__":
    unittest.main()
