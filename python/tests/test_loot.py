"""A20: Loot state: Take, WithdrawFromChest, Drop junk when full; hearts first."""

import random
import unittest
from unittest import mock

from agentrealm_agent.brain import Memory, decide
from agentrealm_agent.config import Policy
from agentrealm_agent.item_table import DEFAULT_CARRY_CAPACITY, InventorySupply, carried_from_inventory
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.loot import (
    carry_slots_used,
    inventory_full,
    is_counter_supply,
    learn_loot_rejection,
    worst_droppable,
)
from agentrealm_agent.states import dispatch
from agentrealm_agent.states.base import PlayContext
from agentrealm_agent.world import Entity, WorldModel


def world(rows: list[str], at=(0, 0), perception=5) -> WorldModel:
    glyph = {".": "dirt", "#": "wall"}
    w = WorldModel(character_id=1, map_id=7, pos=at, perception=perception)
    for y, row in enumerate(rows):
        for x, g in enumerate(row):
            w.view.tiles[(x, y)] = glyph[g]
    w.terrain_center, w.terrain_map = at, 7
    return w


def scripted(**kw) -> Policy:
    return Policy(kind="scripted", **kw)


def ctx(policy=None, kb=None) -> PlayContext:
    return PlayContext(Memory(), policy or scripted(), random.Random(0), knowledge=kb)


def priced(**prices) -> KnowledgeBase:
    kb = KnowledgeBase("sandbox")
    for code, gems in prices.items():
        kb.items[code] = {"gem_price": gems}
    return kb


def full_inventory(w: WorldModel, *, junk: str = "torch") -> None:
    """Ten slots: knife armed, nine junk held."""
    w.armed_code = "pocket_knife"
    w.worn_codes = {}
    w.held_supplies = [InventorySupply(i, junk) for i in range(1, 10)]
    w.chest_supplies = []


class LootPriorityTest(unittest.TestCase):
    def test_life_codes_unknown_so_no_counter_supply(self):
        self.assertFalse(is_counter_supply("heart"))
        self.assertFalse(is_counter_supply("gem"))

    def test_heart_beats_sword_once_the_life_code_is_known(self):
        w = world(["...", "...", "..."], at=(1, 1))
        w.entities = [Entity("supply", 2, (1, 2), "bronze_sword"), Entity("supply", 3, (2, 1), "heart")]
        with mock.patch("agentrealm_agent.loot.UNKNOWN_LIFE_SUPPLY_CODES", frozenset({"heart"})):
            d = decide(w, Memory(), scripted(), random.Random(0), knowledge=priced(bronze_sword=15))
        self.assertEqual(d.intent, {"verb": "Take", "supply_id": 3})

    def test_counter_supply_taken_with_a_full_pack(self):
        w = world(["...", "...", "..."], at=(1, 1))
        full_inventory(w)
        w.entities = [Entity("supply", 3, (2, 1), "heart")]
        with mock.patch("agentrealm_agent.loot.UNKNOWN_LIFE_SUPPLY_CODES", frozenset({"heart"})):
            out = dispatch(w, ctx())
        self.assertEqual(out.intents, [{"verb": "Take", "supply_id": 3}])

    def test_priced_supply_is_shop_not_loot(self):
        w = world(["...", "...", "..."], at=(1, 1))
        w.entities = [Entity("supply", 5, (1, 2), "potion", gem_price=2)]
        d = decide(w, Memory(), scripted(goals=["hold"]), random.Random(0))
        self.assertIsNone(d.intent, d.reason)

    def test_loot_walks_to_a_supply_in_sight(self):
        w = world(["....", "...."], at=(0, 0))
        w.entities = [Entity("supply", 8, (3, 0), "apple")]
        out = dispatch(w, ctx(scripted(goals=["explore"])))
        self.assertEqual(out.state, "Loot")
        self.assertEqual(out.intents, [{"verb": "SetPosition", "x": 1, "y": 0}])

    def test_pickup_off_leaves_supplies(self):
        w = world(["...", "...", "..."], at=(1, 1))
        w.entities = [Entity("supply", 8, (1, 2), "apple")]
        out = dispatch(w, ctx(scripted(pickup=False, goals=["hold"])))
        self.assertNotEqual(out.state, "Loot")
        self.assertIsNone(out.intents)


