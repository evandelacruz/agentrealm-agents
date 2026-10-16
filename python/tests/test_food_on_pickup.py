"""A155: food is eaten on pickup, never carried (Manual §11 Food and potions).

Food heals the moment it is taken, even at full health, and never takes room
in the chest; a potion is used up even at full health. So food is left lying
until the character is hurt by at least its heal, takes no pack room, a
``fetch_item`` of it is done once taken, the heals carried are potions only,
and no potion is sent at full health.
"""

import random
import unittest

from agentrealm_adk.config import Policy
from agentrealm_adk.directives import PARAM_DEFAULTS
from agentrealm_adk.gem_yield import GemYieldTracker
from agentrealm_adk.healing import POTION_RULE, carried_heal, food_near, held_heal, spend_potion, worth_eating
from agentrealm_adk.item_table import InventorySupply
from agentrealm_adk.knowledge_base import KnowledgeBase
from agentrealm_adk.loot import Pickup
from agentrealm_adk.memory import Memory
from agentrealm_adk.pack import TAKE, make_room
from agentrealm_adk.supplies import heal_of
from agentrealm_adk.world import Entity, WorldModel, ZoneFact
from agentrealm_agent.plan import Plan, goal_done
from agentrealm_agent.runner import Runner
from agentrealm_agent.states import PlayContext
from agentrealm_agent.states.detour import valuable
from agentrealm_agent.states.pickup import pickup_outcome
from agentrealm_agent.states.use_item import potion_refused
from agentrealm_agent.strategist import food_line, system_prompt
from ladder import readiness

POTION = InventorySupply(9, "small_potion")
KNIFE = InventorySupply(3, "pocket_knife")


def world(health: int = 10) -> WorldModel:
    """Map 7, open dirt, off safe ground, the knife armed."""
    w = WorldModel(character_id=1, map_id=7, pos=(5, 5), perception=6, health=health, max_health=10)
    for x in range(11):
        for y in range(11):
            w.view.tiles[(x, y)] = "dirt"
    w.terrain_center, w.terrain_map = (5, 5), 7
    w.zones[7] = {(5, 5): ZoneFact(safe=False)}
    w.held_supplies = [KNIFE]
    w.armed_code, w.armed_id = "pocket_knife", 3
    return w


def ctx(*ops: dict) -> PlayContext:
    return PlayContext(Memory(), Policy(kind="scripted"), random.Random(0), plan=Plan(list(ops), dict(PARAM_DEFAULTS)))


def apple(at=(6, 5), sid: int = 50) -> Entity:
    return Entity("supply", sid, at, "apple")


class WorthEatingTest(unittest.TestCase):
    def test_food_is_worth_taking_only_when_hurt_by_at_least_its_heal(self):
        self.assertEqual(heal_of("apple"), 2)
        self.assertFalse(worth_eating(world(10), "apple"), "full health: its heal is lost")
        self.assertFalse(worth_eating(world(9), "apple"), "hurt by 1: half of it lost")
        self.assertTrue(worth_eating(world(8), "apple"))
        self.assertTrue(worth_eating(world(9), "berry"), "a berry heals 1")
        self.assertFalse(worth_eating(world(5), "small_potion"), "a potion is no food")
        w = world()
        w.health = None
        self.assertFalse(worth_eating(w, "apple"), "health unknown")

    def test_food_near_leaves_food_lying_until_then(self):
        w = world(9)
        w.entities = [apple()]
        w.view.tiles[(5, 7)] = "bush"
        foods, blocks = food_near(w, Memory(), None)
        self.assertEqual(foods, [], "the apple waits")
        self.assertEqual(blocks, [(5, 7)], "a bush's berry heals 1")
        w.health = 10
        self.assertEqual(food_near(w, Memory(), None), ([], []), "at full health nothing is food near")
        self.assertTrue(food_line(w, None, Memory()).startswith("food_near=none"))
        w.health = 8
        self.assertEqual([e.id for e in food_near(w, Memory(), None)[0]], [50])


class PickupLeavesFoodTest(unittest.TestCase):
    def test_pickup_takes_food_only_once_hurt_by_its_heal(self):
        w = world(10)
        w.entities = [apple()]
        self.assertIsNone(pickup_outcome(w, ctx(), state="Pickup"))
        w.health = 9
        self.assertIsNone(pickup_outcome(w, ctx(), state="Pickup"))
        w.health = 8
        out = pickup_outcome(w, ctx(), state="Pickup")
        assert out is not None
        self.assertEqual(out.intents, [{"verb": "Take", "supply_id": 50}])

    def test_pickup_still_takes_what_is_not_food(self):
        w = world(10)
        w.entities = [Entity("supply", 51, (6, 5), "small_potion")]
        out = pickup_outcome(w, ctx(), state="Pickup")
        assert out is not None
        self.assertEqual(out.intents, [{"verb": "Take", "supply_id": 51}])

    def test_detour_goes_for_food_only_once_hurt_by_its_heal(self):
        w = world(9)
        self.assertFalse(valuable(w, apple((8, 8)), {}, ctx()))
        w.health = 8
        self.assertTrue(valuable(w, apple((8, 8)), {}, ctx()))


class FoodTakesNoRoomTest(unittest.TestCase):
    def test_a_full_pack_takes_food_with_no_drop(self):
        w = world(3)
        w.held_supplies = [KNIFE] + [InventorySupply(10 + i, "rope") for i in range(8)]
        self.assertEqual(w.slots_carried(), w.carry_capacity)
        self.assertEqual(make_room(w, Pickup(50, "apple", (6, 5), None, 200)).kind, TAKE)
        self.assertNotEqual(make_room(w, Pickup(51, "small_potion", (6, 5), None, 200)).kind, TAKE)


