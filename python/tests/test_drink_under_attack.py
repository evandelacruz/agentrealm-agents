"""A165: under attack, a held potion is drunk before anything else; no food
under attack; Retreat never steps toward a hostile closing on us; and a
chase it cannot outrun is fought, not walked under."""

import unittest

from agentrealm_adk.engagement import Engagement
from agentrealm_adk.healing import drink_first
from agentrealm_adk.item_table import InventorySupply
from agentrealm_adk.survival import beats_the_chase, closing_hostiles, opens_distance, under_attack
from agentrealm_adk.world import Entity, chebyshev
from agentrealm_adk.zone_discovery import apply_zone
from agentrealm_agent.states import dispatch
from agentrealm_agent.states.retreat import RETREAT_PROBE_TICKS
from tests.test_food_first import berry, ctx as food_ctx, world as food_world
from tests.test_survival_runs import MAP, ctx, hit, world

POTION = InventorySupply(5, "small_potion")


def closing_in(w, npc: Entity, came_from) -> None:
    """``npc`` stepped from ``came_from`` to where it stands this tick: toward us."""
    w.entity_moves[(npc.kind, npc.id)] = (came_from, w.tick)


def step_of(out):
    return (out.intents[0]["x"], out.intents[0]["y"])


class DrinkFirstTest(unittest.TestCase):
    """Health falls through the low line with a potion held while Retreat walks."""

    def setUp(self):
        self.w, self.c = world(health=6), ctx(on_hostile="fight")  # the low line: 5 of 10
        apply_zone(self.w, MAP, -10, 10, {"safe": True})
        self.w.entities = [Entity("npc", 7, (11, 10), code="chaser")]
        self.w.held_supplies = [POTION]
        hit(self.w)

    def retreating(self):
        out = dispatch(self.w, self.c)
        self.assertEqual(out.state, "Retreat", "above the low line: no drink yet")
        m = self.c.memory
        m.held_queue = {"queue_id": "q1", "next_index": 1}  # its walk queue under way
        self.w.tick += 1
        hit(self.w)
        self.w.health = 5  # through the bar
        return m

    def test_he_drinks_on_the_next_tick(self):
        self.retreating()
        out = dispatch(self.w, self.c)
        self.assertEqual(out.state, "Heal")
        self.assertEqual([i["verb"] for i in out.intents], ["Arm", "Use"])
        self.assertTrue(out.reflex, "a reflex: it goes out over the held walk queue")

    def test_the_held_queue_probe_sends_it_too(self):
        self.retreating()
        self.c.probe = True
        out = dispatch(self.w, self.c)
        self.assertEqual(out.state, "Heal")
        self.assertTrue(out.reflex)
        self.assertEqual(out.intents[-1], {"verb": "Use", "target": {"kind": "self"}})

    def test_the_drink_and_its_arm_wait_on_no_cooldown(self):
        m = self.retreating()
        m.last_use_tick = self.w.tick - 1  # a swing a tick ago
        out = dispatch(self.w, self.c)
        self.assertEqual([i["verb"] for i in out.intents], ["Arm", "Use"], "no Wait between them")

    def test_above_the_line_no_drink_goes_first(self):
        self.assertFalse(drink_first(self.w, self.c.policy, self.c.params))
        self.assertEqual(dispatch(self.w, self.c).state, "Retreat")

    def test_a_hostile_closing_is_under_attack_with_no_hit(self):
        w, c = world(health=5), ctx(on_hostile="fight")
        npc = Entity("npc", 7, (14, 10), code="chaser")
        w.entities = [npc]
        w.hostile_types.add(("npc", "chaser"))
        self.assertFalse(under_attack(w, c.policy))
        closing_in(w, npc, (16, 10))
        self.assertEqual(closing_hostiles(w, c.policy), [npc])
        self.assertTrue(under_attack(w, c.policy))
        self.assertTrue(drink_first(w, c.policy, c.params))


