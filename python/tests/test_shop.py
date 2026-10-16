"""A21: Shop state — the executor for the plan's ``buy`` op on priced supplies."""

import random
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agentrealm_adk import config
from agentrealm_agent.brain import Decision
from agentrealm_adk.config import CharacterConfig, Policy
from agentrealm_adk.directives import PARAM_DEFAULTS
from agentrealm_adk.item_table import InventorySupply
from agentrealm_adk.knowledge_base import KnowledgeBase
from agentrealm_adk.memory import Memory
from agentrealm_agent.plan import PLAN_STALL_SECONDS, Plan, goal_done
from agentrealm_agent.runner import Runner
from agentrealm_agent.shop import (
    SHOP_MAX_REFUSALS,
    SHOP_PENDING_TICKS,
    note_shop_result,
    sync_shop,
)
from agentrealm_agent.states import dispatch
from agentrealm_agent.states.base import PlayContext, StateOutcome
from agentrealm_agent.brain import walkable_prefix
from agentrealm_agent.plan import parse_directives_goals, validate_goal_op
from agentrealm_adk.sellers import Seller, lapse_not_sold, refuse_unsold, sellers_of
from agentrealm_adk.travel import record_shop_cell
from agentrealm_adk.travel.knowledge import iter_not_sold, iter_shop_sales
from agentrealm_adk.world import Entity, WorldModel


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

    def test_buy_goal_done_once_one_more_is_held(self):
        # Free-play run 5: a buy is one purchase, whatever was already held.
        w = world()
        op = {"op": "buy", "code": "torch"}
        plan = Plan([op], dict(PARAM_DEFAULTS))
        plan.advance(w)
        self.assertFalse(goal_done(op, w, plan))
        w.held_supplies = [InventorySupply(1, "torch")]
        self.assertTrue(goal_done(op, w, plan))

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
        plan = Plan([{"op": "buy", "code": "torch", "drop": "stick"}], dict(PARAM_DEFAULTS))
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

    def test_buy_op_with_no_seller_known_is_dropped_with_why(self):
        # A buy needs a known seller (A110, A131): with none it is dropped at once, never walked.
        explore = {"op": "explore_area", "x": 2, "y": 2, "radius": 2}
        plan = Plan([{"op": "buy", "code": "torch"}, explore], dict(PARAM_DEFAULTS))
        w = world()
        c = ctx(w, plan=plan)
        record_shop_cell(c.knowledge, 1, (30, 0), "small_potion", 3)  # a shop, but not a torch seller
        with self.assertLogs("agentrealm_agent.plan", "WARNING") as logs:
            out = dispatch(w, c)
        self.assertNotEqual(out.state, "Shop")
        self.assertIn("no seller of torch known", "".join(logs.output))
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
        c = ctx(w, plan=plan, params=params)
        record_shop_cell(c.knowledge, 1, (30, 0), "torch", 2)
        out = dispatch(w, c)
        # No torch in sight: Shop walks toward its seller, buying nothing here.
        self.assertEqual(out.state, "Shop", out.reason)
        self.assertNotIn({"verb": "Take", "supply_id": 5}, out.intents or [])
        w.entities.append(Entity("supply", 6, (2, 1), "torch", gem_price=2))
        out = dispatch(w, c)
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
        record_shop_cell(c.knowledge, 1, (30, 0), "small_potion", 3)
        out = dispatch(w, c)
        self.assertEqual(out.state, "Shop", out.reason)
        self.assertEqual(out.intents, [{"verb": "SetPosition", "x": 1, "y": 0}])
        self.assertIn("travel:shop", out.reason)

    def test_keeps_the_shop_it_picked_when_another_comes_nearer(self):
        w = big_world()
        c = ctx(w, plan=self.plan())
        record_shop_cell(c.knowledge, 1, (30, 0), "small_potion", 3)
        dispatch(w, c)
        record_shop_cell(c.knowledge, 1, (0, 20), "small_potion", 3)  # nearer now, but the walk is committed (A71)
        out = dispatch(w, c)
        self.assertEqual(out.intents, [{"verb": "SetPosition", "x": 1, "y": 0}])

    def test_buys_once_the_item_comes_into_sight(self):
        w = big_world()
        c = ctx(w, plan=self.plan())
        record_shop_cell(c.knowledge, 1, (12, 0), "small_potion", 3)
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
        record_shop_cell(c.knowledge, 1, (3, 0), "bronze_sword", 15)
        with self.assertLogs("agentrealm_agent.plan", "WARNING"):
            dispatch(w, c)
        self.assertIsNone(plan.current())

    def test_dropped_when_every_known_seller_is_in_sight_without_it(self):
        w = big_world()
        plan = self.plan()
        c = ctx(w, plan=plan)
        record_shop_cell(c.knowledge, 1, (3, 0), "small_potion", 3)  # sold out: nothing priced there now
        with self.assertLogs("agentrealm_agent.plan", "WARNING") as logs:
            dispatch(w, c)
        self.assertIsNone(plan.current())
        self.assertIn("no small_potion for sale at any known seller", "".join(logs.output))

    def test_dropped_when_it_costs_more_than_the_gems_held(self):
        w = big_world()
        w.gems = 1
        w.entities = [Entity("supply", 5, (3, 0), "small_potion", gem_price=3)]
        plan = self.plan()
        c = ctx(w, plan=plan)
        record_shop_cell(c.knowledge, 1, (30, 0), "small_potion", 3)
        with self.assertLogs("agentrealm_agent.plan", "WARNING") as logs:
            dispatch(w, c)
        self.assertIsNone(plan.current())
        self.assertIn("cannot afford", "".join(logs.output))

    def town_buy(self, x: int, y: int) -> Plan:
        """A buy whose seller is known only by its town: the planner names the place (A131)."""
        return Plan([{"op": "buy", "code": "small_potion", "x": x, "y": y, "map_id": 1}], dict(PARAM_DEFAULTS))

    def test_a_seller_known_only_by_town_walks_to_the_town(self):
        w = big_world()
        c = ctx(w, plan=self.town_buy(30, 0))
        out = dispatch(w, c)
        self.assertEqual(out.state, "Shop", out.reason)
        self.assertEqual(out.intents, [{"verb": "SetPosition", "x": 1, "y": 0}])
        self.assertIn("plan seller", out.reason)

    def test_a_seller_known_only_by_town_is_searched_for_there(self):
        w = WorldModel(character_id=1, map_id=1, pos=(20, 20), perception=3, gems=10)
        for x in range(17, 24):
            for y in range(17, 24):
                w.view.tiles[(x, y)] = "dirt"  # town seen, the ground round it not yet
        w.terrain_center, w.terrain_map = (20, 20), 1
        out = dispatch(w, ctx(w, plan=self.town_buy(20, 20)))
        self.assertEqual(out.state, "Shop", out.reason)
        self.assertIn("searching round its plan seller", out.reason)

    def test_dropped_when_the_search_round_its_town_runs_out(self):
        w = big_world(at=(25, 25), size=60, perception=3)  # every cell within 20 of town seen: no frontier there
        plan = self.town_buy(25, 25)
        c = ctx(w, plan=plan)
        with self.assertLogs("agentrealm_agent.plan", "WARNING") as logs:
            dispatch(w, c)
        self.assertIsNone(plan.current())
        self.assertIn("no small_potion for sale at any known seller", "".join(logs.output))
        self.assertIn("(1 reached without it)", "".join(logs.output))

    def test_no_step_this_decision_keeps_the_town_search(self):
        w = WorldModel(character_id=1, map_id=1, pos=(20, 20), perception=3, gems=10)
        for x in range(17, 24):
            for y in range(17, 24):
                w.view.tiles[(x, y)] = "dirt"
        w.terrain_center, w.terrain_map = (20, 20), 1
        plan = self.town_buy(20, 20)
        c = ctx(w, plan=plan)
        with mock.patch("agentrealm_agent.states.shop.explore_outcome",
                        return_value=StateOutcome(None, "first cell taken", state="Shop")):
            dispatch(w, c)
        self.assertEqual(plan.current()["op"], "buy", "frontier left: not dropped")


