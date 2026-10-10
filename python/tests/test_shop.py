"""A21: Shop state — the executor for the plan's ``buy`` op on priced supplies."""

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
from agentrealm_agent.item_table import InventorySupply
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.memory import Memory
from agentrealm_agent.plan import PLAN_STALL_SECONDS, Plan, goal_done
from agentrealm_agent.runner import Runner
from agentrealm_agent.shop import (
    SHOP_MAX_REFUSALS,
    SHOP_PENDING_TICKS,
    note_shop_result,
    sync_shop,
)
from agentrealm_agent.states import dispatch
from agentrealm_agent.states.base import PlayContext
from agentrealm_agent.brain import walkable_prefix
from agentrealm_agent.plan import parse_directives_goals
from agentrealm_agent.travel import record_shop_cell
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
        with self.assertLogs("agentrealm_agent.plan", "WARNING"):
            out = dispatch(w, ctx(w, plan=plan))
        self.assertNotEqual(out.state, "Shop")
        self.assertIsNone(plan.current(), "it costs more than the gems held: dropped")

    def test_buy_goal_done_when_held(self):
        w = world()
        w.held_supplies = [InventorySupply(1, "torch")]
        op = {"op": "buy", "code": "torch"}
        self.assertTrue(goal_done(op, w, Plan([op], dict(PARAM_DEFAULTS))))

    def _sent_take(self):
        w = world()
        w.entities = [Entity("supply", 5, (1, 2), "small_potion", gem_price=3)]
        m = Memory()
        plan = Plan([{"op": "buy", "code": "small_potion"}], dict(PARAM_DEFAULTS))
        out = dispatch(w, ctx(w, m=m, plan=plan))
        self.assertEqual(out.intents, [{"verb": "Take", "supply_id": 5}])
        self.assertIsNotNone(m.shop_pending)
        return w, m, out.intents[0]

    def test_applied_take_settles_pending(self):
        _, m, intent = self._sent_take()
        note_shop_result(m, intent, applied=True)
        self.assertIsNone(m.shop_pending)
        self.assertEqual(m.shop_refusals, {})

    def test_rejected_take_counts_refusal(self):
        _, m, intent = self._sent_take()
        note_shop_result(m, intent, applied=False)
        self.assertIsNone(m.shop_pending)
        self.assertEqual(m.shop_refusals, {5: 1})

    def test_gem_drop_settles_pending(self):
        w, m, _ = self._sent_take()
        w.gems = 7
        sync_shop(w, m)
        self.assertIsNone(m.shop_pending)

    def test_full_pack_drops_then_takes(self):
        w = world()
        w.carry_capacity = 1
        w.held_supplies = [InventorySupply(9, "stick")]
        w.entities = [Entity("supply", 5, (1, 2), "torch", gem_price=3)]
        plan = Plan([{"op": "buy", "code": "torch"}], dict(PARAM_DEFAULTS))
        m = Memory(equip_not_wearable={"stick"})  # Equip already tried the stick on (A55)
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
        w, m, _ = self._sent_take()
        w.pos = (4, 4)
        sync_shop(w, m)
        self.assertIsNone(m.shop_pending)

    def test_pending_take_expires_after_timeout(self):
        w, m, _ = self._sent_take()
        w.tick += SHOP_PENDING_TICKS + 1
        sync_shop(w, m)
        self.assertIsNone(m.shop_pending)

    def test_buy_op_with_no_shop_or_town_known_is_dropped_after_stall(self):
        explore = {"op": "explore_area", "x": 2, "y": 2, "radius": 2}
        plan = Plan([{"op": "buy", "code": "torch"}, explore], dict(PARAM_DEFAULTS))
        m = Memory()
        w = world()
        out = dispatch(w, ctx(w, m=m, plan=plan))
        self.assertNotEqual(out.state, "Shop")  # nothing in sight: Shop sends nothing
        self.assertEqual(plan.current()["op"], "buy")
        w.tick += PLAN_STALL_SECONDS * plan.tick_hz
        with self.assertLogs("agentrealm_agent.plan", "WARNING"):
            dispatch(w, ctx(w, m=m, plan=plan))
        self.assertEqual(plan.current(), explore)

    def test_hostile_in_range_outranks_shop(self):
        w = world()
        w.entities = [Entity("supply", 5, (1, 2), "torch", gem_price=3), Entity("npc", 3, (2, 2), "slime")]
        w.hostile_types.add(("npc", "slime"))  # a type seen attacking (survival.is_hostile)
        plan = Plan([{"op": "buy", "code": "torch"}], dict(PARAM_DEFAULTS))
        out = dispatch(w, ctx(w, plan=plan))
        self.assertNotEqual(out.state, "Shop")

    def test_no_buy_op_no_shop(self):
        # Shop buys only the plan's `buy` op: no potion-reserve restock (A21).
        w = world()
        w.health, w.max_health = 10, 10
        w.entities = [Entity("supply", 5, (1, 2), "small_potion", gem_price=3)]
        params = dict(PARAM_DEFAULTS, potion_reserve=2)
        out = dispatch(w, ctx(w, params=params))
        self.assertNotEqual(out.state, "Shop")
        self.assertNotIn({"verb": "Take", "supply_id": 5}, out.intents or [])

    def test_buys_only_the_ops_code(self):
        # A36: with `buy torch` on top, a potion in sight is not bought.
        w = world()
        w.health, w.max_health = 10, 10
        w.entities = [Entity("supply", 5, (1, 2), "small_potion", gem_price=3)]
        plan = Plan([{"op": "buy", "code": "torch"}], dict(PARAM_DEFAULTS))
        params = dict(PARAM_DEFAULTS, potion_reserve=2)
        out = dispatch(w, ctx(w, plan=plan, params=params))
        self.assertNotEqual(out.state, "Shop")
        self.assertIsNone(plan.acted)
        w.entities.append(Entity("supply", 6, (2, 1), "torch", gem_price=2))
        out = dispatch(w, ctx(w, plan=plan, params=params))
        self.assertEqual((out.state, out.intents), ("Shop", [{"verb": "Take", "supply_id": 6}]))
        self.assertEqual(plan.acted, {"op": "buy", "code": "torch"})


