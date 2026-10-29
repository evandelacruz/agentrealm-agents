"""Modal Mona (docs/v4/SPEC.md) on made-up worlds: the decider, the kit, get health, the reflexes, the modes, the loop.

Each test builds a small invented map and checks one rule; the scene tests
run the whole decision over a few ticks. No test reaches the network or
``python/.state``.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

from agentrealm_adk.item_table import InventorySupply
from agentrealm_adk.memory import Memory as NavMemory
from agentrealm_adk.world import Entity, MapView, WorldModel

from adk3.core.ledger import Ledger
from adk3.realm import actions, value
from adk3.realm.context import Ctx
from adk3.realm.memory import Death, Memory
from adk3.realm.state import State
from mona import modes, reflexes, route
from mona.agent import Mona
from mona.decider import Mode, decide, next_level
from mona.health import GetHealth
from mona.kit import Kit, KitStore

MAP = 1
value.use_world("olympuff")
OPEN = ["g" * 21] * 21


def world(rows=OPEN, at=(10, 10), health=10, gems=0, armed="bronze_sword", safe=(), cleared=()) -> WorldModel:
    """A map from rows of letters: g grass, d dirt, b bush, t tree, w wall, l lava."""
    names = {"g": "grass", "d": "dirt", "b": "bush", "t": "tree", "w": "wall", "l": "lava"}
    w = WorldModel(42)
    w.map_id, w.map_level, w.pos, w.tick = MAP, 0, at, 1000
    w.health, w.max_health, w.gems, w.lives = health, 10, gems, 3
    w.armed_code, w.armed_id = armed, 1 if armed else None
    w.attack_range, w.perception, w.level_count = 1, 12, 8
    w.levels_cleared = set(cleared)
    w.inventory_read = True
    view = MapView()
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            view.tiles[(x, y)] = names[ch]
    view.safe = set(safe)
    w.maps[MAP] = view
    return w


def ctx_of(w: WorldModel, memory: Memory | None = None) -> Ctx:
    return Ctx(State.of(w, 10, None, 6, None, 4), memory or Memory(), None, NavMemory(), (MAP, (10, 10)))


def snotling(w: WorldModel, at, nid=100) -> Entity:
    e = Entity("npc", nid, at, "snotling")
    w.entities.append(e)
    w.hostile_types.add(("npc", "snotling"))
    return e


class GateTest(unittest.TestCase):
    def test_once_she_sets_off_a_potion_spent_on_the_way_does_not_turn_her_back(self):
        kit = Kit(level=1, items=["bronze_mail"], potions=1, trusted=True)
        s = ctx_of(world()).state  # no mail, no potion
        self.assertEqual(decide(s, kit), Mode.MONEY)
        kit.set_off = True
        self.assertEqual(decide(s, kit), Mode.BEAT)

    def test_a_revision_that_changes_nothing_keeps_her_on_her_way(self):
        a = Kit(level=1, items=["bronze_sword", "bronze_mail"], potions=2, trusted=True)
        b = Kit(level=1, items=["bronze_mail", "bronze_sword"], potions=2, trusted=True, why="again")
        self.assertTrue(b.same_as(a))
        self.assertFalse(Kit(level=1, items=["bronze_sword"], potions=2).same_as(a))


class DeciderTest(unittest.TestCase):
    def test_no_kit_or_an_untrusted_one_is_learn(self):
        s = ctx_of(world()).state
        self.assertEqual(decide(s, None), Mode.LEARN)
        self.assertEqual(decide(s, Kit(level=1, items=["bronze_sword"])), Mode.LEARN)

    def test_short_of_gems_is_money_then_gear_then_beat(self):
        kit = Kit(level=1, items=["bronze_sword", "bronze_mail"], trusted=True)
        self.assertEqual(decide(ctx_of(world(gems=5)).state, kit), Mode.MONEY)  # mail costs 20
        self.assertEqual(decide(ctx_of(world(gems=25)).state, kit), Mode.GEAR)
        w = world(gems=0)
        w.worn_codes = {"body": "bronze_mail"}
        self.assertEqual(decide(ctx_of(w).state, kit), Mode.BEAT)

    def test_a_kit_for_a_cleared_level_is_learnt_again_for_the_next(self):
        s = ctx_of(world(cleared={1})).state
        self.assertEqual(next_level(s), 2)
        self.assertEqual(decide(s, Kit(level=1, trusted=True)), Mode.LEARN)


class KitTest(unittest.TestCase):
    def test_missing_pieces_come_weapon_first_then_armor_then_tools_then_potions(self):
        kit = Kit(level=1, items=["bomb", "iron_mail", "iron_sword"], potions=2)
        self.assertEqual(kit.missing(ctx_of(world(armed=None)).state),
                         ["iron_sword", "iron_mail", "bomb", "small_potion", "small_potion"])

    def test_cost_is_what_is_missing_at_list_price(self):
        kit = Kit(level=1, items=["bronze_sword", "bronze_mail"], potions=1)
        self.assertEqual(kit.cost(ctx_of(world()).state), 30)  # mail 20 + potion 10; the sword is armed

    def test_it_keeps_across_runs(self):
        store = KitStore(Path(tempfile.mkdtemp()) / "kit.json")
        store.save(Kit(level=2, items=["iron_sword"], trusted=True, learned=["town"]))
        self.assertEqual(store.load().learned, ["town"])


class GetHealthTest(unittest.TestCase):
    def test_in_at_three_out_only_at_ten(self):
        g = GetHealth()
        for health, active in ((4, False), (3, True), (6, True), (9, True), (10, False)):
            g.update(ctx_of(world(health=health)).state)
            self.assertEqual(g.active, active, health)

    def test_a_potion_only_against_death_at_hand_else_berries(self):
        w = world(health=3)
        w.held_supplies = [InventorySupply(9, "small_potion")]
        w.entities.append(Entity("supply", 902, (14, 10), "berry"))
        self.assertEqual(reflexes.first(ctx_of(w))[0], "take")  # nothing near: the berry
        snotling(w, (11, 11))
        self.assertEqual(reflexes.first(ctx_of(w))[0], "drink")  # a hostile on her: the potion

    def test_above_three_a_potion_is_kept_and_she_goes_for_food(self):
        w = world(health=9, at=(1, 1))
        w.held_supplies = [InventorySupply(9, "small_potion")]
        g = GetHealth()
        g.active = True
        self.assertIsInstance(g.act(ctx_of(w)), actions.Walk)

    def test_she_goes_to_a_bush_she_has_seen_when_it_is_nearer_than_town(self):
        rows = ["d" * 60] * 21
        rows[10] = "d" * 45 + "b" + "d" * 14
        w = world(rows, health=5, at=(58, 10))  # town is at 10,10
        g = GetHealth()
        g.active = True
        action = g.act(ctx_of(w))
        self.assertEqual((type(action), action.why), (actions.Walk, "to a bush for its berry"))

    def test_with_no_potion_she_goes_to_town_for_food(self):
        w = world(health=3, at=(1, 1))
        g = GetHealth()
        g.update(ctx_of(w).state)
        self.assertIsInstance(g.act(ctx_of(w)), actions.Walk)


class ReflexTest(unittest.TestCase):
    def test_on_lava_she_steps_off_before_anything(self):
        rows = ["g" * 21] * 10 + ["g" * 10 + "l" + "g" * 10] + ["g" * 21] * 10
        w = world(rows)
        snotling(w, (11, 10))
        self.assertEqual(reflexes.first(ctx_of(w))[0], "off the fire")

    def test_hurt_she_fights_the_hostile_on_her_before_she_eats(self):
        w = world(health=6)
        snotling(w, (14, 10))
        w.entities.append(Entity("supply", 7, (8, 10), "berry"))
        self.assertEqual(reflexes.first(ctx_of(w))[0], "danger")

    def test_hurt_with_no_hostile_near_she_eats(self):
        w = world(health=6)
        w.entities.append(Entity("supply", 7, (8, 10), "berry"))
        self.assertEqual(reflexes.first(ctx_of(w))[0], "take")

    def test_at_full_health_food_is_left_and_the_hostile_fought(self):
        w = world()
        snotling(w, (14, 10))
        w.entities.append(Entity("supply", 7, (8, 10), "berry"))
        self.assertEqual(reflexes.first(ctx_of(w))[0], "danger")

    def test_hurt_with_no_food_a_bush_is_cut_for_its_berry(self):
        rows = ["g" * 21] * 10 + ["g" * 11 + "b" + "g" * 9] + ["g" * 21] * 10
        name, action = reflexes.first(ctx_of(world(rows, health=7)))
        self.assertEqual((name, type(action)), ("take", actions.UseOn))

    def test_a_hostile_beyond_ten_blocks_is_not_gone_for(self):
        w = world(["g" * 41] * 21, at=(5, 10))
        snotling(w, (20, 10))
        self.assertIsNone(reflexes.danger(ctx_of(w)))

    def test_gems_and_free_gear_within_25_are_taken(self):
        w = world(["g" * 41] * 21, at=(5, 10))
        w.entities.append(Entity("supply", 7, (28, 10), "gem"))
        self.assertEqual(reflexes.first(ctx_of(w))[0], "take")

    def test_a_sign_not_read_is_read(self):
        w = world()
        w.view.readable[(12, 10)] = True
        self.assertIsInstance(reflexes.first(ctx_of(w))[1], actions.Read)


class TakeTest(unittest.TestCase):
    def test_hurt_she_takes_the_nearer_gem_before_the_berry(self):
        w = world(health=5)
        w.entities.append(Entity("supply", 901, (11, 10), "gem"))
        w.entities.append(Entity("supply", 902, (14, 10), "berry"))
        name, action = reflexes.first(ctx_of(w))
        self.assertEqual((name, action.code), ("take", "gem"))


    def test_a_nearer_berry_comes_before_a_gem(self):
        w = world(health=5)
        w.entities.append(Entity("supply", 901, (14, 10), "gem"))
        w.entities.append(Entity("supply", 902, (11, 10), "berry"))
        self.assertEqual(reflexes.first(ctx_of(w))[1].code, "berry")


    def test_a_berry_on_a_bush_that_would_not_cut_is_still_eaten(self):
        w = world(health=5)
        w.entities.append(Entity("supply", 902, (13, 10), "berry"))
        mem = Memory()
        mem.changed_nothing(MAP, (13, 10), "cut")
        self.assertEqual(reflexes.first(ctx_of(w, mem))[0], "take")

    def test_near_means_the_walk_not_the_line_a_berry_behind_trees_is_far(self):
        rows = []
        for y in range(21):
            rows.append("".join("t" if 2 <= max(abs(x - 16), abs(y - 10)) <= 4 else "g" for x in range(21)))
        w = world(rows, health=5)
        w.entities.append(Entity("supply", 901, (16, 10), "berry"))  # 6 off as the crow flies, behind three trees
        w.entities.append(Entity("supply", 902, (10, 17), "gem"))  # 7 off, in the open
        self.assertEqual(reflexes.first(ctx_of(w))[1].why, "to the gem")


class DangerTest(unittest.TestCase):
    def strong(self, w, at, nid=100):
        e = snotling(w, at, nid)
        w.threat.record(("npc", "snotling"), 9)  # it hits for 9: a fight she would lose
        return e

    def test_a_fight_she_would_win_is_fought(self):
        w = world()
        snotling(w, (11, 10))
        self.assertIsInstance(reflexes.danger(ctx_of(w)), actions.Swing)

    def test_a_fight_she_would_lose_is_escaped_behind_a_screen_the_chaser_cannot_pass(self):
        rows = ["g" * 21] * 21
        rows = [r[:12] + "w" + r[13:] if 6 <= y <= 14 else r for y, r in enumerate(rows)]  # a wall at x 12
        w = world(rows, at=(11, 10))
        self.strong(w, (10, 10))
        action = reflexes.danger(ctx_of(w))
        self.assertEqual(action.why, "escape")
        self.assertGreater(action.path[-1][0], 12)  # round the wall, to the far side of it

    def test_cornered_she_fights(self):
        rows = ["w" * 5, "wgggw", "wgggw", "wgggw", "w" * 5]
        w = world(rows, at=(1, 2))
        self.strong(w, (2, 2))
        self.assertIsInstance(reflexes.danger(ctx_of(w)), actions.Swing)

    def test_a_chaser_stuck_behind_a_wall_is_no_danger(self):
        rows = ["g" * 21] * 21
        rows = [r[:10] + "w" + r[11:] for r in rows]  # a wall the whole height at x 10
        w = world(rows, at=(12, 10))
        self.strong(w, (8, 10))
        self.assertIsNone(reflexes.danger(ctx_of(w)))


class ArmedTest(unittest.TestCase):
    def test_walking_she_rearms_her_weapon_first(self):
        w = world(armed="small_potion")
        w.held_supplies = [InventorySupply(3, "bronze_sword")]
        action = reflexes.ready(ctx_of(w), actions.Walk(((11, 10),), "east"))
        self.assertEqual((action.verb, action.code), ("Arm", "bronze_sword"))

    def test_a_tool_armed_for_a_break_is_kept_until_the_blow_lands(self):
        w = world(armed="bronze_mallet")
        w.held_supplies = [InventorySupply(3, "bronze_sword")]
        ctx = ctx_of(w)
        for action in (actions.UseOn(11, 10, None, "break the rock"), actions.Stop("waiting to swing")):
            self.assertIs(reflexes.ready(ctx, action), action)


class RouterTest(unittest.TestCase):
    def test_she_arms_her_best_weapon_and_wears_her_armor_before_swinging(self):
        w = world(armed=None)
        w.held_supplies = [InventorySupply(3, "bronze_sword"), InventorySupply(5, "bronze_mail")]
        foe = snotling(w, (11, 10))
        self.assertEqual(route.strike(ctx_of(w), foe).verb, "Arm")
        w.armed_code, w.armed_id, w.held_supplies = "bronze_sword", 3, [InventorySupply(5, "bronze_mail")]
        self.assertEqual(route.strike(ctx_of(w), foe).verb, "Wear")

    def test_a_route_being_walked_is_kept_over_an_equal_one_found_later(self):
        rows = ["g" * 21] * 9 + ["g" * 12 + "w" + "g" * 8] * 3 + ["g" * 21] * 9
        w = world(rows, at=(10, 10))
        nav = NavMemory()
        at = lambda: Ctx(State.of(w, 10, None, 6, None, 4), Memory(), None, nav, (MAP, (10, 10)))
        first = route.to(at(), MAP, (16, 10), "east")
        w.pos = first.path[0]
        self.assertEqual(list(route.to(at(), MAP, (16, 10), "east").path), list(first.path[1:]))
        south = [(11, 11), (12, 12), (13, 12), (14, 11), (15, 10), (16, 10)]
        w.pos = (10, 10)
        nav.route_held = ((MAP, ("to", MAP, (16, 10))), [(10, 10), *south])
        self.assertEqual(list(route.to(at(), MAP, (16, 10), "east").path), south)  # kept, though north is as short

    def test_a_hedge_the_sword_cannot_chop_is_burnt_with_matches_when_there_is_no_way_round(self):
        rows = ["w" * 21] + ["w" + "g" * 10 + "t" + "g" * 8 + "w"] * 19 + ["w" * 21]  # a hedge from wall to wall
        w = world(rows, at=(10, 10))
        w.held_supplies = [InventorySupply(7, "matches")]
        mem = Memory()
        for y in range(21):
            mem.changed_nothing(MAP, (11, y), "chop")  # the sword left it standing
        action = route.to(ctx_of(w, mem), MAP, (16, 10), "east")
        self.assertEqual((type(action), action.arm_id), (actions.UseOn, 7))

    def test_a_kept_route_is_dropped_when_a_shelf_comes_into_view_on_it(self):
        w = world(at=(10, 10))
        nav = NavMemory()
        at = lambda: Ctx(State.of(w, 10, None, 6, None, 4), Memory(), None, nav, (MAP, (10, 10)))
        nav.route_held = ((MAP, ("to", MAP, (16, 10))), [(10, 10), (11, 10), (12, 10), (13, 10), (14, 10), (15, 10), (16, 10)])
        w.entities.append(Entity("supply", 77, (12, 10), "torch", gem_price=10))
        self.assertNotIn((12, 10), route.to(at(), MAP, (16, 10), "east").path)

    def test_a_fence_is_routed_around_not_given_up_on(self):
        rows = ["g" * 21] * 9 + ["g" * 12 + "w" + "g" * 8] * 3 + ["g" * 21] * 9
        action = route.to(ctx_of(world(rows, at=(10, 10))), MAP, (16, 10), "east")
        self.assertIsInstance(action, actions.Walk)
        self.assertTrue(all(p[0] != 12 or not 9 <= p[1] <= 11 for p in action.path))


class MoneyTest(unittest.TestCase):
    def test_she_asks_which_field_then_cuts_grass_there(self):
        flow = modes.Money(Kit(level=1, items=["bronze_mail"], trusted=True))
        ctx = ctx_of(world())
        self.assertIsInstance(flow.act(ctx), actions.Stop)
        self.assertIn("inner band", flow.question.options[0])
        flow.picked(0, ctx)
        flow.field = ("here", (10, 10))
        self.assertIn(type(flow.act(ctx)), (actions.UseOn, actions.Walk))

    def test_the_outer_band_opens_only_with_its_kit_a_sword_armor_and_a_potion(self):
        knife = modes.Money(Kit(level=1)).fields(ctx_of(world(armed="pocket_knife")))
        w = world(armed="bronze_sword")
        w.held_supplies = [InventorySupply(5, "bronze_mail"), InventorySupply(6, "small_potion")]
        kitted = modes.Money(Kit(level=1)).fields(ctx_of(w))
        self.assertFalse(any("outer" in name for name, _ in knife))  # the inner band needs nothing: money from nothing
        self.assertTrue(any("outer" in name for name, _ in kitted))

    def test_on_another_map_a_field_is_not_crossed_off_as_out_of_reach(self):
        flow = modes.Money(Kit(level=1))
        flow.field = ("north", (10, 10))
        w = world()
        w.map_id = 17
        w.maps = {17: w.maps[MAP]}
        flow.act(ctx_of(w))  # town is on map 1; no warp known from 17, so no route: crossed, but not by the map mix-up
        ctx = ctx_of(world())
        flow2 = modes.Money(Kit(level=1))
        flow2.field = ("north", (10, 10))
        self.assertIsNotNone(flow2.act(ctx))

    def test_a_cut_out_field_is_crossed_off(self):
        flow = modes.Money(Kit(level=1))
        flow.field = ("bare", (10, 10))
        ctx = ctx_of(world(["d" * 21] * 21))
        flow.act(ctx)
        self.assertIn("bare", flow.crossed)
        self.assertIsNone(flow.field)


    def test_a_poor_field_is_crossed_off_after_forty_cuts_and_a_good_one_is_kept(self):
        for gems, crossed in ((3, True), (8, False)):
            mem = Memory()
            flow = modes.Money(Kit(level=1))
            flow.field = ("field", (10, 10))
            ctx = ctx_of(world(), mem)
            flow.act(ctx)  # the first cut notes where the field's count starts
            for i in range(40):
                mem.broke(MAP, (10, 10), 1 if i < gems else 0)
            flow.act(ctx)
            self.assertEqual("field" in flow.crossed, crossed, gems)


class BeatTest(unittest.TestCase):
    def test_the_door_she_came_in_by_is_not_a_door_to_try(self):
        w = world(at=(10, 10))
        w.map_level = 1
        w.maps[MAP].tiles[(10, 11)] = "framed_door"
        flow = modes.Beat(Kit(level=1, trusted=True))
        action = flow.act(ctx_of(w))
        self.assertNotEqual(getattr(action, "why", ""), "through a door not taken yet")


class BeatStuckTest(unittest.TestCase):
    def test_inside_a_level_with_no_way_on_she_tells_the_strategist_once(self):
        rows = ["w" * 5] + ["wgggw"] * 3 + ["w" * 5]
        w = world(rows, at=(2, 2))
        w.map_level = 1
        flow = modes.Beat(Kit(level=1, trusted=True))
        flow.act(ctx_of(w))
        self.assertIn("no way on", flow.blocked)
        flow.blocked = ""
        flow.act(ctx_of(w))
        self.assertEqual(flow.blocked, "")


class GearTest(unittest.TestCase):
    def test_the_death_chest_holding_a_piece_comes_before_a_shop(self):
        mem = Memory()
        mem.died(Death(1.0, MAP, 4, 4, "snotling", 77, MAP, 14, 10, ["bronze_mail"]))
        mem.saw_for_sale("bronze_mail", 20, MAP, (2, 2))
        action = modes.Gear(Kit(level=1, items=["bronze_mail"])).act(ctx_of(world(gems=30), mem))
        self.assertIsInstance(action, actions.Walk)
        self.assertEqual(action.path[-1][0], 13)

    def test_a_sold_out_shelf_is_waited_at(self):
        mem = Memory()
        mem.saw_for_sale("bronze_mail", 20, MAP, (11, 10))
        action = modes.Gear(Kit(level=1, items=["bronze_mail"])).act(ctx_of(world(gems=30), mem))
        self.assertIsInstance(action, actions.Stop)
        self.assertIn("restock", action.why)

    def test_with_no_seller_known_she_searches_each_way_then_is_blocked(self):
        flow = modes.Gear(Kit(level=1, items=["bronze_mail"]))
        flow.crossed = {"search north", "search east", "search south", "search west"}
        flow.act(ctx_of(world(gems=30)))
        self.assertIn("no seller of bronze_mail", flow.blocked)


class LearnTest(unittest.TestCase):
    def test_the_steps_run_in_order_and_each_is_reported(self):
        flow = modes.Learn(None, [])
        self.assertEqual(flow.step(), "town")
        flow.started = time.time() - modes.TOWN_LOOK_SECONDS
        flow.act(ctx_of(world()))
        self.assertEqual((flow.done, flow.learned), (["town"], "walked town"))
        self.assertIsInstance(flow.act(ctx_of(world())), actions.Stop)  # waits on the strategist

    def test_walking_town_never_leaves_it(self):
        w = world(["g" * 81] * 81, at=(40, 40))
        for p in [p for p in w.view.tiles if max(abs(p[0] - 40), abs(p[1] - 40)) > 10]:
            del w.view.tiles[p]  # only the middle of town seen yet
        ctx = Ctx(State.of(w, 10, None, 6, None, 4), Memory(), None, NavMemory(), (MAP, (40, 40)))
        action = modes.Learn(None, []).act(ctx)
        self.assertTrue(all(abs(x - 40) <= modes.TOWN_RADIUS + 1 and abs(y - 40) <= modes.TOWN_RADIUS + 1
                            for x, y in action.path))

    def test_scouting_goes_to_the_kit_entrance(self):
        mem = Memory()
        mem.entrance(MAP, (18, 10))
        flow = modes.Learn(Kit(level=1, entrance="1:18,10"), ["town"])
        self.assertIsInstance(flow.act(ctx_of(world(), mem)), actions.Walk)


class WorldFactsTest(unittest.TestCase):
    def test_a_new_character_inherits_the_world_not_the_deaths(self):
        mem = Memory()
        mem.entrance(MAP, (9, 9))
        mem.died(Death(1.0, MAP, 4, 4, "snotling", 77, MAP, 4, 5, ["bronze_sword"]))
        fresh = mem.world_facts(None)
        self.assertIn("1:9,9", fresh.entrances)
        self.assertEqual(fresh.deaths, [])


class KitAnswerTest(unittest.TestCase):
    def test_potions_listed_as_items_are_counted_once(self):
        from mona.strategist import Ask, kit_of
        ask = Ask("kit", ctx_of(world()), None, 1, "x")
        kit = kit_of({"items": ["bronze_sword", "small_potion", "small_potion"], "potions": 2, "trusted": True}, ask)
        self.assertEqual((kit.items, kit.potions), (["bronze_sword"], 2))


class FakeStrategist:
    """Answers at once: a trusted kit of the bronze set, and the first of any pick."""

    def __call__(self, ask):
        if ask.kind == "kit":
            return {"level": ask.level, "items": ["bronze_sword", "bronze_mail"], "potions": 0, "trusted": True, "why": "test"}
        return {"pick": 0, "why": "test"}


class FakeBody:
    def __init__(self, w: WorldModel, memory: Memory):
        self.world, self.memory, self.kb, self.nav, self.town = w, memory, None, NavMemory(), (MAP, (10, 10))
        self.acted: list = []

    def sense(self):
        return State.of(self.world, 10, None, 6, None, 4)

    def act(self, action):
        self.acted.append(action)

    def take_surprises(self):
        return []

    def save(self):
        pass


class SceneTest(unittest.TestCase):
    def mona(self, w: WorldModel, mem: Memory | None = None, kit: Kit | None = None) -> tuple[Mona, FakeBody]:
        tmp = Path(tempfile.mkdtemp())
        store = KitStore(tmp / "kit.json")
        if kit is not None:
            store.save(kit)
        body = FakeBody(w, mem or Memory())
        m = Mona(body, FakeStrategist(), store, Ledger.load(tmp / "ledger.json"), tmp / "trace.jsonl", out=lambda s: None)
        return m, body

    def tick(self, m: Mona, n: int = 1) -> None:
        for _ in range(n):
            ctx = m.sense()
            m.decide(ctx, time.time())
            time.sleep(0.01)

    def test_before_her_first_read_she_still_ticks_so_the_read_comes(self):
        w = world()
        w.inventory_read = False
        m, body = self.mona(w)
        self.tick(m)
        self.assertEqual(body.acted, [None])

    def test_parking_fights_a_hostile_on_her_rather_than_walking_off_in_its_hits(self):
        w = world(safe=[(0, 0)])
        snotling(w, (11, 10))
        m, body = self.mona(w)
        with mock.patch("mona.agent.PARK_SECONDS", 0.2):
            m.park()
        self.assertTrue(any(isinstance(a, actions.Swing) for a in body.acted))

    def test_in_get_health_a_hostile_on_her_is_still_answered_first(self):
        w = world(health=3, gems=5)
        snotling(w, (11, 10))
        m, body = self.mona(w, kit=Kit(level=1, items=["bronze_sword", "bronze_mail"], trusted=True))
        self.tick(m)
        self.assertTrue(m.health.active)
        self.assertNotIn("food", str(body.acted[-1]))  # the danger reflex acts: fight or escape, never walk off to eat

    def test_a_trusted_kit_short_of_gems_puts_her_in_money_and_she_stays_there(self):
        m, _ = self.mona(world(gems=5), kit=Kit(level=1, items=["bronze_sword", "bronze_mail"], trusted=True))
        self.tick(m, 5)
        self.assertEqual(m.mode, Mode.MONEY)
        self.assertIsNotNone(m.flow.field)  # the pick landed and stands

    def test_a_snotling_on_her_while_she_works_is_fought_not_walked_from(self):
        w = world(gems=5)
        snotling(w, (12, 10))
        m, body = self.mona(w, kit=Kit(level=1, items=["bronze_sword", "bronze_mail"], trusted=True))
        self.tick(m, 3)
        self.assertTrue(all(not isinstance(a, actions.Walk) or "close on" in a.why for a in body.acted[-2:]))

    def test_low_health_far_from_food_puts_her_in_get_health_and_only_ten_lets_her_out(self):
        w = world(health=3, gems=5)
        m, _ = self.mona(w, kit=Kit(level=1, items=["bronze_sword", "bronze_mail"], trusted=True))
        self.tick(m)
        self.assertTrue(m.health.active)
        w.health = 9
        self.tick(m)
        self.assertTrue(m.health.active)
        w.health = 10
        self.tick(m)
        self.assertFalse(m.health.active)
        self.assertEqual(m.mode, Mode.MONEY)  # the goal she had resumes

    def test_no_kit_starts_in_learn_and_the_answer_moves_her_on(self):
        m, _ = self.mona(world(gems=0))
        self.tick(m)
        self.assertEqual(m.mode, Mode.LEARN)
        self.tick(m, 5)
        self.assertEqual(m.mode, Mode.MONEY)  # trusted kit, 20 gems short of the mail


if __name__ == "__main__":
    unittest.main()
