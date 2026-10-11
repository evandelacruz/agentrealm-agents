"""A96: one rule for when a carried potion is spent, read by Heal, Retreat and the planner.

Free-play run 11: the planner bought a potion for the reserve and Heal drank
it a second later at 7/10 in town, so the reserve never built. A potion is
now drunk only when health is low (``healing.health_low``) or the win
estimate says the drink turns the fight under way; otherwise Heal eats food
or rests, and the planner's prompt and State say the same.
"""

from __future__ import annotations

import unittest

from agentrealm_agent.directives import PARAM_DEFAULTS, Directives
from agentrealm_agent.engagement import drink_turns_fight, sync_engagement
from agentrealm_agent.healing import POTION_RULE, carried_heal, spend_potion
from agentrealm_agent.item_table import InventorySupply
from agentrealm_agent.plan import Plan
from agentrealm_agent.states import dispatch
from agentrealm_agent.states.retreat import _turn_on_losing
from agentrealm_agent.strategist import build_prompt, potion_line, system_prompt

from tests.test_engagement import biter, ctx as fight_ctx, world as open_ground
from tests.test_heal import ctx, grid, verbs

POTION = InventorySupply(4, "small_potion")
BERRY = InventorySupply(5, "berry")


def in_town(health: int) -> tuple:
    """Hurt on a safe cell in town, a potion held, nothing in sight."""
    w = grid(at=(0, 0))
    w.health = health
    w.held_supplies = [POTION]
    return w, ctx()


class HealKeepsTheReserveTest(unittest.TestCase):
    def test_not_low_in_town_rests_and_keeps_the_potion(self):
        w, c = in_town(7)
        out = dispatch(w, c)
        self.assertEqual(out.state, "Heal")
        self.assertNotIn("Use", verbs(out))
        self.assertIsNone(c.memory.heal_drink)

    def test_low_drinks_it(self):
        w, c = in_town(5)
        self.assertEqual(verbs(dispatch(w, c)), ["Arm", "Use"])

    def test_carried_food_is_eaten_whatever_the_health(self):
        w, c = in_town(9)
        w.held_supplies = [POTION, BERRY]
        out = dispatch(w, c)
        self.assertEqual(out.intents[0], {"verb": "Arm", "supply_id": BERRY.id})

    def test_no_potion_offered_above_the_line(self):
        w, c = in_town(6)
        self.assertIsNone(carried_heal(w, c.memory, c.policy, c.params))
        w.health = 5
        self.assertEqual(carried_heal(w, c.memory, c.policy, c.params), POTION)


class DrinkInAFightTest(unittest.TestCase):
    """A drink is worth it in a fight when the win estimate changes with it."""

    def engaged(self, health: int, hostiles: int):
        w, c = open_ground(health=health), fight_ctx()
        w.entities = [biter(7 + i, (11, 10 + i)) for i in range(hostiles)]
        w.held_supplies = [POTION]
        sync_engagement(w, c.memory, c.policy, c.params)
        return w, c

    def test_a_drink_that_turns_the_fight_is_spent(self):
        w, c = self.engaged(6, 1)  # one biter: not fought at 6/10, fought at 10/10
        self.assertFalse(c.memory.engagement.fight)
        self.assertTrue(drink_turns_fight(w, c.memory, c.policy, c.params, 10))
        self.assertTrue(spend_potion(w, c.memory, c.policy, c.params, POTION.code))

    def test_a_fight_already_won_keeps_the_potion(self):
        w, c = self.engaged(9, 1)
        self.assertTrue(c.memory.engagement.fight)
        self.assertFalse(spend_potion(w, c.memory, c.policy, c.params, POTION.code))

    def test_a_fight_lost_either_way_keeps_the_potion(self):
        w, c = self.engaged(6, 2)  # a pair beats us at 6/10 and at 10/10
        self.assertFalse(spend_potion(w, c.memory, c.policy, c.params, POTION.code))

    def test_each_potion_held_is_asked_for_its_own_heal(self):
        # Three biters at 51/100: 10 more health changes nothing, 30 more makes it a fight.
        w, c = self.engaged(51, 3)
        w.max_health = 100
        sync_engagement(w, c.memory, c.policy, c.params)
        large = InventorySupply(5, "large_potion")
        w.held_supplies = [POTION, large]
        self.assertFalse(spend_potion(w, c.memory, c.policy, c.params, POTION.code))
        self.assertEqual(carried_heal(w, c.memory, c.policy, c.params), large)

    def test_retreat_losing_ground_reads_the_same_rule(self):
        w, c = self.engaged(6, 2)
        self.assertIsNone(_turn_on_losing(w, c, "Retreat"), "no drink, and no fight to turn to")
        # A pair at 21/40 loses (ratio under break-even); 10 more health wins it.
        w, c = self.engaged(21, 2)
        w.max_health = 40
        sync_engagement(w, c.memory, c.policy, c.params)
        out = _turn_on_losing(w, c, "Retreat")
        self.assertEqual(verbs(out), ["Arm", "Use"])
        self.assertEqual(c.memory.heal_drink, POTION.id)

    def test_retreat_weighs_the_drink_once_running_is_found_not_to_work(self):
        # A pair at 26/40: under the fight margin (1.5) but above break-even. Once
        # running cannot open distance the bar is break-even, so it is a fight
        # already and a drink turns nothing.
        w, c = self.engaged(26, 2)
        w.max_health = 40
        sync_engagement(w, c.memory, c.policy, c.params)
        self.assertFalse(c.memory.engagement.fight)
        out = _turn_on_losing(w, c, "Retreat")
        self.assertTrue(c.memory.engagement.cannot_outrun and c.memory.engagement.fight)
        self.assertNotIn("Use", verbs(out) if out else [])
        self.assertIsNone(c.memory.heal_drink)


class PlannerSeesTheRuleTest(unittest.TestCase):
    def test_the_prompt_states_the_rule(self):
        self.assertIn(POTION_RULE, system_prompt())

    def test_state_shows_the_line_and_the_count(self):
        w, _ = in_town(7)
        w.held_supplies = [POTION, InventorySupply(6, "small_potion")]
        self.assertEqual(potion_line(w), "potions=2 potion_drunk_at=health<=5")
        msgs = build_prompt(triggers=[], w=w, plan=Plan([], dict(PARAM_DEFAULTS)), directives=Directives(), knowledge=None)
        self.assertIn("potions=2 potion_drunk_at=health<=5", msgs[-1]["content"])


if __name__ == "__main__":
    unittest.main()
