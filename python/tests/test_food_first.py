"""A135: out of a fight lost health comes back from food; a potion is for a fight."""

import random
import unittest

from agentrealm_agent.config import Policy
from agentrealm_agent.directives import PARAM_DEFAULTS, default_directives
from agentrealm_agent.healing import carried_heal, food_near, in_combat, spend_potion
from agentrealm_agent.item_table import InventorySupply
from agentrealm_agent.memory import Memory
from agentrealm_agent.plan import Plan
from agentrealm_agent.states import PlayContext, dispatch
from agentrealm_agent.states.use_item import potion_refused
from agentrealm_agent.strategist import POTION_RULE, food_line
from agentrealm_agent.world import Entity, WorldModel, ZoneFact

POTION = InventorySupply(9, "potion")
KNIFE = InventorySupply(3, "pocket_knife")


def world(health: int = 7) -> WorldModel:
    """Map 7, open dirt, off safe ground, a potion held and the knife armed."""
    w = WorldModel(character_id=1, map_id=7, pos=(5, 5), perception=6, health=health, max_health=10)
    for x in range(11):
        for y in range(11):
            w.view.tiles[(x, y)] = "dirt"
    w.terrain_center, w.terrain_map = (5, 5), 7
    w.zones[7] = {(5, 5): ZoneFact(safe=False)}
    w.held_supplies = [POTION, KNIFE]
    w.armed_code, w.armed_id = "pocket_knife", 3
    return w


def ctx(*ops: dict) -> PlayContext:
    return PlayContext(
        Memory(),
        Policy(kind="scripted", goals=[]),
        random.Random(0),
        directives=default_directives(),
        plan=Plan(list(ops), dict(PARAM_DEFAULTS)),
    )


def drank(out) -> bool:
    return any(i.get("verb") == "Use" and i.get("supply_id") is None for i in out.intents or []) and "potion" in out.reason


def berry(at=(7, 5)) -> Entity:
    return Entity("supply", 50, at, "berry")


def failures(c: PlayContext) -> list[dict]:
    return [s for s in c.memory.strategist_signals if s.get("trigger") == "goal_failed"]


class OutOfCombatFoodFirstTest(unittest.TestCase):
    def test_berries_near_at_7_of_10_are_eaten_and_no_potion_drunk(self):
        w = world(7)
        w.entities = [berry()]
        out = dispatch(w, ctx())
        self.assertEqual((out.state, out.reason), ("Heal", "heal_food → (7, 5)"))
        self.assertFalse(drank(out))

    def test_a_bush_near_at_7_of_10_is_cut_for_berries_and_no_potion_drunk(self):
        w = world(7)
        w.view.tiles[(6, 5)] = "bush"
        out = dispatch(w, ctx())
        self.assertEqual(out.state, "Heal")
        self.assertIn("cut a bush for berries", out.reason)
        self.assertEqual(out.intents, [{"verb": "Use", "target": {"kind": "block", "x": 6, "y": 5}}])
        self.assertFalse(drank(out))

    def test_at_the_low_line_food_near_still_goes_before_a_potion(self):
        w = world(4)
        w.entities = [berry()]
        out = dispatch(w, ctx())
        self.assertEqual(out.reason, "heal_food → (7, 5)")
        self.assertFalse(spend_potion(w, Memory(), Policy(kind="scripted"), dict(PARAM_DEFAULTS), "potion", food=True))

    def test_at_the_survival_floor_with_no_food_near_a_potion_is_drunk(self):
        w = world(4)
        self.assertEqual(food_near(w, Memory(), None), [])
        out = dispatch(w, ctx())
        self.assertEqual(out.state, "Heal")
        self.assertIn("use potion", out.reason)

    def test_a_bush_only_burning_opens_is_no_food(self):
        # Burning a bush spends a match: no berries are worth that.
        w = world(4)
        w.held_supplies = [POTION, InventorySupply(4, "matches")]
        w.armed_code, w.armed_id = None, None
        w.view.tiles[(6, 5)] = "bush"
        self.assertEqual(food_near(w, Memory(), None), [])


