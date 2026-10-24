"""One value for every target (A170): food, chests and gems priced alike, on every walk."""

import unittest
from unittest import mock

from agentrealm_adk import value
from agentrealm_adk.item_table import InventorySupply
from agentrealm_adk.knowledge_base import KnowledgeBase
from agentrealm_adk.world import DeathChest, Entity
from ladder import act, goals, readiness
from ladder.act import Ctx
from agentrealm_adk.travel.knowledge import record_shop_cell
from tests import test_ladder_act
from tests.test_ladder_act import ctx, nearer, open_ground
from tests.test_ladder_goals import MAP, hostile, knowledge, world

# A world whose reference prices a health point at a small potion's: 10 gems for 10.
WORLD = "olympuff"


def kb() -> KnowledgeBase:
    return KnowledgeBase.empty(WORLD)


def berry(w, eid=20, at=(7, 5)) -> Entity:
    e = Entity("supply", eid, at, "berry")
    w.entities.append(e)
    return e


class ValueTest(unittest.TestCase):
    """The ADK's one value (``agentrealm_adk.value``)."""

    def test_food_is_worth_the_health_it_would_give_back(self):
        w = world()
        self.assertEqual(value.supply_value("apple", w, kb()), 0, "nothing to give back at full health")
        w.health = 9
        self.assertEqual(value.supply_value("apple", w, kb()), 1.0, "1 of its 2 at a gem a health point")
        w.health = 5
        self.assertEqual(value.supply_value("apple", w, kb()), 2.0)

    def test_a_health_point_costs_the_cheapest_potion_seen_else_the_reference(self):
        w = world()
        self.assertEqual(value.gems_per_health(w, kb()), 1.0)  # a small potion, 10 for 10
        w.entities = [Entity("supply", 5, (9, 9), "small_potion", gem_price=5)]
        self.assertEqual(value.gems_per_health(w, kb()), 0.5)

    def test_gems_lives_and_priced_items(self):
        w = world()
        self.assertEqual(value.supply_value("gem", w, kb()), 1.0)
        self.assertEqual(value.supply_value("gem_cache_7", w, kb()), 7.0)
        self.assertEqual(value.supply_value("heart", w, kb()), value.LIFE_GEMS)
        self.assertEqual(value.supply_value("bronze_sword", w, kb()), 15.0)

    def test_a_chest_is_worth_what_it_holds_that_fits(self):
        w = world(carry_capacity=3)
        self.assertEqual(value.chest_value(["bronze_sword", "small_potion"], w, kb()), 25.0)
        w.held_supplies = [InventorySupply(1, "rope")]  # with the sword armed, one slot left
        self.assertEqual(value.chest_value(["bronze_sword", "small_potion"], w, kb()), 15.0)

    def test_a_clear_step_is_nearly_free_and_urgency_declines_every_find(self):
        self.assertEqual(value.step_gems(), value.STEP_GEMS)
        self.assertAlmostEqual(value.net(1.0, 4), 0.6)
        self.assertEqual(value.step_gems(urgent=True), value.URGENT)
        self.assertEqual(value.allowance(1.0, value.URGENT), -1)

    def test_a_chest_never_opened_is_worth_what_opened_chests_held(self):
        w, k = world(), kb()
        self.assertEqual(value.expected_chest_gems(w, k), value.CHEST_PRIOR_GEMS)
        w.chest_contents = {7: [InventorySupply(1, "bronze_sword")], 8: []}  # a storage chest seen empty counts nothing
        value.learn_opened_chests(w, k)
        value.learn_opened_chests(w, k)  # each chest once
        expected = (15 + value.CHEST_PRIOR_GEMS * value.CHEST_PRIOR_WEIGHT) / (1 + value.CHEST_PRIOR_WEIGHT)
        self.assertAlmostEqual(value.expected_chest_gems(w, k), expected)
        w.entities = [Entity("chest", 8, (9, 5))]
        self.assertEqual([f.id for f in value.finds(w, k)], [], "the empty one is worth nothing")

    def test_a_bush_is_worth_its_berry_times_the_chance_learned_from_cuts(self):
        w, k = world(health=6), kb()
        self.assertAlmostEqual(value.bush_value("bush", w, k), value.BUSH_PRIOR * 1.0)
        self.assertEqual(value.bush_value("bush", world(), k), 0, "no berry is worth anything at full health")
        watch = value.DropWatch()
        for i, berry_dropped in enumerate([True] * 10):
            cell = (20 + i, 5)
            w.view.tiles[cell] = "bush"
            watch.note_cut(w, cell, w.tick)
            w.view.tiles[cell] = "dirt"
            if berry_dropped:
                w.entities = [Entity("supply", 100 + i, cell, "berry")]
            watch.update(w, k)
            w.tick += 10
            watch.update(w, k)
        self.assertGreater(value.berry_chance(k), value.BUSH_PRIOR)

    def test_a_cut_that_left_the_bush_standing_passes_it_over(self):
        w, k = world(), kb()
        w.view.tiles[(6, 5)] = "bush"
        watch = value.DropWatch()
        watch.note_cut(w, (6, 5), w.tick)
        w.tick += 10
        watch.update(w, k)
        self.assertTrue(watch.unbroken(w, (6, 5)))
        self.assertEqual(k.extra.get(value.BUSH_KEY), None, "no break: nothing filed")