class FullPackTest(unittest.TestCase):
    def test_drop_junk_before_take_when_full(self):
        w = world(["...", "...", "..."], at=(1, 1))
        full_inventory(w)
        w.entities = [Entity("supply", 99, (1, 2), "bronze_sword")]
        out = dispatch(w, ctx(kb=priced(bronze_sword=15)))
        self.assertEqual(out.state, "Loot")
        self.assertEqual(out.intents, [{"verb": "Drop", "supply_id": 1}])

    def test_full_pack_skips_junk_and_explore_runs(self):
        # Regression: Loot used to claim the round and send nothing ("carry full").
        w = world(["....", "...."], at=(1, 1))
        full_inventory(w)
        w.entities = [Entity("supply", 99, (1, 0), "torch")]
        c = ctx(scripted(goals=["explore"]))
        for _ in range(3):
            out = dispatch(w, c)
            self.assertNotEqual(out.state, "Loot")
            self.assertEqual(out.intents[0]["verb"], "SetPosition", out.reason)

    def test_full_pack_of_knives_drops_nothing(self):
        w = world(["...", "...", "..."], at=(1, 1))
        full_inventory(w, junk="pocket_knife")
        self.assertIsNone(worst_droppable(w, {}))
        w.entities = [Entity("supply", 99, (1, 2), "bronze_sword")]
        out = dispatch(w, ctx(scripted(goals=["hold"]), kb=priced(bronze_sword=15)))
        self.assertNotEqual(out.state, "Loot")
        self.assertIsNone(out.intents)

    def test_stowed_supplies_count_but_are_not_dropped(self):
        w = world(["..."], at=(1, 0))
        w.armed_code = "pocket_knife"
        w.chest_supplies = [InventorySupply(i, "torch") for i in range(1, 10)]
        self.assertEqual(carry_slots_used(w), 10)
        self.assertTrue(inventory_full(w))
        self.assertIsNone(worst_droppable(w, {}))


class StallTest(unittest.TestCase):
    def test_unreachable_supply_falls_through_to_explore(self):
        w = world([".#..", "##..", "...."], at=(0, 0))
        w.entities = [Entity("supply", 8, (3, 0), "apple")]
        out = dispatch(w, ctx(scripted(goals=["hold"])))
        self.assertNotEqual(out.state, "Loot")

    def test_loot_never_claims_a_round_without_an_intent(self):
        cases = []
        w = world(["....", "...."], at=(1, 1))
        full_inventory(w)
        w.entities = [Entity("supply", 99, (1, 0), "torch")]
        cases.append(w)
        w = world(["....", "...."], at=(1, 1))
        full_inventory(w)
        w.entities = [Entity("chest", 50, (2, 1))]
        w.chest_contents[50] = [InventorySupply(70, "torch")]
        cases.append(w)
        for w in cases:
            c = ctx(scripted(goals=["explore"]))
            for _ in range(3):
                out = dispatch(w, c)
                if out.state == "Loot":
                    self.assertIsNotNone(out.intents, out.reason)


class ChestTest(unittest.TestCase):
    def test_withdraw_best_supply_from_adjacent_chest(self):
        w = world(["...", "...", "..."], at=(1, 1))
        w.entities = [Entity("chest", 50, (2, 1))]
        w.chest_contents[50] = [InventorySupply(70, "torch"), InventorySupply(71, "bronze_sword")]
        out = dispatch(w, ctx(kb=priced(bronze_sword=15)))
        self.assertEqual(out.state, "Loot")
        self.assertEqual(out.intents, [{"verb": "WithdrawFromChest", "chest_id": 50, "supply_ids": [71]}])

    def test_full_pack_drops_junk_before_withdraw(self):
        w = world(["...", "...", "..."], at=(1, 1))
        full_inventory(w)
        w.entities = [Entity("chest", 50, (2, 1))]
        w.chest_contents[50] = [InventorySupply(71, "bronze_sword")]
        out = dispatch(w, ctx(kb=priced(bronze_sword=15)))
        self.assertEqual(out.intents, [{"verb": "Drop", "supply_id": 1}])

    def test_full_pack_skips_chest_of_junk(self):
        w = world(["...", "...", "..."], at=(1, 1))
        full_inventory(w)
        w.entities = [Entity("chest", 50, (2, 1))]
        w.chest_contents[50] = [InventorySupply(71, "torch")]
        out = dispatch(w, ctx(scripted(goals=["hold"])))
        self.assertNotEqual(out.state, "Loot")
        self.assertIsNone(out.intents)

    def test_empty_chest_is_not_a_target(self):
        w = world(["...", "...", "..."], at=(1, 1))
        w.entities = [Entity("chest", 50, (2, 1))]
        w.chest_contents[50] = []
        out = dispatch(w, ctx(scripted(goals=["hold"])))
        self.assertNotEqual(out.state, "Loot")

    def test_death_chest_is_left_to_recover(self):
        # No safe tile known (A7), so Recover waits and Loot must not go around it.
        w = world(["...", "...", "..."], at=(1, 1))
        w.death_chest = (7, (2, 1), 50)
        w.entities = [Entity("chest", 50, (2, 1))]
        w.chest_contents[50] = [InventorySupply(71, "bronze_sword")]
        out = dispatch(w, ctx(scripted(goals=["hold"]), kb=priced(bronze_sword=15)))
        self.assertNotIn(out.state, ("Loot", "Recover"))
        self.assertIsNone(out.intents)


