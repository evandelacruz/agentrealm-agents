"""A135: out of a fight lost health comes back from food; a potion is for a fight."""

import random
import unittest
from unittest import mock

from agentrealm_agent.config import Policy
from agentrealm_agent.directives import PARAM_DEFAULTS, default_directives
from agentrealm_agent.healing import carried_heal, food_near, in_combat, spend_potion
from agentrealm_agent.item_table import InventorySupply
from agentrealm_agent.memory import Memory
from agentrealm_agent.plan import Plan
from agentrealm_agent.states import PlayContext, dispatch
from agentrealm_agent.states.heal import use_carried_heal
from agentrealm_agent.states.use_item import fight_ahead, potion_refused
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
    return out is not None and "potion" in out.reason and any(i.get("verb") == "Use" for i in out.intents or [])


def berry(at=(7, 5)) -> Entity:
    return Entity("supply", 50, at, "berry")


def walled(w: WorldModel, at=(7, 5)) -> None:
    """Rock round ``at``, which no held item opens: food there no walk reaches."""
    for x in range(at[0] - 1, at[0] + 2):
        for y in range(at[1] - 1, at[1] + 2):
            if (x, y) != at:
                w.view.tiles[(x, y)] = "rock"


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
        self.assertIn("break for food", out.reason)
        self.assertEqual(out.intents, [{"verb": "Use", "target": {"kind": "block", "x": 6, "y": 5}}])

    def test_at_the_low_line_food_near_still_goes_before_a_potion(self):
        w = world(4)
        w.entities = [berry()]
        self.assertEqual(dispatch(w, ctx()).reason, "heal_food → (7, 5)")

    def test_at_the_survival_floor_with_no_food_near_a_potion_is_drunk(self):
        w = world(4)
        self.assertEqual(food_near(w, Memory(), None), ([], []))
        out = dispatch(w, ctx())
        self.assertEqual(out.state, "Heal")
        self.assertTrue(drank(out), out.reason)

    def test_food_near_no_walk_reaches_lets_the_potion_be_drunk_at_the_floor(self):
        # The food is a candidate, but Heal cannot act on it: the potion is drunk.
        w = world(4)
        w.entities = [berry()]
        walled(w)
        self.assertEqual(len(food_near(w, Memory(), None)[0]), 1)
        out = dispatch(w, ctx())
        self.assertTrue(drank(out), out.reason)

    def test_a_bush_whose_break_sends_nothing_lets_the_potion_be_drunk_at_the_floor(self):
        w = world(4)
        w.view.tiles[(6, 5)] = "bush"
        from agentrealm_agent.states import heal
        from agentrealm_agent.states.base import StateOutcome

        with mock.patch.object(heal, "break_toward", return_value=StateOutcome(None, "no step", state="Heal")):
            out = dispatch(w, ctx())
        self.assertTrue(drank(out), out.reason)

    def test_a_bush_only_burning_opens_is_no_food(self):
        # Burning a bush uses up a match: no berries are worth that.
        w = world(4)
        w.held_supplies = [POTION, InventorySupply(4, "matches")]
        w.armed_code, w.armed_id = None, None
        w.view.tiles[(6, 5)] = "bush"
        self.assertEqual(food_near(w, Memory(), None), ([], []))


class InCombatPotionTest(unittest.TestCase):
    def test_under_attack_at_low_health_a_potion_is_drunk_with_food_near(self):
        # Flee cornered and Retreat losing ground drink through use_carried_heal.
        w = world(3)
        w.entities = [berry()]
        w.attacked_tick = w.tick
        c = ctx()
        self.assertTrue(in_combat(w, c.memory, c.policy))
        self.assertEqual(carried_heal(w, c.memory, c.policy, dict(PARAM_DEFAULTS), food=True), POTION)
        self.assertTrue(drank(use_carried_heal(w, c)))

    def test_recently_attacked_heal_drinks_before_cutting_a_bush(self):
        w = world(3)
        w.view.tiles[(6, 5)] = "bush"
        w.attacked_tick = w.tick
        out = dispatch(w, ctx())
        self.assertEqual(out.state, "Heal")
        self.assertTrue(drank(out), out.reason)

    def test_in_a_level_a_potion_is_allowed(self):
        w = world(4)
        w.map_level = 1
        m, policy = Memory(), Policy(kind="scripted")
        self.assertTrue(in_combat(w, m, policy))
        self.assertTrue(spend_potion(w, m, policy, dict(PARAM_DEFAULTS), "potion", food=True))
        w.health = 7
        c = ctx({"op": "use_item", "code": "potion"})
        out = dispatch(w, c)
        self.assertEqual(out.state, "UseItem", out.reason)
        self.assertEqual(out.intents[-1]["verb"], "Use")

    def test_a_fight_next_on_the_plan_lets_the_planner_drink(self):
        w = world(10)
        w.entities = [berry()]
        c = ctx({"op": "use_item", "code": "potion"}, {"op": "fight", "npc_type": "rat"})
        self.assertEqual(dispatch(w, c).state, "UseItem")

    def test_what_counts_as_a_fight_next(self):
        use = {"op": "use_item", "code": "potion"}
        for nxt in ({"op": "fight_boss"}, {"op": "enter_level"}, {"op": "gather_gems", "fight": True}):
            self.assertTrue(fight_ahead(Plan([use, nxt], dict(PARAM_DEFAULTS))), nxt)
        for nxt in ({"op": "gather_gems"}, {"op": "travel", "to": "town"}):
            self.assertFalse(fight_ahead(Plan([use, nxt], dict(PARAM_DEFAULTS))), nxt)
        self.assertFalse(fight_ahead(Plan([use], dict(PARAM_DEFAULTS))))


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

    def test_a_bush_near_is_named_as_the_food(self):
        w = world(10)
        w.view.tiles[(6, 5)] = "bush"
        c = ctx({"op": "use_item", "code": "potion"})
        self.assertIn("a bush at 7:6,5 to break for berry", potion_refused(w, c, c.plan))

    def test_food_no_walk_reaches_is_no_reason_to_refuse(self):
        # The same food test as Heal: food it cannot walk to is not named.
        w = world(4)
        w.entities = [berry()]
        walled(w)
        c = ctx({"op": "use_item", "code": "potion"})
        self.assertIsNone(potion_refused(w, c, c.plan))

    def test_out_of_combat_above_the_low_line_with_no_food_it_is_kept(self):
        c = ctx({"op": "use_item", "code": "potion"})
        dispatch(world(7), c)
        self.assertIsNone(c.plan.current())
        self.assertIn("above the low_health line", failures(c)[-1]["reason"])


class StateShowsFoodNearTest(unittest.TestCase):
    def test_food_and_bushes_near_are_shown_with_heal_and_walk(self):
        w = world(7)
        w.entities = [berry()]
        w.view.tiles[(5, 8)] = "bush"
        line = food_line(w, None, Memory())
        self.assertTrue(line.startswith("food_near=["), line)
        self.assertIn('"cell": "7:7,5", "code": "berry", "heal": 1, "kind": "food", "walk_seconds": 1', line)
        self.assertIn('"cell": "7:5,8", "code": "berry", "heal": 1, "kind": "bush"', line)

    def test_none_near(self):
        self.assertTrue(food_line(world(), None, Memory()).startswith("food_near=none"))

    def test_the_contract_says_a_potion_is_for_a_fight(self):
        self.assertIn("A potion is for a fight", POTION_RULE)
        self.assertIn("a use_item of a potion out of combat is refused while food near can be had", POTION_RULE)


if __name__ == "__main__":
    unittest.main()