def gather(w, k=None):
    return act.gather_targets(ctx(w, k or kb()))


class CutLandsTest(unittest.TestCase):
    """A bush cut is judged from the tick its ``Use`` landed (``LadderRunner.apply_results``)."""

    setUp = test_ladder_act.LoopTest.setUp
    runner = test_ladder_act.LoopTest.runner

    def test_a_cut_behind_a_cooldown_is_judged_from_when_it_landed(self):
        w = open_ground(world(health=6, armed_code="pocket_knife", armed_id=2))
        w.view.tiles[(6, 5)] = "bush"
        r = self.runner(w)
        queue = [{"verb": "Wait", "ticks": 8}, {"verb": "Use", "target": {"kind": "block", "x": 6, "y": 5}}]
        r.apply({"tick": 3, "queue_id": "q1"}, mock.Mock(intents=queue))
        w.tick = 10
        r.drops.update(w, r.knowledge)  # past the window from the send, the cut not yet landed
        self.assertFalse(r.drops.unbroken(w, (6, 5)))
        self.assertEqual(r.drops.pending, [])
        results = [{"queue_id": "q1", "index": i, "outcome": "applied", "tick": 11} for i in (0, 1)]
        r.apply({"tick": 11, "intent_results": results}, mock.Mock(intents=None))
        self.assertEqual([p for _, p, _ in r.drops.pending], [(6, 5)])
        w.view.tiles[(6, 5)] = "dirt"
        w.entities = [Entity("supply", 9, (6, 5), "berry")]
        w.tick = 12
        r.drops.update(w, r.knowledge)
        w.tick = 20
        r.drops.update(w, r.knowledge)
        self.assertEqual(r.knowledge.extra[value.BUSH_KEY], {"cuts": 1, "berries": 1})


