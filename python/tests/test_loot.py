"""A20: Pickup reflex (Take, WithdrawFromChest, Drop junk when full; gems first, hearts first is A47)
and the Loot executor for ``fetch_item`` ops."""

import random
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agentrealm_agent import config, knowledge_base
from agentrealm_agent.brain import Decision, Memory, decide
from agentrealm_agent.config import CharacterConfig, Policy
from agentrealm_agent.directives import PARAM_DEFAULTS
from agentrealm_agent.item_table import (
    DEFAULT_CARRY_CAPACITY,
    InventorySupply,
    carried_from_inventory,
)
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.loot import (
    LIFE_SCORE,
    carry_slots_used,
    droppable_supplies,
    inventory_full,
    is_counter_supply,
    learn_life_code,
    learn_loot_rejection,
    loot_score,
    worst_droppable,
)
from agentrealm_agent.plan import Plan
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


def ctx(policy=None, kb=None, plan=None) -> PlayContext:
    # Equip has already tried the junk torch on and got not_wearable (A55), so it leaves Pickup the round.
    return PlayContext(
        Memory(equip_not_wearable={"torch"}), policy or scripted(), random.Random(0), knowledge=kb, plan=plan
    )


def fetch(code: str, **xy) -> Plan:
    return Plan([{"op": "fetch_item", "code": code, **xy}], dict(PARAM_DEFAULTS))


def priced(**prices) -> KnowledgeBase:
    kb = KnowledgeBase("sandbox")
    for code, gems in prices.items():
        kb.items[code] = {"gem_price": gems}
    return kb


LOOT_VERBS = ("Take", "Drop", "WithdrawFromChest")


def no_loot_intent(intents) -> bool:
    """No state took, dropped or withdrew anything (the safe default may still step)."""
    return not any(i.get("verb") in LOOT_VERBS for i in intents or [])


def full_inventory(w: WorldModel, *, junk: str = "torch") -> None:
    """Ten slots: knife armed, nine junk held."""
    w.armed_code = "pocket_knife"
    w.worn_codes = {}
    w.held_supplies = [InventorySupply(i, junk) for i in range(1, 10)]
    w.chest_supplies = []


class LootPriorityTest(unittest.TestCase):
    def test_gem_is_a_counter_supply(self):
        self.assertTrue(is_counter_supply("gem", {}))
        self.assertFalse(is_counter_supply("heart", {}))

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

    def test_far_gem_is_not_walked_to_without_a_fetch_op(self):
        # Pickup is a reflex for what is in reach; walking to loot is a plan `fetch_item` op.
        w = world([".....", ".....", "....."], at=(1, 1))
        w.entities = [Entity("supply", 9, (4, 2), "gem")]
        out = dispatch(w, ctx(kb=priced(bronze_sword=500)))
        self.assertNotIn(out.state, ("Loot", "Pickup"))

    def test_priced_shop_gem_is_ignored(self):
        w = world(["...", "...", "..."], at=(1, 1))
        w.entities = [Entity("supply", 5, (1, 2), "gem", gem_price=3)]
        d = decide(w, Memory(), scripted(goals=[]), random.Random(0))
        self.assertTrue(no_loot_intent([d.intent] if d.intent else None), d.reason)

    def test_full_pack_takes_gem_before_dropping_for_gear(self):
        w = world(["...", "...", "..."], at=(1, 1))
        full_inventory(w)
        w.entities = [Entity("supply", 99, (1, 2), "bronze_sword"), Entity("supply", 3, (2, 1), "gem")]
        out = dispatch(w, ctx(kb=priced(bronze_sword=15)))
        self.assertEqual(out.intents, [{"verb": "Take", "supply_id": 3}])

    def test_priced_supply_is_shop_not_loot(self):
        w = world(["...", "...", "..."], at=(1, 1))
        w.entities = [Entity("supply", 5, (1, 2), "potion", gem_price=2)]
        d = decide(w, Memory(), scripted(goals=[]), random.Random(0))
        self.assertTrue(no_loot_intent([d.intent] if d.intent else None), d.reason)

    def test_pickup_off_leaves_supplies(self):
        w = world(["...", "...", "..."], at=(1, 1))
        w.entities = [Entity("supply", 8, (1, 2), "apple")]
        out = dispatch(w, ctx(scripted(pickup=False, goals=[])))
        self.assertNotEqual(out.state, "Pickup")
        self.assertNotIn({"verb": "Take", "supply_id": 8}, out.intents or [])