class FetchFoodDoneWhenTakenTest(unittest.TestCase):
    def test_a_fetch_of_food_is_done_once_its_take_goes_through(self):
        w = world(5)
        op = {"op": "fetch_item", "code": "apple"}
        plan = Plan([op], dict(PARAM_DEFAULTS))
        self.assertFalse(goal_done(op, w, plan))
        plan.note_taken("small_potion")
        self.assertFalse(goal_done(op, w, plan), "another item's Take is no fetch of this one")
        plan.note_taken("apple")
        self.assertTrue(goal_done(op, w, plan), "eaten on pickup, never held")

    def test_only_the_head_fetch_and_only_food(self):
        plan = Plan([{"op": "travel", "to": "town"}, {"op": "fetch_item", "code": "apple"}], dict(PARAM_DEFAULTS))
        plan.note_taken("apple")
        self.assertIsNone(plan.eaten, "a Take under another op is not the fetch's")
        potion = Plan([{"op": "fetch_item", "code": "small_potion"}], dict(PARAM_DEFAULTS))
        potion.note_taken("small_potion")
        self.assertIsNone(potion.eaten, "a potion is held: it is done by being held")

    def test_the_runner_tells_the_plan_of_each_take(self):
        r = Runner.__new__(Runner)
        r.world, r.mem, r.knowledge, r.acceptance = world(5), Memory(), None, None
        r.plan = Plan([{"op": "fetch_item", "code": "apple"}], dict(PARAM_DEFAULTS))
        r.gem_cuts, r._applied_uses, r._applied_take_codes, r._loadout_verbs = GemYieldTracker(), [], [], []
        r.world.entities = [apple()]
        r.mem.pending = {"verb": "Take", "supply_id": 50}
        r.on_result({"outcome": "applied", "tick": 101}, 0)
        self.assertTrue(r.plan.eaten)
        r.world.entities = []
        r.plan.advance(r.world, r.mem)
        self.assertIsNone(r.plan.current(), "done, though nothing is held")


class HeldHealsArePotionsTest(unittest.TestCase):
    def test_food_in_the_pack_is_no_heal_carried(self):
        w = world(3)
        w.held_supplies = [KNIFE, InventorySupply(5, "berry"), POTION]
        self.assertEqual(held_heal(w), heal_of("small_potion"))
        self.assertEqual(carried_heal(w, Memory(), Policy(kind="scripted"), dict(PARAM_DEFAULTS)), POTION)
        w.held_supplies = [KNIFE, InventorySupply(5, "berry")]
        self.assertIsNone(carried_heal(w, Memory(), Policy(kind="scripted"), dict(PARAM_DEFAULTS)))

    def test_lorraine_counts_the_same_heals_and_the_same_food(self):
        w = world(3)
        w.held_supplies = [KNIFE, InventorySupply(5, "berry"), POTION]
        self.assertEqual(readiness.Loadout.now(w).heal, held_heal(w))
        w.health = 9
        w.entities = [apple()]
        self.assertIsNone(readiness.food_in_sight(w), "the apple waits, as Gary leaves it")
        w.health = 8
        self.assertEqual(readiness.food_in_sight(w).id, 50)

    def test_lorraine_still_fetches_a_free_potion_at_any_health(self):
        # A potion is carried, not eaten on pickup: only food waits (A155).
        w = world(10)
        w.entities = [apple(), Entity("supply", 52, (7, 5), "small_potion")]
        self.assertEqual(readiness.food_in_sight(w).id, 52)
        w.entities.append(Entity("supply", 53, (5, 6), "small_potion", gem_price=3))
        self.assertEqual(readiness.food_in_sight(w).id, 52, "a priced one is the shop's")


class NoPotionAtFullHealthTest(unittest.TestCase):
    def test_spend_potion_never_drinks_at_full_health(self):
        w = world(10)
        w.held_supplies = [KNIFE, POTION]
        w.map_level = 1  # in a level: in combat
        m, policy, params = Memory(), Policy(kind="scripted"), dict(PARAM_DEFAULTS)
        self.assertFalse(spend_potion(w, m, policy, params, "small_potion", ordered=True))
        self.assertIsNone(carried_heal(w, m, policy, params))
        w.health = 9
        self.assertTrue(spend_potion(w, m, policy, params, "small_potion", ordered=True))

    def test_the_planners_potion_is_refused_at_full_health_with_why(self):
        w = world(10)
        w.held_supplies = [KNIFE, POTION]
        c = ctx({"op": "use_item", "code": "small_potion"}, {"op": "fight", "npc_type": "rat"})
        why = potion_refused(w, c, c.plan, "small_potion")
        self.assertIn("none is drunk at full health", why)


class RetiredHealLearningTest(unittest.TestCase):
    def test_saved_heal_keys_are_dropped_at_load(self):
        raw = {
            "world_code": "sandbox",
            "items": {
                "apple": {"heal_amount": 5, "heal_on_pickup": True},
                "small_potion": {"heal_amount": 10, "gem_price": 4},
            },
        }
        kb = KnowledgeBase.from_dict("sandbox", raw)
        self.assertEqual(kb.items, {"small_potion": {"gem_price": 4}})


class ContractTest(unittest.TestCase):
    def test_the_planner_is_told_to_leave_food_lying(self):
        prompt = system_prompt()
        self.assertIn("Food is eaten the moment it is taken, even at full health, and never carried", prompt)
        self.assertIn("leave it lying until the character is hurt by at least its heal", prompt)
        self.assertNotIn("Pick up food along the way", prompt)
        self.assertIn("or food, eaten on pickup once the character is hurt by at least its heal", prompt)
        self.assertNotIn("whenever hurt", prompt)
        self.assertIn("none is drunk at full health", POTION_RULE)


if __name__ == "__main__":
    unittest.main()