def walk(w: WorldModel, c: PlayContext, state: str, limit: int = 20) -> tuple[list, object]:
    """Dispatch and apply each SetPosition ``state`` sends until it sends
    something else: the cells stepped onto, and that last outcome."""
    stepped = []
    for _ in range(limit):
        out = dispatch(w, c)
        if out.state != state or not out.intents or out.intents[0]["verb"] != "SetPosition":
            return stepped, out
        w.pos = (out.intents[0]["x"], out.intents[0]["y"])
        stepped.append(w.pos)
        if c.memory.path and c.memory.path[0] == w.pos:
            c.memory.path = c.memory.path[1:]  # as the runner does once a Step is queued
        w.tick += 10
    return stepped, out


def big_world(at=(0, 0), size=40, perception=5) -> WorldModel:
    w = WorldModel(character_id=1, map_id=1, pos=at, perception=perception, gems=10)
    for x in range(size):
        for y in range(size):
            w.view.tiles[(x, y)] = "dirt"
    w.terrain_center, w.terrain_map = at, 1
    return w


class ShopOutOfSightTest(unittest.TestCase):
    """Free-play run 4: a buy with no shop in sight sent nothing, and the safe
    default walked about 100 cells away from town into a pack. The shop is a
    prerequisite of the buy (A71): walk there, or toward town to find one."""

    def plan(self) -> Plan:
        return Plan([{"op": "buy", "code": "small_potion"}], dict(PARAM_DEFAULTS))

    def test_walks_to_a_known_shop_out_of_sight(self):
        w = big_world()
        c = ctx(w, plan=self.plan())
        record_shop_cell(c.knowledge, 1, (30, 0))
        out = dispatch(w, c)
        self.assertEqual(out.state, "Shop", out.reason)
        self.assertEqual(out.intents, [{"verb": "SetPosition", "x": 1, "y": 0}])
        self.assertIn("travel:shop", out.reason)

    def test_keeps_the_shop_it_picked_when_another_comes_nearer(self):
        w = big_world()
        c = ctx(w, plan=self.plan())
        record_shop_cell(c.knowledge, 1, (30, 0))
        dispatch(w, c)
        record_shop_cell(c.knowledge, 1, (0, 20))  # nearer now, but the walk is committed (A71)
        out = dispatch(w, c)
        self.assertEqual(out.intents, [{"verb": "SetPosition", "x": 1, "y": 0}])

    def test_buys_once_the_item_comes_into_sight(self):
        w = big_world()
        c = ctx(w, plan=self.plan())
        record_shop_cell(c.knowledge, 1, (12, 0))
        stepped, out = walk(w, c, "Shop", limit=6)
        self.assertTrue(stepped)
        w.entities = [Entity("supply", 5, (12, 0), "small_potion", gem_price=3)]
        stepped, out = walk(w, c, "Shop", limit=20)
        self.assertEqual(out.intents, [{"verb": "Take", "supply_id": 5}], out.reason)

    def test_dropped_when_no_known_shop_sells_it(self):
        w = big_world()
        w.entities = [Entity("supply", 7, (3, 0), "bronze_sword", gem_price=15)]
        plan = self.plan()
        c = ctx(w, plan=plan)
        record_shop_cell(c.knowledge, 1, (3, 0))
        with self.assertLogs("agentrealm_agent.plan", "WARNING"):
            dispatch(w, c)
        self.assertIsNone(plan.current())

    def test_dropped_when_it_costs_more_than_the_gems_held(self):
        w = big_world()
        w.gems = 1
        w.entities = [Entity("supply", 5, (3, 0), "small_potion", gem_price=3)]
        plan = self.plan()
        c = ctx(w, plan=plan)
        record_shop_cell(c.knowledge, 1, (30, 0))
        with self.assertLogs("agentrealm_agent.plan", "WARNING") as logs:
            dispatch(w, c)
        self.assertIsNone(plan.current())
        self.assertIn("cannot afford", "".join(logs.output))

    def test_no_shop_known_walks_to_town(self):
        w = big_world()
        c = ctx(w, plan=self.plan())
        c.knowledge.extra["town"] = {"map_id": 1, "x": 30, "y": 0}
        out = dispatch(w, c)
        self.assertEqual(out.state, "Shop", out.reason)
        self.assertEqual(out.intents, [{"verb": "SetPosition", "x": 1, "y": 0}])
        self.assertIn("to town", out.reason)

    def test_no_shop_known_in_town_explores_around_it(self):
        w = WorldModel(character_id=1, map_id=1, pos=(20, 20), perception=3, gems=10)
        for x in range(17, 24):
            for y in range(17, 24):
                w.view.tiles[(x, y)] = "dirt"  # town seen, the ground round it not yet
        w.terrain_center, w.terrain_map = (20, 20), 1
        c = ctx(w, plan=self.plan())
        c.knowledge.extra["town"] = {"map_id": 1, "x": 20, "y": 20}
        out = dispatch(w, c)
        self.assertEqual(out.state, "Shop", out.reason)
        self.assertIn("exploring town", out.reason)