class FullPackTest(unittest.TestCase):
    def test_drop_junk_before_take_when_full(self):
        w = world(["...", "...", "..."], at=(1, 1))
        full_inventory(w)
        w.entities = [Entity("supply", 99, (1, 2), "bronze_sword")]
        out = dispatch(w, ctx(kb=priced(bronze_sword=15)))
        self.assertEqual(out.state, "Pickup")
        self.assertEqual(out.intents, [{"verb": "Drop", "supply_id": 1}])

    def test_full_pack_skips_junk_and_explore_runs(self):
        # Regression: Loot used to claim the round and send nothing ("carry full").
        w = world(["....", "...."], at=(1, 1))
        full_inventory(w)
        w.entities = [Entity("supply", 99, (1, 0), "torch")]
        c = ctx(scripted(goals=["explore"]))
        for _ in range(3):
            out = dispatch(w, c)
            self.assertNotEqual(out.state, "Pickup")
            self.assertEqual(out.intents[0]["verb"], "SetPosition", out.reason)

    def test_full_pack_of_knives_drops_nothing(self):
        w = world(["...", "...", "..."], at=(1, 1))
        full_inventory(w, junk="pocket_knife")
        self.assertIsNone(worst_droppable(w, {}))
        w.entities = [Entity("supply", 99, (1, 2), "bronze_sword")]
        out = dispatch(w, ctx(scripted(goals=[]), kb=priced(bronze_sword=15)))
        self.assertNotEqual(out.state, "Pickup")
        self.assertTrue(no_loot_intent(out.intents), out.intents)

    def test_stowed_supplies_count_but_are_not_dropped(self):
        w = world(["..."], at=(1, 0))
        w.armed_code = "pocket_knife"
        w.chest_supplies = [InventorySupply(i, "torch") for i in range(1, 10)]
        self.assertEqual(carry_slots_used(w), 10)
        self.assertTrue(inventory_full(w))
        self.assertIsNone(worst_droppable(w, {}))


class StallTest(unittest.TestCase):
    def test_unreachable_fetch_target_yields(self):
        w = world([".#..", "##..", "...."], at=(0, 0))
        w.entities = [Entity("supply", 8, (3, 0), "apple")]
        out = dispatch(w, ctx(plan=fetch("apple")))
        self.assertNotEqual(out.state, "Loot")

    def test_unreachable_fetch_target_lets_the_safe_default_move(self):
        # Boxed in with room to move; the apple is outside the box.
        w = world(["######", "#..#.#", "######"], at=(1, 1))
        w.entities = [Entity("supply", 8, (4, 1), "apple")]
        plan = fetch("apple")
        out = dispatch(w, ctx(plan=plan))
        self.assertEqual(out.state, "Explore")
        self.assertIsNotNone(out.intents, out.reason)
        self.assertIsNotNone(plan.stalled_since_tick)  # the fetch op's stall clock runs

    def test_chest_with_unread_contents_is_not_a_target(self):
        w = world(["...", "...", "..."], at=(1, 1))
        w.entities = [Entity("chest", 50, (2, 1))]
        out = dispatch(w, ctx(scripted(goals=[])))
        self.assertNotEqual(out.state, "Pickup")
        self.assertTrue(no_loot_intent(out.intents), out.intents)

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
                if out.state == "Pickup":
                    self.assertIsNotNone(out.intents, out.reason)