class InCombatPotionTest(unittest.TestCase):
    def test_under_attack_at_low_health_a_potion_is_drunk_with_food_near(self):
        w = world(3)
        w.entities = [berry()]
        w.attacked_tick = w.tick
        m, policy = Memory(), Policy(kind="scripted")
        self.assertTrue(in_combat(w, m, policy))
        self.assertEqual(carried_heal(w, m, policy, dict(PARAM_DEFAULTS), food=True), POTION)

    def test_in_a_level_a_potion_is_allowed(self):
        w = world(4)
        w.map_level = 1
        w.entities = [berry()]
        m, policy = Memory(), Policy(kind="scripted")
        self.assertTrue(in_combat(w, m, policy))
        self.assertTrue(spend_potion(w, m, policy, dict(PARAM_DEFAULTS), "potion", food=True))
        w.entities, w.health = [], 7
        c = ctx({"op": "use_item", "code": "potion"})
        out = dispatch(w, c)
        self.assertEqual(out.state, "UseItem", out.reason)
        self.assertEqual(out.intents[-1]["verb"], "Use")

    def test_a_fight_next_on_the_plan_lets_the_planner_drink(self):
        w = world(10)
        w.entities = [berry()]
        c = ctx({"op": "use_item", "code": "potion"}, {"op": "fight", "npc_type": "rat"})
        out = dispatch(w, c)
        self.assertEqual(out.state, "UseItem", out.reason)


class PlannerPotionRefusedTest(unittest.TestCase):
    def test_out_of_combat_with_food_near_the_planners_potion_is_refused(self):
        w = world(7)
        w.entities = [berry()]
        c = ctx({"op": "use_item", "code": "potion"})
        why = potion_refused(w, c, c.plan)
        self.assertIn("a potion is for a fight", why)
        self.assertIn("berry at 7:7,5, heal 1, 1 s walk", why)
        self.assertIn("State food_near", why)
        # Hurt, Heal goes for the food; unhurt, UseItem refuses the drink with why.
        self.assertEqual(dispatch(w, c).reason, "heal_food → (7, 5)")
        w.health = 10
        dispatch(w, c)
        self.assertIsNone(c.plan.current())
        self.assertIn("berry at 7:7,5", failures(c)[-1]["reason"])
        self.assertIn(POTION, w.held_supplies)

    def test_out_of_combat_above_the_low_line_with_no_food_it_is_kept(self):
        c = ctx({"op": "use_item", "code": "potion"})
        dispatch(world(7), c)
        self.assertIsNone(c.plan.current())
        self.assertIn("above the low_health line", failures(c)[-1]["reason"])

    def test_at_the_low_line_with_no_food_near_the_planner_may_drink(self):
        w = world(4)
        c = ctx({"op": "use_item", "code": "potion"})
        out = dispatch(w, c)
        self.assertIn(out.state, ("UseItem", "Heal"))
        self.assertIn("potion", out.reason)


class StateShowsFoodNearTest(unittest.TestCase):
    def test_food_and_bushes_near_are_shown_with_heal_and_walk(self):
        w = world(7)
        w.entities = [berry()]
        w.view.tiles[(5, 8)] = "bush"
        line = food_line(w, food_near(w, Memory(), None))
        self.assertTrue(line.startswith("food_near=["), line)
        self.assertIn('"kind": "food", "walk_seconds": 1', line)
        self.assertIn('"cell": "7:5,8", "code": "berry", "heal": 1, "kind": "bush"', line)
        self.assertIn("a potion is not drunk while any is near", line)

    def test_none_near(self):
        self.assertTrue(food_line(world(), []).startswith("food_near=none"))

    def test_the_contract_says_a_potion_is_for_a_fight(self):
        self.assertIn("A potion is for a fight", POTION_RULE)
        self.assertIn("a use_item of a potion out of combat is refused while food is near", POTION_RULE)


if __name__ == "__main__":
    unittest.main()
