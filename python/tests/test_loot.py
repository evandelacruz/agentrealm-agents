"""A20: Loot state: Take, WithdrawFromChest, Drop junk when full; gems first (hearts first is A47)."""

import random
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agentrealm_agent import config

from agentrealm_agent.brain import Memory, decide
from agentrealm_agent.brain import Decision
from agentrealm_agent.config import CharacterConfig, Policy
from agentrealm_agent.item_table import DEFAULT_CARRY_CAPACITY, InventorySupply, carried_from_inventory
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.loot import (
    _observed_life_codes,
    carry_slots_used,
    droppable_supplies,
    inventory_full,
    is_counter_supply,
    learn_loot_applied,
    learn_loot_life_take,
    learn_loot_rejection,
    observe_life_supply_code,
    worst_droppable,
)
from agentrealm_agent.runner import Runner
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
    def test_gem_is_a_counter_supply(self):
        self.assertTrue(is_counter_supply("gem"))
        self.assertFalse(is_counter_supply("heart"))

    def test_gem_beats_sword(self):
        w = world(["...", "...", "..."], at=(1, 1))
        w.entities = [Entity("supply", 2, (1, 2), "bronze_sword"), Entity("supply", 3, (2, 1), "gem")]
        d = decide(w, Memory(), scripted(), random.Random(0), knowledge=priced(bronze_sword=15))
        self.assertEqual(d.intent, {"verb": "Take", "supply_id": 3})

    def test_heart_beats_sword_once_the_life_code_is_known(self):
        w = world(["...", "...", "..."], at=(1, 1))
        w.entities = [Entity("supply", 2, (1, 2), "bronze_sword"), Entity("supply", 3, (2, 1), "heart")]
        with mock.patch("agentrealm_agent.loot.LIFE_SUPPLY_CODES", frozenset({"heart"})):
            d = decide(w, Memory(), scripted(), random.Random(0), knowledge=priced(bronze_sword=15))
        self.assertEqual(d.intent, {"verb": "Take", "supply_id": 3})

    def test_counter_supply_taken_with_a_full_pack(self):
        w = world(["...", "...", "..."], at=(1, 1))
        full_inventory(w)
        w.entities = [Entity("supply", 3, (2, 1), "gem")]
        out = dispatch(w, ctx())
        self.assertEqual(out.intents, [{"verb": "Take", "supply_id": 3}])

    def test_gem_beats_all_other_adjacent_loot(self):
        w = world(["...", "...", "..."], at=(1, 1))
        w.entities = [
            Entity("supply", 2, (1, 2), "bronze_sword"),
            Entity("supply", 3, (0, 1), "apple"),
            Entity("supply", 4, (0, 0), "mystery"),
            Entity("supply", 9, (2, 2), "gem"),
        ]
        d = decide(w, Memory(), scripted(), random.Random(0), knowledge=priced(bronze_sword=500))
        self.assertEqual(d.intent, {"verb": "Take", "supply_id": 9})

    def test_walks_to_a_far_gem_past_other_loot(self):
        w = world([".....", ".....", "....."], at=(1, 1))
        # Sword is nearer (3 west of the gem); the step goes toward the gem.
        w.entities = [Entity("supply", 2, (3, 0), "bronze_sword"), Entity("supply", 9, (4, 2), "gem")]
        out = dispatch(w, ctx(kb=priced(bronze_sword=500)))
        self.assertEqual(out.state, "Loot")
        self.assertIn("loot gem", out.reason)

    def test_priced_shop_gem_is_ignored(self):
        w = world(["...", "...", "..."], at=(1, 1))
        w.entities = [Entity("supply", 5, (1, 2), "gem", gem_price=3)]
        d = decide(w, Memory(), scripted(goals=["hold"]), random.Random(0))
        self.assertIsNone(d.intent, d.reason)

    def test_full_pack_takes_gem_before_dropping_for_gear(self):
        w = world(["...", "...", "..."], at=(1, 1))
        full_inventory(w)
        w.entities = [Entity("supply", 99, (1, 2), "bronze_sword"), Entity("supply", 3, (2, 1), "gem")]
        out = dispatch(w, ctx(kb=priced(bronze_sword=15)))
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

    def test_unreachable_supply_lets_explore_send(self):
        # Boxed in with room to move; the apple is outside the box.
        w = world(["######", "#..#.#", "######"], at=(1, 1))
        w.entities = [Entity("supply", 8, (4, 1), "apple")]
        out = dispatch(w, ctx(scripted(goals=["wander"])))
        self.assertEqual(out.state, "Explore")
        self.assertIsNotNone(out.intents, out.reason)

    def test_chest_with_unread_contents_is_not_a_target(self):
        w = world(["...", "...", "..."], at=(1, 1))
        w.entities = [Entity("chest", 50, (2, 1))]
        out = dispatch(w, ctx(scripted(goals=["hold"])))
        self.assertNotEqual(out.state, "Loot")
        self.assertIsNone(out.intents)

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

    def test_walks_to_a_chest_in_sight(self):
        w = world(["....", "...."], at=(0, 0))
        w.entities = [Entity("chest", 50, (3, 0))]
        w.chest_contents[50] = [InventorySupply(71, "bronze_sword")]
        out = dispatch(w, ctx(scripted(goals=["hold"]), kb=priced(bronze_sword=15)))
        self.assertEqual(out.state, "Loot")
        self.assertEqual(out.intents, [{"verb": "SetPosition", "x": 1, "y": 0}])

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

    def test_ground_chest_beside_the_death_chest_is_still_looted(self):
        w = world(["...", "...", "..."], at=(1, 1))
        w.death_chest = (7, (2, 1), 50)
        w.entities = [Entity("chest", 50, (2, 1)), Entity("chest", 51, (0, 1))]
        w.chest_contents[50] = [InventorySupply(71, "bronze_sword")]
        w.chest_contents[51] = [InventorySupply(72, "apple")]
        out = dispatch(w, ctx(scripted(goals=["hold"]), kb=priced(bronze_sword=15)))
        self.assertEqual(out.state, "Loot")
        self.assertEqual(out.intents, [{"verb": "WithdrawFromChest", "chest_id": 51, "supply_ids": [72]}])