class ShopTilesAreNotWalkedTest(unittest.TestCase):
    """Free-play run 1: ``travel:shop`` stepped onto the shop's sword and
    bought it (21 → 6 gems) when the plan wanted a potion."""

    def test_travel_to_shop_ends_next_to_it(self):
        w = world(at=(0, 2))
        w.gems = 21
        w.entities = [Entity("supply", 7, (4, 2), "bronze_sword", gem_price=15)]
        c = ctx(w, plan=Plan(parse_directives_goals(["travel:shop"]), dict(PARAM_DEFAULTS)))
        record_shop_cell(c.knowledge, 1, (4, 2))
        stepped, out = walk(w, c, "Travel")
        self.assertNotIn((4, 2), stepped)
        self.assertEqual(max(abs(w.pos[0] - 4), abs(w.pos[1] - 2)), 1, (w.pos, out.reason))
        self.assertIsNone(c.plan.current(), "arrived beside the shop: the op is done")

    def test_walks_route_around_items_for_sale(self):
        w = world(at=(0, 2))
        w.entities = [Entity("supply", 7, (2, 2), "bronze_sword", gem_price=15)]
        c = ctx(w, plan=Plan(parse_directives_goals(["travel:point:4:2"]), dict(PARAM_DEFAULTS)))
        stepped, _ = walk(w, c, "Travel")
        self.assertNotIn((2, 2), stepped)
        self.assertEqual(w.pos, (4, 2))

    def test_travel_to_a_point_on_an_item_for_sale_ends_beside_it(self):
        w = world(at=(0, 2))
        w.entities = [Entity("supply", 7, (4, 2), "bronze_sword", gem_price=15)]
        c = ctx(w, plan=Plan(parse_directives_goals(["travel:point:4:2"]), dict(PARAM_DEFAULTS)))
        stepped, _ = walk(w, c, "Travel")
        self.assertNotIn((4, 2), stepped)
        self.assertIsNone(c.plan.current(), "arrived beside it: the op is done")

    def test_walk_queue_stops_before_an_item_for_sale(self):
        w = world(at=(0, 2))
        w.entities = [Entity("supply", 7, (2, 2), "bronze_sword", gem_price=15)]
        m = Memory()
        prefix = walkable_prefix(w, m, Policy(kind="scripted"), [(1, 2), (2, 2), (3, 2)])
        self.assertEqual(prefix, [(1, 2)])

    def test_buy_takes_only_the_named_item(self):
        w = world(at=(0, 2))
        w.gems = 21
        w.entities = [
            Entity("supply", 7, (2, 2), "bronze_sword", gem_price=15),
            Entity("supply", 8, (4, 2), "small_potion", gem_price=3),
        ]
        c = ctx(w, plan=Plan([{"op": "buy", "code": "small_potion"}], dict(PARAM_DEFAULTS)))
        stepped, out = walk(w, c, "Shop")
        self.assertNotIn((2, 2), stepped)
        self.assertNotIn((4, 2), stepped)
        self.assertEqual(out.state, "Shop")
        self.assertEqual(out.intents, [{"verb": "Take", "supply_id": 8}])