class ChestTest(unittest.TestCase):
    def test_withdraw_best_supply_from_adjacent_chest(self):
        w = world(["...", "...", "..."], at=(1, 1))
        w.entities = [Entity("chest", 50, (2, 1))]
        w.chest_contents[50] = [InventorySupply(70, "torch"), InventorySupply(71, "bronze_sword")]
        out = dispatch(w, ctx(kb=priced(bronze_sword=15)))
        self.assertEqual(out.state, "Pickup")
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
        out = dispatch(w, ctx(scripted(goals=[])))
        self.assertNotEqual(out.state, "Pickup")
        self.assertTrue(no_loot_intent(out.intents), out.intents)

    def test_empty_chest_is_not_a_target(self):
        w = world(["...", "...", "..."], at=(1, 1))
        w.entities = [Entity("chest", 50, (2, 1))]
        w.chest_contents[50] = []
        out = dispatch(w, ctx(scripted(goals=[])))
        self.assertNotEqual(out.state, "Pickup")

    def test_death_chest_is_left_to_recover(self):
        # No safe tile known (A7), so Recover waits and Loot must not go around it.
        w = world(["...", "...", "..."], at=(1, 1))
        w.death_chest = (7, (2, 1), 50)
        w.entities = [Entity("chest", 50, (2, 1))]
        w.chest_contents[50] = [InventorySupply(71, "bronze_sword")]
        out = dispatch(w, ctx(scripted(goals=[]), kb=priced(bronze_sword=15)))
        self.assertNotIn(out.state, ("Pickup", "Recover"))
        self.assertTrue(no_loot_intent(out.intents), out.intents)

    def test_ground_chest_beside_the_death_chest_is_still_looted(self):
        w = world(["...", "...", "..."], at=(1, 1))
        w.death_chest = (7, (2, 1), 50)
        w.entities = [Entity("chest", 50, (2, 1)), Entity("chest", 51, (0, 1))]
        w.chest_contents[50] = [InventorySupply(71, "bronze_sword")]
        w.chest_contents[51] = [InventorySupply(72, "apple")]
        out = dispatch(w, ctx(scripted(goals=[]), kb=priced(bronze_sword=15)))
        self.assertEqual(out.state, "Pickup")
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


class LifeCodeTest(unittest.TestCase):
    """A47: a life's ground code is learned only when one Take explains the rise."""

    def test_one_take_and_lives_up_files_the_code(self):
        kb = KnowledgeBase("sandbox")
        self.assertEqual(learn_life_code(kb, ["heart"], 10, 11), "heart")
        self.assertEqual(kb.items, {"heart": {"life_on_pickup": True}})

    def test_two_takes_in_one_response_learn_nothing(self):
        kb = KnowledgeBase("sandbox")
        self.assertIsNone(learn_life_code(kb, ["heart", "berry"], 10, 11))
        self.assertEqual(kb.items, {})

    def test_lives_unchanged_or_down_learn_nothing(self):
        kb = KnowledgeBase("sandbox")
        self.assertIsNone(learn_life_code(kb, ["berry"], 10, 10))
        self.assertIsNone(learn_life_code(kb, ["berry"], 10, 9))
        self.assertEqual(kb.items, {})

    def test_a_gem_or_unknown_code_is_never_a_life(self):
        kb = KnowledgeBase("sandbox")
        self.assertIsNone(learn_life_code(kb, ["gem"], 10, 11))
        self.assertIsNone(learn_life_code(kb, [None], 10, 11))
        self.assertEqual(kb.items, {})

    def test_a_learned_life_scores_above_gems_and_takes_no_slot(self):
        items = {"heart": {"life_on_pickup": True}}
        self.assertEqual(loot_score("heart", items), LIFE_SCORE)
        self.assertTrue(is_counter_supply("heart", items))
        self.assertFalse(is_counter_supply("berry", items))

    def test_a_learned_life_survives_a_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch("agentrealm_agent.knowledge_base.WORLDS_DIR", Path(tmp)):
            kb = KnowledgeBase("sandbox")
            learn_life_code(kb, ["heart"], 10, 11)
            knowledge_base.save(kb)
            loaded = knowledge_base.load("sandbox")
        self.assertTrue(is_counter_supply("heart", loaded.items))

    def test_stowed_supplies_are_never_dropped(self):
        w = WorldModel(character_id=1)
        w.held_supplies = [InventorySupply(1, "torch")]
        w.chest_supplies = [InventorySupply(2, "apple")]
        self.assertEqual([s.id for s in droppable_supplies(w)], [1])