class ShopWalksToASellerTest(unittest.TestCase):
    """A buy walks only toward a seller of its item, never to nearer shops
    that do not sell it (A131)."""

    def raft_world(self) -> tuple[WorldModel, PlayContext, Plan]:
        w = big_world(at=(20, 2), size=80)
        w.gems = 30
        plan = Plan([{"op": "buy", "code": "raft"}], dict(PARAM_DEFAULTS))
        c = ctx(w, plan=plan)
        for cell in ((10, 2), (14, 6), (8, 8)):  # nearer shops, west, that sell no rafts
            record_shop_cell(c.knowledge, 1, cell, "small_potion", 3)
        c.knowledge.clues.append({"kind": "npc", "text": "Raft, thirty gems", "map_id": 1, "x": 75, "y": 2, "tick": 1, "speaker_id": 9})
        return w, c, plan

    def test_a_raft_sold_only_by_a_far_npc_walks_toward_that_npc(self):
        w, c, _ = self.raft_world()
        stepped, out = walk(w, c, "Shop", limit=12)
        self.assertEqual(len(stepped), 12, out.reason)
        self.assertEqual([p[0] for p in stepped], list(range(21, 33)), "east, toward the fisher, past the nearer shops")
        self.assertIn("clue seller", out.reason)

    def test_the_npc_seller_is_searched_for_round_where_it_spoke(self):
        w, c, _ = self.raft_world()
        w.pos = w.terrain_center = (75, 2)  # the fisher's cell in sight; unseen ground past x 79
        out = dispatch(w, c)
        self.assertEqual(out.state, "Shop", out.reason)
        self.assertIn("searching round its clue seller", out.reason)

    def test_an_item_with_no_known_seller_is_dropped(self):
        w, c, plan = self.raft_world()
        c.knowledge.clues.clear()
        with self.assertLogs("agentrealm_agent.plan", "WARNING") as logs:
            out = dispatch(w, c)
        self.assertNotEqual(out.state, "Shop")
        self.assertIsNone(plan.current())
        self.assertIn("no seller of raft known", "".join(logs.output))

    def test_the_nearer_of_two_sellers_is_chosen(self):
        w = big_world()
        plan = Plan([{"op": "buy", "code": "torch"}], dict(PARAM_DEFAULTS))
        c = ctx(w, plan=plan)
        record_shop_cell(c.knowledge, 1, (30, 0), "torch", 2)
        record_shop_cell(c.knowledge, 1, (0, 20), "torch", 2)
        out = dispatch(w, c)
        self.assertEqual(out.intents, [{"verb": "SetPosition", "x": 0, "y": 1}], out.reason)

    def test_a_clue_must_name_the_item_and_a_gem_price(self):
        kb = KnowledgeBase.empty("sandbox")
        w = big_world()
        for text in ("The raft is by the lake", "Thirty gems for a torch", "Raft is free. Sword, ten gems"):
            kb.clues.append({"kind": "sign", "text": text, "map_id": 1, "x": 5, "y": 5, "tick": 1})
        self.assertEqual(sellers_of("raft", w, kb), [])
        kb.clues.append({"kind": "npc", "text": "Rafts, thirty gems each.", "map_id": 1, "x": 9, "y": 9, "tick": 1})
        self.assertEqual(sellers_of("raft", w, kb), [Seller(1, (9, 9), "clue", False)])


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
        # Skipped: nothing left to take, so Shop holds for the planner (A120).
        self.assertEqual((out.state, out.intents, out.wait), ("Shop", None, True), out.reason)

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