class LootHostileTest(unittest.TestCase):
    def test_flee_runs_before_a_pickup(self):
        # Fleeing is the Flee state (A9), which outranks Loot.
        w = world(["...", "...", "..."], at=(1, 1))
        w.entities = [Entity("supply", 8, (1, 2), "apple"), Entity("npc", 5, (2, 1))]
        out = dispatch(w, ctx())
        self.assertEqual(out.state, "Flee")
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

    def test_refused_drop_moves_on_to_the_next_junk(self):
        w = world(["...", "...", "..."], at=(1, 1))
        full_inventory(w)
        w.entities = [Entity("supply", 99, (1, 2), "bronze_sword")]
        c = ctx(kb=priced(bronze_sword=15))
        self.assertEqual(dispatch(w, c).intents, [{"verb": "Drop", "supply_id": 1}])
        learn_loot_rejection(w, {"verb": "Drop", "supply_id": 1}, "not_transferable")
        self.assertEqual(dispatch(w, c).intents, [{"verb": "Drop", "supply_id": 2}])

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


class A47CarryEdgeCasesTest(unittest.TestCase):
    def setUp(self):
        _observed_life_codes.clear()

    def test_middle_chest_take_sets_authored_capacity(self):
        w = WorldModel(character_id=1)
        entities = [Entity("supply", 50, (1, 0), "middle_chest")]
        learn_loot_applied(
            w,
            {"verb": "Take", "supply_id": 50},
            "applied",
            entities_before=entities,
        )
        self.assertEqual(w.carry_capacity, 30)

    def test_stowed_drop_supported_includes_chest_in_droppables(self):
        w = WorldModel(character_id=1)
        w.held_supplies = [InventorySupply(1, "torch")]
        w.chest_supplies = [InventorySupply(2, "apple")]
        self.assertEqual([s.id for s in droppable_supplies(w)], [1])
        learn_loot_applied(w, {"verb": "Drop", "supply_id": 2}, "applied", entities_before=[])
        w.chest_supplies = [InventorySupply(2, "apple")]
        self.assertEqual(sorted(s.id for s in droppable_supplies(w)), [1, 2])

    def test_life_code_learned_when_take_raises_lives(self):
        w = WorldModel(character_id=1)
        w.lives = 11
        entities = [Entity("supply", 9, (0, 0), "heart")]
        learn_loot_life_take(
            w,
            {"verb": "Take", "supply_id": 9},
            entities_before=entities,
            lives_before=10,
        )
        self.assertTrue(is_counter_supply("heart"))
        observe_life_supply_code("heart")  # idempotent
        self.assertTrue(is_counter_supply("heart"))


class RunnerRejectionTest(unittest.TestCase):
    """The runner feeds each rejected intent to learn_loot_rejection."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.object(config, "STATE_DIR", Path(tmp.name))
        patch.start()
        self.addCleanup(patch.stop)
        cfg = CharacterConfig("T", "default", "test", "sandbox", scripted(goals=["hold"]), Path("t.toml"))
        self.r = Runner(cfg, None, 1, threading.Event(), out=lambda _: None)
        self.addCleanup(self.r.trace.close)
        self.r.world = world(["..."], at=(1, 0))
        self.r.mem = Memory(need_self=False, need_position=False)

    def reject(self, intent: dict, code: str) -> None:
        self.r.intents_for(Decision(intent, "test"))
        rejection = {"category": "state", "code": code, "retryability": "permanent"}
        self.assertTrue(self.r.on_result({"tick": 30, "outcome": "rejected", "rejection": rejection}, 0))

    def test_drop_not_transferable_reaches_the_world(self):
        full_inventory(self.r.world)
        self.reject({"verb": "Drop", "supply_id": 1}, "not_transferable")
        self.assertEqual(self.r.world.undroppable, {1})

    def test_withdraw_carry_capacity_full_reaches_the_world(self):
        self.r.world.armed_code = "pocket_knife"
        self.r.world.held_supplies = [InventorySupply(i, "torch") for i in range(1, 4)]
        self.reject({"verb": "WithdrawFromChest", "chest_id": 50, "supply_ids": [71]}, "carry_capacity_full")
        self.assertEqual(self.r.world.carry_capacity, 4)


if __name__ == "__main__":
    unittest.main()