class LootHostileTest(unittest.TestCase):
    def test_flee_reflex_runs_before_a_pickup(self):
        w = world(["...", "...", "..."], at=(1, 1))
        w.entities = [Entity("supply", 8, (1, 2), "apple"), Entity("npc", 5, (2, 1))]
        out = dispatch(w, ctx())
        self.assertEqual(out.state, "Loot")
        self.assertEqual(out.intents[0]["verb"], "SetPosition")
        self.assertTrue(out.reason.startswith("flee"), out.reason)


class InventoryParseTest(unittest.TestCase):
    def test_carried_from_inventory(self):
        held, stowed, armed, worn = carried_from_inventory({
            "gems": 4,
            "armed": {"id": 9, "supply_subtype_code": "pocket_knife"},
            "worn": {"head": {"id": 11, "supply_subtype_code": "bronze_helm"}},
            "held": [{"id": 12, "supply_subtype_code": "torch"}, {"supply_subtype_code": "no_id"}, "junk"],
            "chest": [{"id": 13, "supply_subtype_code": "apple"}],
        })
        self.assertEqual(held, [InventorySupply(12, "torch")])
        self.assertEqual(stowed, [InventorySupply(13, "apple")])
        self.assertEqual(armed, "pocket_knife")
        self.assertEqual(worn, {"head": "bronze_helm"})

    def test_missing_inventory_is_empty(self):
        self.assertEqual(carried_from_inventory(None), ([], [], None, {}))

    def test_snapshot_fills_the_carry_model(self):
        w = WorldModel(character_id=1)
        w.apply_observation({"complete": True, "snapshot": {"inventory": {
            "armed": {"id": 9, "supply_subtype_code": "pocket_knife"},
            "worn": {},
            "held": [{"id": 12, "supply_subtype_code": "torch"}],
            "chest": [],
        }}})
        self.assertEqual(carry_slots_used(w), 2)
        self.assertEqual(w.carry_capacity, DEFAULT_CARRY_CAPACITY)


class RejectionTest(unittest.TestCase):
    def test_drop_not_transferable_is_never_dropped_again(self):
        w = world(["..."], at=(1, 0))
        full_inventory(w)
        learn_loot_rejection(w, {"verb": "Drop", "supply_id": 1}, "not_transferable")
        self.assertIn(1, w.undroppable)
        self.assertEqual(worst_droppable(w, {}).id, 2)

    def test_carry_capacity_full_lowers_capacity_to_what_is_carried(self):
        w = world(["..."], at=(1, 0))
        w.armed_code = "pocket_knife"
        w.held_supplies = [InventorySupply(i, "torch") for i in range(1, 6)]
        learn_loot_rejection(w, {"verb": "Take", "supply_id": 40}, "carry_capacity_full")
        self.assertEqual(w.carry_capacity, 6)
        self.assertTrue(inventory_full(w))

    def test_carry_capacity_full_with_unread_inventory_learns_nothing(self):
        w = world(["..."], at=(1, 0))
        learn_loot_rejection(w, {"verb": "WithdrawFromChest", "chest_id": 3}, "carry_capacity_full")
        self.assertEqual(w.carry_capacity, DEFAULT_CARRY_CAPACITY)

    def test_respawn_restores_the_default_capacity(self):
        w = WorldModel(character_id=1)
        w.carry_capacity = 4
        w.apply_events([{"tick": 5, "events": [{"kind": "Respawned", "map_id": 1, "x": 2, "y": 3}]}])
        self.assertEqual(w.carry_capacity, DEFAULT_CARRY_CAPACITY)


if __name__ == "__main__":
    unittest.main()
