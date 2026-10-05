"""A21: Shop state — priced supplies, plan buy, buy_signals, potion_reserve."""

import random
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agentrealm_agent import config
from agentrealm_agent.brain import Decision
from agentrealm_agent.config import CharacterConfig, Policy
from agentrealm_agent.directives import PARAM_DEFAULTS
from agentrealm_agent.healing import raise_buy_potion
from agentrealm_agent.item_table import InventorySupply
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.memory import Memory
from agentrealm_agent.plan import PLAN_STALL_SECONDS, Plan, goal_done
from agentrealm_agent.pathing import replan as path_replan
from agentrealm_agent.runner import Runner
from agentrealm_agent.shop import SHOP_MAX_REFUSALS, SHOP_PENDING_TICKS, note_shop_result, sync_shop
from agentrealm_agent.states import dispatch
from agentrealm_agent.states.base import PlayContext
from agentrealm_agent.states.shop import ShopState
from agentrealm_agent.world import Entity, WorldModel


def world(at=(1, 1)) -> WorldModel:
    w = WorldModel(character_id=1, map_id=1, pos=at, perception=5, gems=10)
    for x in range(5):
        for y in range(5):
            w.view.tiles[(x, y)] = "dirt"
    w.terrain_center, w.terrain_map = at, 1
    return w


def ctx(
    w: WorldModel,
    *,
    m: Memory | None = None,
    plan: Plan | None = None,
    params: dict | None = None,
) -> PlayContext:
    return PlayContext(
        m or Memory(),
        Policy(kind="scripted", goals=["explore"]),
        random.Random(0),
        params=params or dict(PARAM_DEFAULTS),
        knowledge=KnowledgeBase.empty("sandbox"),
        plan=plan,
    )