class NoFoodUnderAttackTest(unittest.TestCase):
    """At health 1 with a hostile closing from out of range, Heal walks to no food."""

    def setUp(self):
        self.w, self.c = food_world(1), food_ctx()
        self.w.held_supplies = [s for s in self.w.held_supplies if s.code != "potion"]
        self.npc = Entity("npc", 7, (5, 10), code="chaser")  # 5 cells off: out of range
        self.w.hostile_types.add(("npc", "chaser"))

    def test_no_walk_to_food_lying_near(self):
        self.w.entities = [berry(), self.npc]
        closing_in(self.w, self.npc, (5, 11))
        self.assertTrue(under_attack(self.w, self.c.policy))
        out = dispatch(self.w, self.c)
        self.assertNotIn("heal_food", out.reason)
        self.assertNotEqual(out.state, "Heal")

    def test_no_bush_cut_for_food(self):
        self.w.view.tiles[(6, 5)] = "bush"
        self.w.entities = [self.npc]
        closing_in(self.w, self.npc, (5, 11))
        out = dispatch(self.w, self.c)
        self.assertNotIn("break for food", out.reason)

    def test_no_food_after_a_hit_either(self):
        self.w.entities = [berry()]
        hit(self.w, npc_id=9)  # from out of view
        out = dispatch(self.w, self.c)
        self.assertNotIn("heal_food", out.reason)

    def test_a_far_hostile_wandering_closer_is_no_attack(self):
        far = Entity("npc", 8, (5, 13), code="chaser")  # 8 off: beyond the reach a fight counts
        self.w.entities = [berry(), far]
        closing_in(self.w, far, (5, 14))
        self.assertFalse(under_attack(self.w, self.c.policy))
        self.assertEqual(dispatch(self.w, self.c).reason, "heal_food → (7, 5)")

    def test_with_none_closing_the_food_is_walked_to(self):
        self.w.entities = [berry(), self.npc]  # it stands still: not closing
        self.assertEqual(dispatch(self.w, self.c).reason, "heal_food → (7, 5)")


class RetreatStepsAwayTest(unittest.TestCase):
    """The pack 2–3 cells NW, the refuge far west: the straight first step is up_left, into the pack."""

    def setUp(self):
        self.w, self.c = world(health=2), ctx()
        apply_zone(self.w, MAP, -25, 0, {"safe": True})
        self.pack = [Entity("npc", 7, (8, 8), code="chaser"), Entity("npc", 8, (7, 9), code="chaser")]
        self.w.entities = list(self.pack)
        hit(self.w)
        closing_in(self.w, self.pack[0], (7, 7))
        closing_in(self.w, self.pack[1], (6, 8))

    def test_the_first_step_opens_distance_from_every_closing_hostile(self):
        here = self.w.pos
        self.assertFalse(opens_distance(here, (9, 9), self.pack), "up_left closes on the pack")
        out = dispatch(self.w, self.c)
        self.assertEqual(out.state, "Retreat")
        step = step_of(out)
        self.assertNotEqual(step, (9, 9))
        self.assertTrue(opens_distance(here, step, self.pack), step)

    def test_a_held_queue_stepping_into_the_pack_is_planned_again(self):
        m = self.c.memory
        dispatch(self.w, self.c)
        m.state, m.held_queue = "Retreat", {"queue_id": "q1", "next_index": 0}
        m.pending_intents, m.pending_next_index = [{"verb": "Step", "direction": "up_left"}], 0
        self.w.tick += 1
        out = dispatch(self.w, self.c)
        self.assertFalse(out.wait, "not left running into the pack")
        self.assertTrue(opens_distance(self.w.pos, step_of(out), self.pack))