class RunnerRejectionTest(unittest.TestCase):
    """The runner feeds each rejected intent to learn_loot_rejection."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.object(config, "STATE_DIR", Path(tmp.name))
        patch.start()
        self.addCleanup(patch.stop)
        cfg = CharacterConfig("T", "sandbox", scripted(goals=[]), Path("t.toml"))
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


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)

    def tick(self, cid, intents, snapshot_version=None):
        return self.responses.pop(0)


class RunnerLootLearningTest(unittest.TestCase):
    """A47 through the runner's full tick: results, events, then the observation."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.object(config, "STATE_DIR", Path(tmp.name))
        patch.start()
        self.addCleanup(patch.stop)
        self.kb = KnowledgeBase("sandbox")

    def run_tick(self, queue: list[dict], results: list[dict], *, lives_after: int, events=None) -> Runner:
        """Send ``queue`` as q1 and fold one response with ``results`` and the new lives."""
        response = {
            "tick": 11,
            "queue_id": "q1",
            "intent_results": results,
            "events_by_tick": events or [],
            "observation": {"version": 2, "delta": {"lives": lives_after}},
        }
        cfg = CharacterConfig("T", "sandbox", scripted(goals=[]), Path("t.toml"))
        r = Runner(cfg, FakeClient([response]), 1, threading.Event(), out=lambda _: None, knowledge=self.kb)
        self.addCleanup(r.trace.close)
        r.world = world(["....."], at=(1, 0))
        r.world.lives = 10
        r.world.entities = [
            Entity("supply", 7, (2, 0), "heart"),
            Entity("supply", 8, (0, 0), "berry"),
            Entity("supply", 9, (2, 0), "middle_chest", gem_price=50),
        ]
        r.mem = Memory(need_self=False, need_position=False)

        def send(_d):
            r.mem.pending_intents, r.mem.pending_queue, r.mem.pending_next_index = queue, None, 0
            return queue

        r.intents_for = send
        with mock.patch("agentrealm_agent.runner.decide", return_value=Decision(queue[0], "test")):
            r.tick()
        return r

    def test_one_applied_take_that_raises_lives_files_its_code(self):
        self.run_tick(
            [{"verb": "Take", "supply_id": 7}],
            [{"queue_id": "q1", "index": 0, "tick": 11, "outcome": "applied"}],
            lives_after=11,
        )
        self.assertEqual(self.kb.items.get("heart"), {"life_on_pickup": True})

    def test_two_applied_takes_with_lives_up_file_nothing(self):
        self.run_tick(
            [{"verb": "Take", "supply_id": 7}, {"verb": "Take", "supply_id": 8}],
            [
                {"queue_id": "q1", "index": 0, "tick": 11, "outcome": "applied"},
                {"queue_id": "q1", "index": 1, "tick": 12, "outcome": "applied"},
            ],
            lives_after=11,
        )
        self.assertNotIn("heart", self.kb.items)
        self.assertNotIn("berry", self.kb.items)

    def test_a_result_from_another_queue_is_not_ours(self):
        # A stale result from an older queue must not be read against this queue's Take.
        self.run_tick(
            [{"verb": "Take", "supply_id": 8}],
            [{"queue_id": "q0", "index": 0, "tick": 11, "outcome": "applied"}],
            lives_after=11,
        )
        self.assertEqual(self.kb.items, {})

    def test_take_with_lives_unchanged_files_nothing(self):
        self.run_tick(
            [{"verb": "Take", "supply_id": 8}],
            [{"queue_id": "q1", "index": 0, "tick": 11, "outcome": "applied"}],
            lives_after=10,
        )
        self.assertEqual(self.kb.items, {})

    def test_middle_chest_raises_capacity_to_the_manual_default_until_respawn(self):
        r = self.run_tick(
            [{"verb": "Take", "supply_id": 9}],
            [{"queue_id": "q1", "index": 0, "tick": 11, "outcome": "applied"}],
            lives_after=10,
        )
        self.assertEqual(r.world.carry_capacity, 30)
        r.world.apply_events([{"tick": 20, "events": [{"kind": "Respawned", "map_id": 1, "x": 2, "y": 3}]}])
        self.assertEqual(r.world.carry_capacity, DEFAULT_CARRY_CAPACITY)

    def test_rejected_middle_chest_take_leaves_capacity_alone(self):
        rejection = {"category": "state", "code": "insufficient_gems", "retryability": "permanent"}
        r = self.run_tick(
            [{"verb": "Take", "supply_id": 9}],
            [{"queue_id": "q1", "index": 0, "tick": 11, "outcome": "rejected", "rejection": rejection}],
            lives_after=10,
        )
        self.assertEqual(r.world.carry_capacity, DEFAULT_CARRY_CAPACITY)