class ShopBuyTest(unittest.TestCase):
    def test_takes_priced_supply_in_reach(self):
        w = world()
        w.entities = [Entity("supply", 5, (1, 2), "small_potion", gem_price=3)]
        plan = Plan([{"op": "buy", "code": "small_potion"}], dict(PARAM_DEFAULTS))
        out = dispatch(w, ctx(w, plan=plan))
        self.assertEqual(out.state, "Shop")
        self.assertEqual(out.intents, [{"verb": "Take", "supply_id": 5}])

    def test_walks_to_priced_supply(self):
        w = world(at=(0, 0))
        w.entities = [Entity("supply", 5, (3, 0), "torch", gem_price=2)]
        plan = Plan([{"op": "buy", "code": "torch"}], dict(PARAM_DEFAULTS))
        out = dispatch(w, ctx(w, plan=plan))
        self.assertEqual(out.state, "Shop")
        self.assertEqual(out.intents[0]["verb"], "SetPosition")

    def test_skips_when_cannot_afford(self):
        w = world()
        w.gems = 1
        w.entities = [Entity("supply", 5, (1, 2), "torch", gem_price=5)]
        plan = Plan([{"op": "buy", "code": "torch"}], dict(PARAM_DEFAULTS))
        out = dispatch(w, ctx(w, plan=plan))
        self.assertNotEqual(out.state, "Shop")

    def test_buy_goal_done_when_held(self):
        w = world()
        w.held_supplies = [InventorySupply(1, "torch")]
        op = {"op": "buy", "code": "torch"}
        self.assertTrue(goal_done(op, w, Plan([op], dict(PARAM_DEFAULTS))))

    def test_plan_keeps_buy_op_with_shop(self):
        plan = Plan([{"op": "buy", "code": "torch"}], dict(PARAM_DEFAULTS))
        m = Memory()
        path_replan(world(), m, Policy(kind="scripted", goals=["explore"]), random.Random(0), set(), set(), plan=plan)
        self.assertEqual(plan.current(), {"op": "buy", "code": "torch"})

    def test_signal_only_non_potion_buy_sends_take(self):
        # No plan op and the potion reserve met: only the signal wants the torch.
        w = world()
        w.health, w.max_health = 10, 10
        w.held_supplies = [InventorySupply(1, "small_potion"), InventorySupply(2, "small_potion")]
        w.entities = [Entity("supply", 5, (1, 2), "torch", gem_price=3)]
        m = Memory()
        raise_buy_potion(m, why="test", code="torch")
        c = ctx(w, m=m)
        self.assertTrue(ShopState().guard(w, c))
        self.assertEqual(m.buy_signals[0]["code"], "torch")  # guard left memory alone
        out = dispatch(w, c)
        self.assertEqual(out.state, "Shop")
        self.assertEqual(out.intents, [{"verb": "Take", "supply_id": 5}])
        self.assertEqual(len(m.buy_signals), 1)  # not consumed until the Take lands

    def _sent_heal_take(self):
        w = world()
        w.health, w.max_health = 10, 10
        w.entities = [Entity("supply", 5, (1, 2), "small_potion", gem_price=3)]
        m = Memory()
        raise_buy_potion(m, why="test")
        out = dispatch(w, ctx(w, m=m))
        self.assertEqual(out.intents, [{"verb": "Take", "supply_id": 5}])
        return w, m, out.intents[0]

    def test_applied_take_consumes_heal_buy_signal(self):
        _, m, intent = self._sent_heal_take()
        note_shop_result(m, intent, applied=True)
        self.assertEqual(m.buy_signals, [])
        self.assertIsNone(m.shop_pending)

    def test_rejected_take_keeps_buy_signal(self):
        _, m, intent = self._sent_heal_take()
        note_shop_result(m, intent, applied=False)
        self.assertEqual(len(m.buy_signals), 1)
        self.assertIsNone(m.shop_pending)

    def test_gem_drop_consumes_buy_signal(self):
        w, m, _ = self._sent_heal_take()
        w.gems = 7
        sync_shop(w, m)
        self.assertEqual(m.buy_signals, [])

    def test_stowed_potions_do_not_block_heal_signal(self):
        # Reserve met by stowed potions; Heal drinks only carried ones.
        w = world()
        w.health, w.max_health = 10, 10
        w.chest_supplies = [InventorySupply(1, "small_potion"), InventorySupply(2, "small_potion")]
        w.entities = [Entity("supply", 5, (1, 2), "small_potion", gem_price=3)]
        m = Memory()
        raise_buy_potion(m, why="test")
        out = dispatch(w, ctx(w, m=m))
        self.assertEqual(out.state, "Shop")
        self.assertEqual(out.intents, [{"verb": "Take", "supply_id": 5}])

    def test_full_pack_drops_then_takes(self):
        w = world()
        w.carry_capacity = 1
        w.held_supplies = [InventorySupply(9, "stick")]
        w.entities = [Entity("supply", 5, (1, 2), "torch", gem_price=3)]
        plan = Plan([{"op": "buy", "code": "torch"}], dict(PARAM_DEFAULTS))
        m = Memory()
        out = dispatch(w, ctx(w, m=m, plan=plan))
        self.assertEqual(out.state, "Shop")
        self.assertEqual(out.intents, [{"verb": "Drop", "supply_id": 9}, {"verb": "Take", "supply_id": 5}])
        note_shop_result(m, out.intents[0], applied=False)  # Drop refused: Take never ran
        self.assertIsNone(m.shop_pending)

    def test_full_pack_keeps_held_supply_worth_more_than_purchase(self):
        w = world()
        w.carry_capacity = 1
        w.held_supplies = [InventorySupply(9, "bronze_mail")]
        w.entities = [Entity("supply", 5, (1, 2), "torch", gem_price=3)]
        plan = Plan([{"op": "buy", "code": "torch"}], dict(PARAM_DEFAULTS))
        m = Memory()
        c = ctx(w, m=m, plan=plan)
        c.knowledge.items["bronze_mail"] = {"gem_price": 50}
        out = dispatch(w, c)
        self.assertNotEqual(out.state, "Shop")
        self.assertNotIn({"verb": "Drop", "supply_id": 9}, out.intents or [])
        self.assertIsNone(m.shop_pending)

    def test_pending_take_expires_on_leaving_shop_cell(self):
        w, m, _ = self._sent_heal_take()
        w.pos = (4, 4)
        sync_shop(w, m)
        self.assertIsNone(m.shop_pending)
        w.pos, w.gems = (1, 1), 7  # an unrelated gem drop later
        sync_shop(w, m)
        self.assertEqual(len(m.buy_signals), 1)

    def test_pending_take_expires_after_timeout(self):
        w, m, _ = self._sent_heal_take()
        w.tick += SHOP_PENDING_TICKS + 1
        w.gems = 7
        sync_shop(w, m)
        self.assertIsNone(m.shop_pending)
        self.assertEqual(len(m.buy_signals), 1)

    def test_buy_op_with_nothing_in_sight_is_dropped_after_stall(self):
        plan = Plan([{"op": "buy", "code": "torch"}, {"op": "explore_area", "x": 2, "y": 2, "radius": 2}],
                    dict(PARAM_DEFAULTS))
        m, pol = Memory(), Policy(kind="scripted", goals=["explore"])
        w = world()
        path_replan(w, m, pol, random.Random(0), set(), set(), plan=plan)
        self.assertEqual(plan.current()["op"], "buy")
        w.tick += PLAN_STALL_SECONDS * plan.tick_hz
        with self.assertLogs("agentrealm_agent.plan", "WARNING"):
            path_replan(w, m, pol, random.Random(0), set(), set(), plan=plan)
        self.assertNotEqual((plan.current() or {}).get("op"), "buy")

    def test_hostile_in_range_outranks_shop(self):
        w = world()
        w.entities = [Entity("supply", 5, (1, 2), "torch", gem_price=3), Entity("npc", 3, (2, 2), "slime")]
        plan = Plan([{"op": "buy", "code": "torch"}], dict(PARAM_DEFAULTS))
        out = dispatch(w, ctx(w, plan=plan))
        self.assertNotEqual(out.state, "Shop")

    def test_restock_potion_reserve(self):
        w = world()
        w.health, w.max_health = 10, 10
        w.entities = [Entity("supply", 5, (1, 2), "small_potion", gem_price=3)]
        params = dict(PARAM_DEFAULTS)
        params["potion_reserve"] = 2
        out = dispatch(w, ctx(w, params=params))
        self.assertEqual(out.state, "Shop")
        self.assertEqual(out.intents, [{"verb": "Take", "supply_id": 5}])

    def test_no_shop_when_reserve_met(self):
        w = world()
        w.health, w.max_health = 10, 10
        w.held_supplies = [InventorySupply(1, "small_potion"), InventorySupply(2, "small_potion")]
        w.entities = [Entity("supply", 5, (1, 2), "small_potion", gem_price=3)]
        out = dispatch(w, ctx(w))
        self.assertNotEqual(out.state, "Shop")