class StaleSellerTest(unittest.TestCase):
    """A shop reached without the item sells it no more, so the buy never
    walks back and forth between stale shops; with every seller reached it is
    dropped with why (A131)."""

    def test_two_stale_shops_are_each_reached_once_then_the_buy_is_dropped(self):
        w = big_world(size=60)
        plan = Plan([{"op": "buy", "code": "torch"}], dict(PARAM_DEFAULTS))
        c = ctx(w, plan=plan)
        record_shop_cell(c.knowledge, 1, (12, 0), "torch", 2)
        record_shop_cell(c.knowledge, 1, (0, 30), "torch", 2)
        with self.assertLogs("agentrealm_agent.plan", "WARNING") as logs:
            stepped, out = walk(w, c, "Shop", limit=80)
        self.assertLess(len(stepped), 80, "the walk ends")
        turned = next(i for i, p in enumerate(stepped) if p[1] > 0)
        self.assertTrue(all(p[1] > 0 for p in stepped[turned:]), "never back toward the first stale shop")
        self.assertIsNone(plan.current())
        self.assertIn("no torch for sale at any known seller (2 reached without it)", "".join(logs.output))
        self.assertEqual(iter_shop_sales(c.knowledge), [], "both stale sales are forgotten")

    def test_a_new_sighting_replaces_what_the_cell_sold(self):
        kb = KnowledgeBase.empty("sandbox")
        record_shop_cell(kb, 1, (3, 3), "torch", 2)
        record_shop_cell(kb, 1, (3, 3), "rope", 4)
        self.assertEqual(iter_shop_sales(kb), [(1, (3, 3), "rope", 4)])


