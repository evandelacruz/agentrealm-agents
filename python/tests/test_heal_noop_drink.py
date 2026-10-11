"""A76: Heal works out why a drink that applied drank nothing, and acts on it.

A drink applied when its result is ``applied`` or ``applied_no_effect``; it
drank something only if the supply then leaves what we carry, our own
``SupplyUsed`` names it, or health rises. Otherwise the next decision files
the cause (``healing.noop_drink_cause``): something else armed re-arms, full
health waits for less, and anything else holds that supply until what we
carry, the map or max health changes, or health falls below what it was:
a potion is never unusable while health falls. Free-play run 4: a drink
applied at 8/10 and the potions stayed 2 → 2.

The inventory has the observed shape: the armed supply is in ``armed`` (with
its ``id``) and not in ``held`` (A67 run 1, GAME_NOTES open questions).
"""

from __future__ import annotations

import unittest

from agentrealm_agent.healing import drank, potion_count
from agentrealm_agent.item_table import InventorySupply

from tests.test_heal_refusal import KNIFE, POTION_A, USE_SELF, RefusalTest

ARM_A = {"verb": "Arm", "supply_id": 4}


class NoopDrinkTest(RefusalTest):
    def setUp(self):
        super().setUp()
        w = self.r.world
        w.held_supplies, w.armed_id = [POTION_A], 1  # the knife armed

    def drink(self, outcome: str = "applied", *, events: list[dict] | None = None) -> list[dict]:
        """Decide a drink and apply its results: the Arm, then the Use."""
        sent = self.decide()
        self.assertEqual(sent[-1], USE_SELF)
        for index, intent in enumerate(sent):
            self.r.on_result({"outcome": "applied" if intent["verb"] == "Arm" else outcome, "tick": self.r.world.tick}, index)
        if events and self.r._applied_drink is not None:
            # As Runner.tick does with the response's events.
            self.r._applied_drink.used = drank(events, self.r.cid, self.r._applied_drink.code)
        return sent

    def potion_armed(self) -> None:
        """The observation after the drink: the potion in the slot, still
        carried, and the knife back in ``held``."""
        w = self.r.world
        w.held_supplies = [KNIFE] + [h for h in w.held_supplies if h.id != 4]
        w.armed_code, w.armed_id = "small_potion", 4

    def knife_rearmed(self) -> None:
        """Heal's re-arm after the drink: the knife in the slot, the potion held."""
        w = self.r.world
        w.held_supplies = [POTION_A] + [h for h in w.held_supplies if h.id != 1]
        w.armed_code, w.armed_id = "pocket_knife", 1

    def test_a_drink_that_used_the_potion_up_files_nothing(self):
        self.drink()
        w = self.r.world
        w.held_supplies, w.armed_code, w.armed_id = [KNIFE], None, None
        self.next_tick()
        self.decide()
        self.assertNotIn(("use", 4), self.r.mem.heal_refusals)

    def test_our_supply_used_event_counts_as_drunk(self):
        # The inventory delta may lag; our own SupplyUsed says it was drunk.
        self.drink(events=[{"kind": "SupplyUsed", "actor_id": self.r.cid, "supply_code": "small_potion"}])
        self.potion_armed()
        self.next_tick()
        self.decide()
        self.assertNotIn(("use", 4), self.r.mem.heal_refusals)

    def test_another_characters_supply_used_is_not_ours(self):
        self.drink(events=[{"kind": "SupplyUsed", "actor_id": 999, "supply_code": "small_potion"}])
        self.potion_armed()
        self.next_tick()
        self.decide()
        self.assertEqual(self.r.mem.heal_refusals[("use", 4)].cause, "no_change")

    def test_health_rising_counts_as_drunk(self):
        self.drink()
        self.potion_armed()
        self.r.world.health = 9
        self.next_tick()
        self.decide()
        self.assertNotIn(("use", 4), self.r.mem.heal_refusals)

    def test_armed_and_hurt_and_nothing_changed_holds_until_what_we_carry_changes(self):
        self.drink()
        self.potion_armed()
        self.next_tick()
        self.assertNotIn(USE_SELF, self.decide(), "the same drink is not resent")
        self.assertTrue(any("drank nothing (no_change): hold" in line for line in self.lines))
        w = self.r.world
        # The re-arm, a walk and regen change nothing the drink depends on.
        self.knife_rearmed()
        w.pos, w.health = (3, 4), 8
        for _ in range(5):
            self.next_tick(7)
            self.assertNotIn(USE_SELF, self.decide())
        w.held_supplies = [POTION_A, InventorySupply(6, "bronze_sword")]  # a new item
        self.next_tick()
        self.assertEqual(self.decide(), [ARM_A, USE_SELF])

    def test_a_hold_lifts_once_health_falls_below_it(self):
        # Mid-fight nothing else changes: the potion that did nothing at 7/10
        # is tried again at 6/10, never left unusable as health falls (A67 run 5).
        self.drink()
        self.potion_armed()
        self.next_tick()
        self.assertNotIn(USE_SELF, self.decide())
        self.knife_rearmed()
        w = self.r.world
        w.health = 8  # regen, then a hit back to the reading: not below it
        self.next_tick()
        self.assertNotIn(USE_SELF, self.decide())
        w.health = 7
        self.next_tick()
        self.assertNotIn(USE_SELF, self.decide())
        w.health = 6
        self.next_tick()
        self.assertEqual(self.decide(), [ARM_A, USE_SELF])

    def test_applied_no_effect_is_read_the_same_way(self):
        self.drink("applied_no_effect")
        self.potion_armed()
        self.next_tick()
        self.assertNotIn(USE_SELF, self.decide())
        self.assertEqual(self.r.mem.heal_refusals[("use", 4)].cause, "no_change")

    def test_another_supply_is_still_drunk(self):
        w = self.r.world
        w.held_supplies = [POTION_A, InventorySupply(5, "small_potion")]
        self.drink()
        self.potion_armed()
        self.next_tick()
        self.assertEqual(self.decide(), [{"verb": "Arm", "supply_id": 5}, USE_SELF])

    def test_something_else_armed_re_arms_the_next_drink(self):
        # armed_code said the potion was armed, so the Use went alone; the
        # observation shows the knife in the slot: the next drink arms first.
        w = self.r.world
        w.held_supplies, w.armed_code, w.armed_id = [KNIFE], "small_potion", 4
        self.assertEqual(self.drink(), [USE_SELF])
        self.knife_rearmed()
        self.next_tick()
        self.assertEqual(self.decide(), [ARM_A, USE_SELF])
        self.assertEqual(self.r.mem.heal_refusals[("use", 4)].cause, "not_armed")

    def test_something_else_armed_after_its_own_arm_holds(self):
        self.assertEqual(self.drink(), [ARM_A, USE_SELF])
        self.next_tick()  # the knife is still in the slot: the Arm took no hold
        self.assertNotIn(USE_SELF, self.decide())
        self.assertEqual(self.r.mem.heal_refusals[("use", 4)].action, "hold")

    def test_full_health_waits_for_health_below_it(self):
        self.drink()
        self.potion_armed()
        w = self.r.world
        w.health = 7  # unchanged: regen filled nothing, but the server saw full health
        w.max_health = 7
        self.next_tick()
        self.decide()
        r = self.r.mem.heal_refusals[("use", 4)]
        self.assertEqual((r.cause, r.action), ("full_health", "full"))
        w.max_health = 20  # hurt again by this reading, health not below 7
        self.next_tick()
        self.assertNotIn(USE_SELF, self.decide())
        w.health = 6
        self.next_tick()
        self.assertIn(USE_SELF, self.decide(), "health fell below the full reading: drink again")

    def test_an_armed_potion_left_out_of_held_still_counts(self):
        w = self.r.world
        self.assertEqual(potion_count(w), 1)
        self.potion_armed()
        self.assertEqual(potion_count(w), 1, "a potion moved into the slot is not a potion gone")

    def test_a_held_queue_probe_files_nothing(self):
        # Only a server result keeps a drink to check; a probe decides and sends nothing.
        self.decide()
        self.next_tick()
        self.r.file_heal_refusals()
        self.assertIsNone(self.r._applied_drink)
        self.assertNotIn(("use", 4), self.r.mem.heal_refusals)


if __name__ == "__main__":
    unittest.main()