class FetchItemTest(unittest.TestCase):
    """Loot is the executor for the plan's ``fetch_item`` op (A20)."""

    def test_guard_needs_a_fetch_op(self):
        from agentrealm_agent.states.loot import LootState

        w = world(["....", "...."], at=(0, 0))
        w.entities = [Entity("supply", 8, (3, 0), "apple")]
        self.assertFalse(LootState().guard(w, ctx()))
        self.assertTrue(LootState().guard(w, ctx(plan=fetch("apple"))))

    def test_walks_to_the_ops_code_in_sight(self):
        w = world(["....", "...."], at=(0, 0))
        w.entities = [Entity("supply", 8, (3, 0), "apple")]
        plan = fetch("apple")
        out = dispatch(w, ctx(plan=plan))
        self.assertEqual(out.state, "Loot")
        self.assertEqual(out.intents, [{"verb": "SetPosition", "x": 1, "y": 0}])
        self.assertEqual(plan.acted, plan.current())

    def test_walks_to_the_ops_code_past_other_loot(self):
        w = world([".....", ".....", "....."], at=(1, 1))
        w.entities = [Entity("supply", 2, (3, 0), "bronze_sword"), Entity("supply", 9, (4, 2), "gem")]
        out = dispatch(w, ctx(kb=priced(bronze_sword=500), plan=fetch("gem")))
        self.assertEqual(out.state, "Loot")
        self.assertIn("fetch gem", out.reason)
        self.assertEqual(out.intents[0]["verb"], "SetPosition")
        self.assertEqual(out.intents[0]["x"], 2)

    def test_takes_the_ops_code_in_reach(self):
        # A fetch target in reach: Pickup (a reflex) or Loot takes it, either way a Take.
        w = world(["...", "...", "..."], at=(1, 1))
        w.entities = [Entity("supply", 8, (2, 1), "apple")]
        out = dispatch(w, ctx(scripted(pickup=False), plan=fetch("apple")))
        self.assertEqual(out.state, "Loot")
        self.assertEqual(out.intents, [{"verb": "Take", "supply_id": 8}])

    def test_priced_supply_is_not_fetched(self):
        w = world(["....", "...."], at=(0, 0))
        w.entities = [Entity("supply", 8, (3, 0), "apple", gem_price=2)]
        out = dispatch(w, ctx(plan=fetch("apple")))
        self.assertNotEqual(out.state, "Loot")

    def test_walks_to_the_ops_cell_when_none_in_sight(self):
        w = world(["....", "...."], at=(0, 0))
        out = dispatch(w, ctx(plan=fetch("apple", x=3, y=0)))
        self.assertEqual(out.state, "Loot")
        self.assertEqual(out.intents, [{"verb": "SetPosition", "x": 1, "y": 0}])

    def test_no_code_in_sight_and_no_cell_yields(self):
        w = world(["....", "...."], at=(0, 0))
        out = dispatch(w, ctx(plan=fetch("apple")))
        self.assertNotEqual(out.state, "Loot")


if __name__ == "__main__":
    unittest.main()