class ShopRefusalCapTest(unittest.TestCase):
    """A supply whose Take is rejected SHOP_MAX_REFUSALS times is skipped until
    the loadout, gems or map change (A21)."""

    def refuse_until_skipped(self, w, m):
        for _ in range(SHOP_MAX_REFUSALS):
            out = dispatch(w, ctx(w, m=m))
            self.assertEqual(out.intents, [{"verb": "Take", "supply_id": 5}])
            note_shop_result(m, out.intents[0], applied=False)
        out = dispatch(w, ctx(w, m=m))
        self.assertNotEqual(out.state, "Shop")
        self.assertEqual(len(m.buy_signals), 1)

    def setUp(self):
        self.w = world()
        self.w.entities = [Entity("supply", 5, (1, 2), "small_potion", gem_price=3)]
        self.m = Memory()
        raise_buy_potion(self.m, why="test")

    def test_skips_supply_after_max_refusals(self):
        self.refuse_until_skipped(self.w, self.m)

    def test_other_supply_still_bought(self):
        self.refuse_until_skipped(self.w, self.m)
        self.w.entities.append(Entity("supply", 6, (2, 2), "small_potion", gem_price=3))
        out = dispatch(self.w, ctx(self.w, m=self.m))
        self.assertEqual(out.intents, [{"verb": "Take", "supply_id": 6}])

    def test_gem_change_clears_refusals(self):
        self.refuse_until_skipped(self.w, self.m)
        self.w.gems += 5
        out = dispatch(self.w, ctx(self.w, m=self.m))
        self.assertEqual(out.intents, [{"verb": "Take", "supply_id": 5}])

    def test_loadout_change_clears_refusals(self):
        self.refuse_until_skipped(self.w, self.m)
        self.w.armed_code = "wooden_sword"
        out = dispatch(self.w, ctx(self.w, m=self.m))
        self.assertEqual(out.intents, [{"verb": "Take", "supply_id": 5}])

    def test_map_change_clears_refusals(self):
        self.refuse_until_skipped(self.w, self.m)
        sync_shop(self.w.__class__(character_id=1, map_id=2, pos=(1, 1), gems=self.w.gems), self.m)
        self.assertEqual(self.m.shop_refusals, {})


class RunnerShopResultTest(unittest.TestCase):
    """The runner feeds each Take result to note_shop_result (A21)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.object(config, "STATE_DIR", Path(tmp.name))
        patch.start()
        self.addCleanup(patch.stop)
        cfg = CharacterConfig("T", "default", "test", "sandbox", Policy(kind="scripted", goals=["hold"]), Path("t.toml"))
        self.r = Runner(cfg, None, 1, threading.Event(), out=lambda _: None)
        self.addCleanup(self.r.trace.close)
        self.r.world = world()
        self.r.world.health, self.r.world.max_health = 10, 10
        self.r.world.entities = [Entity("supply", 5, (1, 2), "small_potion", gem_price=3)]
        self.r.mem = Memory(need_self=False, need_position=False)
        raise_buy_potion(self.r.mem, why="test")
        out = dispatch(self.r.world, ctx(self.r.world, m=self.r.mem))
        self.assertEqual(out.intents, [{"verb": "Take", "supply_id": 5}])
        self.r.intents_for(Decision(out.intents[0], "test"))

    def test_applied_take_consumes_buy_signal(self):
        self.assertFalse(self.r.on_result({"tick": 30, "outcome": "applied"}, 0))
        self.assertEqual(self.r.mem.buy_signals, [])
        self.assertIsNone(self.r.mem.shop_pending)

    def test_rejected_take_keeps_buy_signal(self):
        rejection = {"category": "state", "code": "insufficient_gems", "retryability": "permanent"}
        self.assertTrue(self.r.on_result({"tick": 30, "outcome": "rejected", "rejection": rejection}, 0))
        self.assertEqual(len(self.r.mem.buy_signals), 1)
        self.assertIsNone(self.r.mem.shop_pending)