class PreSellerShopCellTest(unittest.TestCase):
    """A shop cell saved before A131 has no record of what it sells: it may
    sell any item a price was seen for, taken after every known seller and
    checked off once in sight."""

    def legacy(self) -> KnowledgeBase:
        kb = KnowledgeBase.empty("sandbox")
        kb.maps["1"] = {"shops": {"5,0": {}}}  # as saved before A131
        kb.items["torch"] = {"gem_price": 2}
        return kb

    def test_it_is_a_seller_of_an_item_with_a_price_seen_only(self):
        kb, w = self.legacy(), big_world()
        self.assertEqual(sellers_of("torch", w, kb), [Seller(1, (5, 0), "unchecked", True)])
        self.assertEqual(sellers_of("raft", w, kb), [])

    def test_it_comes_after_every_known_seller(self):
        kb, w = self.legacy(), big_world()
        kb.clues.append({"kind": "sign", "text": "Torches, two gems", "map_id": 1, "x": 30, "y": 30, "tick": 1})
        self.assertEqual([s.by for s in sellers_of("torch", w, kb)], ["clue", "unchecked"])

    def test_seen_without_the_item_it_is_checked(self):
        w = big_world()
        plan = Plan([{"op": "buy", "code": "torch"}], dict(PARAM_DEFAULTS))
        c = ctx(w, plan=plan)
        c.knowledge.maps["1"] = {"shops": {"3,0": {}}}
        c.knowledge.items["torch"] = {"gem_price": 2}
        with self.assertLogs("agentrealm_agent.plan", "WARNING"):
            dispatch(w, c)
        self.assertEqual(c.knowledge.maps["1"]["shops"]["3,0"], {"sells": {}})
        self.assertIsNone(plan.current())


class SellerPlaceTest(unittest.TestCase):
    """The place a buy names keeps its map (A131)."""

    def test_x_y_and_map_id_go_together(self):
        for bad in (
            {"op": "buy", "code": "raft", "x": 1, "y": 2},
            {"op": "buy", "code": "raft", "map_id": 1},
            {"op": "buy", "code": "raft", "x": "1", "y": 2, "map_id": 1},
            {"op": "buy", "code": "raft", "x": 1, "map_id": 1},
        ):
            with self.assertLogs("agentrealm_agent.plan", "WARNING"):
                self.assertIsNone(validate_goal_op(bad), bad)
        good = {"op": "buy", "code": "raft", "x": 1, "y": 2, "map_id": 1}
        self.assertEqual(validate_goal_op(good), good)

    def test_a_plan_seller_stays_on_its_map_across_a_warp(self):
        w = big_world(at=(20, 20))
        w.map_id = w.terrain_map = 2  # warped: the same x, y on another map
        op = {"op": "buy", "code": "raft", "x": 20, "y": 20, "map_id": 1}
        plan = Plan([dict(op)], dict(PARAM_DEFAULTS))
        c = ctx(w, plan=plan)
        self.assertEqual(sellers_of("raft", w, c.knowledge, op), [Seller(1, (20, 20), "plan", False)])
        out = dispatch(w, c)
        self.assertNotIn("searching round", out.reason)
        self.assertIsNone(plan.sellers_searched, "not searched for on this map")