class ShopRefusalCapTest(unittest.TestCase):
    """A supply whose Take is rejected SHOP_MAX_REFUSALS times is skipped until
    the loadout, gems or map change (A21)."""

    def refuse_until_skipped(self, w, m):
        for _ in range(SHOP_MAX_REFUSALS):
            out = dispatch(w, ctx(w, m=m, plan=self.plan))
            self.assertEqual(out.intents, [{"verb": "Take", "supply_id": 5}])
            note_shop_result(m, out.intents[0], applied=False)
        out = dispatch(w, ctx(w, m=m, plan=self.plan))
        self.assertNotEqual(out.state, "Shop")

    def setUp(self):
        self.w = world()
        self.w.entities = [Entity("supply", 5, (1, 2), "small_potion", gem_price=3)]
        self.m = Memory()
        self.plan = Plan([{"op": "buy", "code": "small_potion"}], dict(PARAM_DEFAULTS))

    def test_skips_supply_after_max_refusals(self):
        self.refuse_until_skipped(self.w, self.m)

    def test_other_supply_still_bought(self):
        self.refuse_until_skipped(self.w, self.m)
        self.w.entities.append(Entity("supply", 6, (2, 2), "small_potion", gem_price=3))
        out = dispatch(self.w, ctx(self.w, m=self.m, plan=self.plan))
        self.assertEqual(out.intents, [{"verb": "Take", "supply_id": 6}])

    def test_gem_change_clears_refusals(self):
        self.refuse_until_skipped(self.w, self.m)
        self.w.gems += 5
        out = dispatch(self.w, ctx(self.w, m=self.m, plan=self.plan))
        self.assertEqual(out.intents, [{"verb": "Take", "supply_id": 5}])

    def test_loadout_change_clears_refusals(self):
        self.refuse_until_skipped(self.w, self.m)
        self.w.armed_code = "wooden_sword"
        out = dispatch(self.w, ctx(self.w, m=self.m, plan=self.plan))
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
        cfg = CharacterConfig("T", "sandbox", Policy(kind="scripted", goals=[]), Path("t.toml"))
        self.r = Runner(cfg, None, 1, threading.Event(), out=lambda _: None)
        self.addCleanup(self.r.trace.close)
        self.r.world = world()
        self.r.world.health, self.r.world.max_health = 10, 10
        self.r.world.entities = [Entity("supply", 5, (1, 2), "small_potion", gem_price=3)]
        self.r.mem = Memory(need_self=False, need_position=False)
        plan = Plan([{"op": "buy", "code": "small_potion"}], dict(PARAM_DEFAULTS))
        out = dispatch(self.r.world, ctx(self.r.world, m=self.r.mem, plan=plan))
        self.assertEqual(out.intents, [{"verb": "Take", "supply_id": 5}])
        self.r.intents_for(Decision(out.intents[0], "test"))

    def test_applied_take_settles_pending(self):
        self.assertFalse(self.r.on_result({"tick": 30, "outcome": "applied"}, 0))
        self.assertIsNone(self.r.mem.shop_pending)
        self.assertEqual(self.r.mem.shop_refusals, {})

    def test_rejected_take_counts_refusal(self):
        rejection = {"category": "state", "code": "insufficient_gems", "retryability": "permanent"}
        self.assertTrue(self.r.on_result({"tick": 30, "outcome": "rejected", "rejection": rejection}, 0))
        self.assertIsNone(self.r.mem.shop_pending)
        self.assertEqual(self.r.mem.shop_refusals, {5: 1})