class GatherByValueTest(unittest.TestCase):
    """Gather works the target worth most per step (``act.gather_targets``)."""

    def test_hurt_she_takes_nearby_berries_over_grass(self):
        w = open_ground(world(health=6))
        w.view.tiles[(6, 5)] = "grass"
        berry(w, at=(7, 5))
        self.assertEqual(gather(w)[0].cell, (7, 5))
        self.assertTrue(nearer(w, act.act({"op": "gather_gems", "count": 5}, ctx(w, kb())), (7, 5)))

    def test_at_full_health_berries_are_worth_nothing(self):
        w = open_ground(world())
        w.view.tiles[(9, 5)] = "grass"
        berry(w, at=(6, 5))
        self.assertEqual([t.cell for t in gather(w)], [(9, 5)])
        a = act.act({"op": "gather_gems", "count": 5}, ctx(w, kb()))
        self.assertNotEqual(a.intents, [{"verb": "Take", "supply_id": 20}])

    def test_she_walks_to_her_own_death_chest_nearby_for_what_it_holds(self):
        w = open_ground(world())
        w.view.tiles[(6, 5)] = "grass"
        w.death_chests[30] = DeathChest(MAP, (10, 5), died=0, contents=["bronze_sword"])  # from an earlier run
        self.assertEqual(gather(w)[0].cell, (10, 5))
        a = act.act({"op": "gather_gems", "count": 5}, ctx(w, kb()))
        self.assertTrue(nearer(w, a, (10, 5)))
        w.pos = (9, 5)
        a = act.act({"op": "gather_gems", "count": 5}, ctx(w, kb()))
        self.assertEqual(a.intents, [{"verb": "WithdrawFromChest", "chest_id": 30}])

    def test_hurt_with_a_knife_she_cuts_a_bush_for_its_berry(self):
        w = open_ground(world(health=6, armed_code="pocket_knife", armed_id=2))
        w.view.tiles[(7, 5)], w.view.tiles[(3, 5)] = "bush", "grass"
        c = ctx(w, kb())
        c.drops = value.DropWatch()
        self.assertEqual(act.gather_targets(c)[0].cell, (7, 5))
        w.pos = (6, 5)
        a = act.act({"op": "gather_gems", "count": 5}, c)
        self.assertEqual(a.intents[-1], {"verb": "Use", "target": {"kind": "block", "x": 7, "y": 5}})

    def test_at_full_health_a_bush_is_not_cut(self):
        w = open_ground(world(armed_code="pocket_knife", armed_id=2))
        w.view.tiles[(7, 5)] = "bush"
        self.assertEqual(gather(w), [])

    def test_a_surcharge_on_the_way_makes_a_near_gem_dearer_than_a_far_one(self):
        w = open_ground(world())
        for y in range(10):
            w.view.tiles[(7, y)] = "lava"  # a hazard between her and the near gem
        w.entities = [Entity("supply", 1, (8, 5), "gem"), Entity("supply", 2, (5, 1), "gem")]
        self.assertEqual(gather(w)[0].cell, (5, 1))

    def test_a_chest_with_nothing_that_fits_is_worth_nothing(self):
        w = open_ground(world(carry_capacity=1))  # the sword armed fills it
        w.death_chests[30] = DeathChest(MAP, (10, 5), died=0, contents=["bronze_sword"])
        self.assertEqual(gather(w), [])


def _end_of_queue(w, c: Ctx):
    """Where the queued Steps leave her: the cell before the path ahead."""
    path = c.mem.path
    x, y = path[0]
    return (x - 1, y) if (x - 1, y) not in path else (x, y)


class DetourOnEveryWalkTest(unittest.TestCase):
    """Any walk goes by a find worth its added steps (``act.detour``)."""

    def walk(self, op, k, *, health=6, find=None):
        w = open_ground(world(health=health, gems=40), 40, 30)
        c = ctx(w, k)
        first = act.act(op, c)
        self.assertFalse(first.over)
        here = _end_of_queue(w, c)
        w.pos = here
        (find or (lambda w: berry(w, at=(here[0] + 2, here[1] + 2))))(w)
        return w, c

    def test_a_buy_walk_takes_berries_beside_it_when_hurt(self):
        k = knowledge(sells={"raft": 30})
        k.world_code = WORLD
        w, c = self.walk({"op": "buy", "code": "raft"}, k)
        a = act.act({"op": "buy", "code": "raft"}, c)
        self.assertTrue(a.why.startswith("detour"), a.why)

    def test_a_level_walk_takes_a_chest_beside_it(self):
        k = knowledge()
        k.world_code = WORLD
        w = open_ground(world(gems=40), 40, 12)
        w.pos = (20, 10)
        c = ctx(w, k)
        op = {"op": "enter_level", "x": 5, "y": 2}
        self.assertFalse(act.act(op, c).over)
        here = _end_of_queue_toward(w, c)
        w.pos = here
        w.death_chests[30] = DeathChest(MAP, (here[0] - 2, here[1] + 2), died=0, contents=["small_potion"])
        a = act.act(op, c)
        self.assertTrue(a.why.startswith("detour"), a.why)
        self.assertIn("chest", a.why)

    def test_a_find_behind_a_wall_pays_its_steps_round_it_once(self):
        k = knowledge(sells={"raft": 30})
        k.world_code = WORLD

        def behind_a_wall(w):
            x, y = w.pos
            for dx in range(-3, 4):
                w.view.tiles[(x + dx, y + 3)] = "wall"
            berry(w, at=(x + 1, y + 5))
            w.entities.append(Entity("supply", 77, (1, 1), "small_potion", gem_price=7))  # a health point at 0.7 gems

        w, c = self.walk({"op": "buy", "code": "raft"}, k, find=behind_a_wall)
        # 6 steps added round the wall (0.6 gems) against a berry worth 0.7: counted twice, it was passed by.
        self.assertTrue(act.act({"op": "buy", "code": "raft"}, c).why.startswith("detour"))

    def test_at_full_health_the_walk_passes_the_berries(self):
        k = knowledge(sells={"raft": 30})
        k.world_code = WORLD
        w, c = self.walk({"op": "buy", "code": "raft"}, k, health=10)
        self.assertFalse(act.act({"op": "buy", "code": "raft"}, c).why.startswith("detour"))

    def test_danger_makes_the_steps_too_dear_and_the_detour_is_declined(self):
        k = knowledge(sells={"raft": 30})
        k.world_code = WORLD
        w, c = self.walk({"op": "buy", "code": "raft"}, k, health=2)
        hostile(w, 50, (w.pos[0] - 4, w.pos[1] - 3))  # can reach her, and her margin cannot pay for it
        self.assertTrue(act.urgent(c))
        self.assertFalse(act.act({"op": "buy", "code": "raft"}, c).why.startswith("detour"))

    def test_under_attack_no_detour(self):
        k = knowledge(sells={"raft": 30})
        k.world_code = WORLD
        w, c = self.walk({"op": "buy", "code": "raft"}, k)
        w.attacked_tick = w.tick
        self.assertFalse(act.act({"op": "buy", "code": "raft"}, c).why.startswith("detour"))

    def test_a_find_in_reach_on_the_walk_is_taken_once(self):
        k = knowledge(sells={"raft": 30})
        k.world_code = WORLD
        w, c = self.walk({"op": "buy", "code": "raft"}, k, find=lambda w: berry(w, at=(w.pos[0] + 1, w.pos[1] + 1)))
        a = act.act({"op": "buy", "code": "raft"}, c)
        self.assertEqual(a.intents, [{"verb": "Take", "supply_id": 20}])
        self.assertFalse(act.act({"op": "buy", "code": "raft"}, c).why.startswith("detour"), "its take was sent")