class SearchedPlaceTest(unittest.TestCase):
    """A seller's place searched round without the item is no seller of it,
    for any buy, until newer evidence: a clue found since (A131)."""

    def searched(self) -> tuple[WorldModel, PlayContext]:
        w = big_world(at=(25, 25), size=60, perception=3)  # every cell round the clue seen: the search runs out
        plan = Plan([{"op": "buy", "code": "raft"}], dict(PARAM_DEFAULTS))
        c = ctx(w, plan=plan)
        c.knowledge.clues.append({"kind": "npc", "text": "Raft, thirty gems", "map_id": 1, "x": 25, "y": 25, "tick": 5})
        w.tick = 100
        with self.assertLogs("agentrealm_agent.plan", "WARNING"):
            dispatch(w, c)
        self.assertIsNone(plan.current())
        return w, c

    def test_it_is_remembered_in_the_knowledge_base(self):
        w, c = self.searched()
        self.assertEqual(iter_not_sold(c.knowledge), {(1, (25, 25), "raft"): 100})
        self.assertEqual(sellers_of("raft", w, c.knowledge), [])

    def test_a_resent_buy_is_refused_not_walked_back(self):
        w, c = self.searched()
        kept, refused = refuse_unsold([{"op": "buy", "code": "raft"}], w, c.knowledge)
        self.assertEqual(kept, [])
        self.assertIn("no seller of raft is known", refused[0])
        named = {"op": "buy", "code": "raft", "x": 25, "y": 25, "map_id": 1}
        self.assertEqual(refuse_unsold([named], w, c.knowledge)[0], [], "the same place named by the op too")

    def test_a_named_place_searched_is_refused_until_a_newer_clue(self):
        w, c = self.searched()
        named = {"op": "buy", "code": "raft", "x": 25, "y": 25, "map_id": 1}
        self.assertEqual(refuse_unsold([named], w, c.knowledge)[0], [])
        # A clue offering rafts found since, at another cell: the named place is a seller again.
        c.knowledge.clues.append({"kind": "npc", "text": "A raft for thirty gems, in town", "map_id": 1, "x": 50, "y": 50, "tick": 150})
        self.assertEqual(refuse_unsold([named], w, c.knowledge)[0], [named])
        self.assertIn(Seller(1, (25, 25), "plan", False), sellers_of("raft", w, c.knowledge, named))

    def test_a_record_lapses_on_newer_evidence(self):
        w, c = self.searched()
        lapse_not_sold(c.knowledge)
        self.assertEqual(len(iter_not_sold(c.knowledge)), 1, "no newer clue: kept")
        c.knowledge.clues.append({"kind": "sign", "text": "Rafts, thirty gems", "map_id": 1, "x": 5, "y": 5, "tick": 150})
        lapse_not_sold(c.knowledge)
        self.assertEqual(iter_not_sold(c.knowledge), {})

    def test_a_shop_seen_selling_it_lapses_the_record(self):
        w, c = self.searched()
        record_shop_cell(c.knowledge, 1, (40, 40), "raft", 30)
        self.assertEqual(iter_not_sold(c.knowledge), {})

    def test_a_clue_found_since_brings_it_back(self):
        w, c = self.searched()
        c.knowledge.clues.append({"kind": "sign", "text": "Rafts restocked: thirty gems", "map_id": 1, "x": 25, "y": 25, "tick": 150})
        self.assertEqual(sellers_of("raft", w, c.knowledge), [Seller(1, (25, 25), "clue", False)])


class RunnerLearnsWhatAShopSellsTest(unittest.TestCase):
    """A shop cell keeps what it was seen selling, with its price (A131)."""

    def test_a_priced_supply_records_its_item_on_the_cell(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.object(config, "STATE_DIR", Path(tmp.name))
        patch.start()
        self.addCleanup(patch.stop)
        kb = KnowledgeBase.empty("sandbox")
        cfg = CharacterConfig("T", "sandbox", Policy(kind="scripted", goals=[]), Path("t.toml"))
        r = Runner(cfg, None, 1, threading.Event(), out=lambda _: None, knowledge=kb)
        self.addCleanup(r.trace.close)
        r.world.map_id = 3
        r._learn_shops_from_entities({"supplies": [
            {"id": 1, "x": 4, "y": 5, "supply_subtype_code": "raft", "gem_price": 30},
            {"id": 2, "x": 6, "y": 5, "supply_subtype_code": "torch"},  # lying free: no shop
        ]})
        self.assertEqual(iter_shop_sales(kb), [(3, (4, 5), "raft", 30)])


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