class RetreatKeepsMovingTest(unittest.TestCase):
    """A step that keeps the distance is no step toward a hostile, and Retreat
    hemmed in still sends something."""

    def test_a_hostile_far_north_leaves_sideways_steps_open(self):
        w, c = world(health=2), ctx()
        apply_zone(w, MAP, 20, 10, {"safe": True})
        for y in range(5, 10):
            w.view.tiles[(12, y)] = "wall"  # the refuge east is round a wall
        far = Entity("npc", 8, (10, 4), code="chaser")  # 6 north, closing
        near = Entity("npc", 7, (9, 11), code="chaser")
        w.entities = [near, far]
        hit(w)
        closing_in(w, far, (10, 3))
        self.assertTrue(opens_distance(w.pos, (11, 10), [far]), "east keeps the distance")
        out = dispatch(w, c)
        self.assertEqual(out.state, "Retreat", out.yielded)
        self.assertTrue(out.intents)
        self.assertTrue(opens_distance(w.pos, step_of(out), closing_hostiles(w, c.policy)))

    def test_hostiles_closing_from_both_sides_never_freeze_it(self):
        w, c = world(health=2), ctx()
        apply_zone(w, MAP, 20, 10, {"safe": True})
        east, west = Entity("npc", 7, (12, 10), code="chaser"), Entity("npc", 8, (8, 10), code="chaser")
        w.entities = [east, west]
        hit(w)
        closing_in(w, east, (13, 10))
        closing_in(w, west, (7, 10))
        out = dispatch(w, c)
        self.assertEqual(out.state, "Retreat", out.yielded)
        self.assertTrue(out.intents, out.reason)
        self.assertTrue(opens_distance(w.pos, step_of(out), [east, west]), out.reason)


class CannotOutrunTest(unittest.TestCase):
    """The chaser keeps pace: once running is found not to work, a fight the
    engagement takes is fought, not walked under to a far refuge."""

    def setUp(self):
        self.w, self.c = world(health=2), ctx(on_hostile="fight")
        self.npc = Entity("npc", 7, (11, 10), code="chaser")
        self.w.entities = [self.npc]
        hit(self.w, amount=1)

    def outran(self) -> None:
        self.c.memory.engagement = Engagement(frozenset({("npc", 7)}), cannot_outrun=True)

    def test_a_far_refuge_is_not_walked_to(self):
        apply_zone(self.w, MAP, -10, 10, {"safe": True})
        self.assertFalse(beats_the_chase(self.w.pos, (-10, 10), [self.npc]))
        self.outran()
        out = dispatch(self.w, self.c)
        self.assertTrue(self.c.memory.engagement.fight, "decided again: a fight it wins outright")
        self.assertNotIn("retreat → safe", out.reason)
        self.assertEqual(out.reason, "cannot outrun to the refuge: fight npc 7")
        self.assertIn("Use", [i["verb"] for i in out.intents])

    def test_a_near_safe_block_closer_than_the_pack_is_taken(self):
        self.npc.pos = (12, 10)  # in range, 2 off: the safe block 1 off is nearer
        apply_zone(self.w, MAP, 9, 10, {"safe": True})
        self.assertTrue(beats_the_chase(self.w.pos, (9, 10), [self.npc]))
        self.outran()
        out = dispatch(self.w, self.c)
        self.assertEqual(out.reason, "retreat → safe (9, 10)")

    def test_a_chase_that_keeps_pace_under_hits_is_running_not_working(self):
        apply_zone(self.w, MAP, -10, 10, {"safe": True})
        out = dispatch(self.w, self.c)
        self.assertTrue(out.reason.startswith("retreat → safe (-10, 10)"), out.reason)
        self.assertFalse(self.c.memory.engagement.cannot_outrun)
        # Two steps on toward the refuge, the chaser a step behind all the way.
        self.w.pos, self.npc.pos = (8, 10), (9, 10)
        self.w.tick += RETREAT_PROBE_TICKS
        closing_in(self.w, self.npc, (11, 10))
        hit(self.w, amount=1)
        dispatch(self.w, self.c)
        self.assertTrue(self.c.memory.engagement.cannot_outrun)
        self.assertGreater(chebyshev(self.w.pos, (-10, 10)), chebyshev(self.w.pos, self.npc.pos))


if __name__ == "__main__":
    unittest.main()
