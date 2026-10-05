"""A21: Shop state — priced supplies, plan buy, buy_signals, potion_reserve."""

import random
import unittest

from agentrealm_agent.config import Policy
from agentrealm_agent.directives import PARAM_DEFAULTS
from agentrealm_agent.healing import raise_buy_potion
from agentrealm_agent.item_table import InventorySupply
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.memory import Memory
from agentrealm_agent.plan import Plan, goal_done
from agentrealm_agent.pathing import replan as path_replan
from agentrealm_agent.states import dispatch
from agentrealm_agent.states.base import PlayContext
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

    def test_consumes_heal_buy_signal(self):
        w = world()
        w.health, w.max_health = 10, 10  # not hurt: Heal stays out
        w.entities = [Entity("supply", 5, (1, 2), "small_potion", gem_price=3)]
        m = Memory()
        raise_buy_potion(m, why="test")
        dispatch(w, ctx(w, m=m))
        self.assertEqual(m.buy_signals, [])

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