def _end_of_queue_toward(w, c: Ctx):
    """Where the queued Steps leave her on a walk west: the cell before the path ahead."""
    x, y = c.mem.path[0]
    return (x + 1, y) if (x + 1, y) not in c.mem.path else (x, y)


class HealCheapestTest(unittest.TestCase):
    """Healing takes the cheapest source, its walk counted (``goals.restore_margin``)."""

    def setUp(self):
        self.alive = goals.find("alive", goals.tree())

    def op(self, w, k):
        return goals.restore_margin(self.alive, goals.Reading(w, k))

    def test_food_near_beats_a_potion(self):
        w = world(health=4, gems=40)
        berry(w, at=(7, 5))
        self.assertEqual(self.op(w, knowledge(sells={"small_potion": 10}))["op"], "fetch_item")

    def test_a_potion_sold_only_off_this_map_never_beats_food_by_a_free_walk(self):
        w = world(health=4, gems=40)
        berry(w, at=(205, 5))
        k = knowledge()
        record_shop_cell(k, MAP + 1, (20, 20), "small_potion", 10)  # its walk cannot be priced from here
        self.assertEqual(self.op(w, k)["op"], "fetch_item")
        w.entities = []
        self.assertEqual(self.op(w, k), {"op": "buy", "code": "small_potion"}, "with nothing priced, still bought")

    def test_a_potion_sold_out_everywhere_is_no_option(self):
        w = world(health=4, gems=40)
        k = knowledge(sells={"small_potion": 10})
        with mock.patch("ladder.goals.sellers_of", return_value=[]):
            self.assertEqual(self.op(w, k), {"op": "travel", "to": "town"})

    def test_under_a_threat_her_margin_cannot_pay_for_the_walks_are_still_priced(self):
        w = world(health=3, gems=40, armed_code="bronze_sword")  # a potion covers the rat, her health alone does not
        berry(w, at=(205, 5))
        hostile(w, 50, (8, 5))
        k = knowledge(sells={"small_potion": 10, "large_potion": 40})
        self.assertTrue(readiness.step_urgent(w, k))
        self.assertEqual(self.op(w, k), {"op": "buy", "code": "small_potion"}, "the cheapest potion, not far food")

    def test_a_potion_wins_when_the_food_is_far(self):
        w = world(health=4, gems=40)
        berry(w, at=(205, 5))
        self.assertEqual(self.op(w, knowledge(sells={"small_potion": 10})), {"op": "buy", "code": "small_potion"})


if __name__ == "__main__":
    unittest.main()
