"""Lorraine's executor and reflexes (A141): each op's walk and intents, the reflexes on her one risk function, her loop."""

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from agentrealm_adk import config
from agentrealm_adk.config import CharacterConfig, Policy
from agentrealm_adk.item_table import InventorySupply
from agentrealm_adk.memory import Memory
from agentrealm_adk.executor import step_landing
from agentrealm_adk.world import Entity, Passage, chebyshev
from ladder import act, goals, readiness, reflexes
from ladder.act import OPS, Act, Ctx, Started
from ladder.ask import Planner
from ladder.play import LadderRunner
from tests.test_ladder_ask import Clock, HeldLLM, short_of_a_raft
from tests.test_ladder_goals import DOOR, MAP, geared, hostile, knowledge, world
from tests.test_starter_agent import FakeClient


def open_ground(w, width=16, height=10):
    for x in range(width):
        for y in range(height):
            w.view.tiles[(x, y)] = "dirt"
    return w


def ctx(w, kb=None, op_started=None) -> Ctx:
    return Ctx(w, Memory(), kb, Policy(), op_started or Started.now(w))


def verbs(a: Act) -> list[str]:
    return [i["verb"] for i in a.intents or []]


def first_step(a: Act) -> str:
    return next(i["direction"] for i in a.intents if i["verb"] == "Step")


def nearer(w, a: Act, to) -> bool:
    """The walk's first Step lands nearer ``to`` than she stands."""
    return chebyshev(step_landing(w.pos, first_step(a)), to) < chebyshev(w.pos, to)


class OpTest(unittest.TestCase):
    """Each op maps to its walk and its intents (``act.OPS``)."""

    def test_every_op_the_tree_may_answer_with_has_its_executor(self):
        stack, ops = [goals.tree()], set()
        while stack:
            n = stack.pop()
            ops |= set(n.ops)
            stack.extend(n.children)
        self.assertEqual(ops - set(OPS), set())

    def test_an_op_she_does_not_run_or_cannot_as_given_is_over(self):
        for op in ({"op": "say", "text": "hi"}, {"op": "enter_level"}, {"op": "enter_level", "x": "a", "y": 1},
                   {"op": "travel", "to": "point"}, {"op": "buy"}, {"op": "fight"}, {"op": "gather_gems"}):
            with self.subTest(op=op):
                self.assertTrue(act.act(op, ctx(open_ground(world()))).over)

    def test_travel_to_town_with_no_cell_goes_there(self):
        w = open_ground(world())
        w.respawn_anchors = [(MAP, (1, 5))]
        self.assertTrue(nearer(w, act.act({"op": "travel", "to": "town"}, ctx(w)), (1, 5)))

    def test_travel_walks_a_paced_step_queue_and_is_over_there(self):
        w = open_ground(world())
        c = ctx(w)
        a = act.act({"op": "travel", "to": "point", "x": 9, "y": 5}, c)
        self.assertFalse(a.over)
        self.assertTrue(nearer(w, a, (9, 5)))
        self.assertIn("Wait", verbs(a))  # paced by movement speed
        w.pos = (9, 5)
        self.assertTrue(act.act({"op": "travel", "to": "point", "x": 9, "y": 5}, c).over)

    def test_enter_level_walks_onto_the_door_and_is_over_once_through(self):
        w = open_ground(geared())
        c = ctx(w, knowledge())
        op = {"op": "enter_level", "x": DOOR[0], "y": DOOR[1]}
        self.assertTrue(nearer(w, act.act(op, c), DOOR))
        w.passage, w.map_id = Passage(MAP, DOOR, 9, w.tick + 1), 9
        self.assertTrue(act.act(op, c).over)

    def test_fetch_item_walks_to_it_takes_it_and_is_over_once_held(self):
        w = open_ground(world())
        w.entities = [Entity("supply", 7, (9, 5), "raft")]
        c = ctx(w)
        op = {"op": "fetch_item", "code": "raft", "x": 9, "y": 5}
        self.assertTrue(nearer(w, act.act(op, c), (9, 5)))
        w.pos = (8, 5)
        self.assertEqual(act.act(op, c).intents, [{"verb": "Take", "supply_id": 7}])
        w.held_supplies, w.entities = [InventorySupply(7, "raft")], []
        self.assertTrue(act.act(op, c).over)

    def test_buy_takes_off_the_shelf_in_reach(self):
        w = open_ground(world(gems=40))
        w.entities = [Entity("supply", 8, (6, 5), "raft", gem_price=30)]
        self.assertEqual(act.act({"op": "buy", "code": "raft"}, ctx(w)).intents, [{"verb": "Take", "supply_id": 8}])

    def test_buy_walks_to_a_known_seller_and_is_over_with_none_left(self):
        w = open_ground(world(gems=40), 30, 30)
        kb = knowledge(sells={"raft": 30})  # a shelf at (20, 20)
        a = act.act({"op": "buy", "code": "raft"}, ctx(w, kb))
        self.assertTrue(nearer(w, a, (20, 20)))
        self.assertTrue(act.act({"op": "buy", "code": "raft"}, ctx(w, knowledge())).over)

    def test_equip_arms_the_better_weapon_held(self):
        w = world(held_supplies=[InventorySupply(4, "adamant_sword")])
        a = act.act({"op": "equip"}, ctx(w))
        self.assertEqual(a.intents, [{"verb": "Arm", "supply_id": 4}])
        w.armed_code, w.held_supplies = "adamant_sword", []
        self.assertTrue(act.act({"op": "equip"}, ctx(w)).over)

    def test_gather_takes_a_gem_cuts_grass_and_is_over_at_its_count(self):
        w = open_ground(world())
        op = {"op": "gather_gems", "count": 3}
        w.entities = [Entity("supply", 9, (6, 5), "gem")]
        self.assertEqual(act.act(op, ctx(w)).intents, [{"verb": "Take", "supply_id": 9}])
        w.entities = []
        w.view.tiles[(6, 5)] = "grass"
        a = act.act(op, ctx(w))
        self.assertEqual(a.intents[-1], {"verb": "Use", "target": {"kind": "block", "x": 6, "y": 5}})
        w.gems = 3
        self.assertTrue(act.act(op, ctx(w)).over)

    def test_explore_area_walks_to_the_frontier_and_is_over_with_none(self):
        w = open_ground(world())  # the ground beyond x 15 and y 9 unseen
        op = {"op": "explore_area", "x": 5, "y": 5, "radius": 100}
        self.assertEqual(verbs(act.act(op, ctx(w)))[-1], "Step")
        self.assertTrue(act.act({"op": "explore_area", "x": 5, "y": 5, "radius": 1}, ctx(w)).over)

    def test_a_swing_arms_the_weapon_to_fight_with_first(self):
        w = open_ground(world(armed_code="small_potion", armed_id=3, held_supplies=[InventorySupply(4, "sword")]))
        hostile(w, 50, (6, 5))
        a = act.act({"op": "fight", "npc_id": 50}, ctx(w))
        self.assertEqual(a.intents[0], {"verb": "Arm", "supply_id": 4})
        self.assertEqual(a.intents[-1], {"verb": "Use", "target": {"kind": "npc", "npc_id": 50}})

    def test_fight_closes_then_swings_and_is_over_once_its_npc_is_gone(self):
        w = open_ground(world())
        rat = hostile(w, 50, (9, 5))
        c = ctx(w)
        op = {"op": "fight", "npc_id": 50}
        self.assertTrue(nearer(w, act.act(op, c), (9, 5)))  # its own ground is no bar to the walk
        rat.pos = (6, 5)
        a = act.act(op, c)
        self.assertEqual(a.intents[0], {"verb": "Use", "target": {"kind": "npc", "npc_id": 50}})
        w.entities = []
        self.assertTrue(act.act(op, c).over)


def short(w, *, at=(6, 5)):
    """A rat in range she cannot afford at 1 health."""
    w.health = 1
    hostile(w, 50, at)
    return w


class ReflexTest(unittest.TestCase):
    def test_sync_wakes_and_downed_holds(self):
        w = world(pos=None, asleep=True)
        self.assertEqual(reflexes.reflex(ctx(w)).intents, [{"verb": "Wait"}])
        w = world(alive=False)
        self.assertEqual((reflexes.reflex(ctx(w)).name, reflexes.reflex(ctx(w)).intents), ("downed", None))

    def test_flee_steps_away_when_the_margin_cannot_pay(self):
        w = short(open_ground(world()))
        r = reflexes.reflex(ctx(w))
        self.assertEqual(r.name, "flee")
        self.assertIn(first_step(Act(r.intents, r.why)), ("left", "up_left", "down_left"))

    def test_a_hostile_the_margin_pays_for_is_not_fled(self):
        w = open_ground(geared())  # health 10 and three large potions against one rat
        hostile(w, 50, (6, 5))
        self.assertIsNone(reflexes.reflex(ctx(w)))

    def test_the_reflexes_read_the_risk_budget_and_nothing_else(self):
        w = short(open_ground(world()))
        with mock.patch.object(readiness, "risk_budget", wraps=readiness.risk_budget) as rb:
            self.assertEqual(reflexes.reflex(ctx(w)).name, "flee")
        self.assertTrue(rb.called)
        self.assertTrue(all(call.kwargs.get("now") for call in rb.call_args_list))
        # The same world, a share that lets the goal cost everything: no reflex fires.
        with mock.patch.dict(readiness.VALUE_RISK_SHARE, {readiness.ORDINARY: 1e9}):
            self.assertIsNone(reflexes.reflex(ctx(w)))

    def test_retreat_walks_to_safe_ground_with_hostiles_near_but_none_in_range(self):
        w = short(open_ground(world()), at=(9, 5))
        w.view.safe.add((1, 5))
        r = reflexes.reflex(ctx(w))
        self.assertEqual(r.name, "retreat")
        self.assertTrue(nearer(w, Act(r.intents, r.why), (1, 5)))

    def test_a_retreat_with_no_step_yields_rather_than_hold_the_round(self):
        w = short(open_ground(world()), at=(9, 5))
        w.view.safe.add((1, 5))
        with mock.patch("ladder.reflexes.walk", return_value=Act(None, "no step")):
            self.assertIsNone(reflexes.retreat(ctx(w)))
            self.assertIsNone(reflexes.reflex(ctx(w)))  # nothing else fires: the op runs

    def test_cornered_with_nothing_to_drink_or_swing_yields(self):
        w = short(world())  # no known ground round her: no step away
        self.assertIsNone(reflexes.flee(ctx(w)))

    def test_fight_back_swings_at_a_hitter_the_margin_pays_for(self):
        w = open_ground(geared())
        rat = hostile(w, 50, (6, 5))
        w.attacked_tick, w.attacker, w.attacker_tick = w.tick, ("npc", rat.id), w.tick
        r = reflexes.reflex(ctx(w))
        self.assertEqual(r.name, "fight back")
        self.assertEqual(r.intents[-1], {"verb": "Use", "target": {"kind": "npc", "npc_id": 50}})

    def test_heal_drinks_once_health_alone_cannot_pay(self):
        w = open_ground(world(held_supplies=[InventorySupply(3, "large_potion")]))
        w.health, w.max_health = 6, 20
        hostile(w, 50, (6, 5))
        w.attacked_tick = w.tick
        b = reflexes.margin(ctx(w))
        self.assertTrue(b.ok and b.heal > 0)
        self.assertFalse(readiness.Budget(b.health, 0, b.cost, b.value).ok, "the case: only the heals pay")
        r = reflexes.reflex(ctx(w))
        self.assertEqual(r.name, "heal")
        self.assertEqual(r.intents[0], {"verb": "Arm", "supply_id": 3})
        self.assertEqual(r.intents[-1], {"verb": "Use", "target": {"kind": "self"}})


def lake(w):
    """Water east of x 7 on open ground; a raft held, the sword armed."""
    for x in range(7, 16):
        for y in range(10):
            w.view.tiles[(x, y)] = "water"
    w.held_supplies = [InventorySupply(21, "raft"), *w.held_supplies]
    return w


class LoopTest(unittest.TestCase):
    """Her own loop: a reflex holds the round and hands back to the tree; a hung call never blocks one."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.object(config, "STATE_DIR", Path(tmp.name))
        patch.start()
        self.addCleanup(patch.stop)

    def runner(self, w, planner=None) -> LadderRunner:
        cfg = CharacterConfig("T", "sandbox", Policy(), Path("t.toml"))
        r = LadderRunner(cfg, FakeClient(), 42, threading.Event(), planner or Planner.off(), knowledge=knowledge(),
                         out=lambda *_: None)
        self.addCleanup(r.trace.close)
        r.world = w
        return r

    def test_a_reflex_preempts_and_hands_back_without_setting_a_goal(self):
        w = open_ground(world())
        r = self.runner(w)
        worked = r.decide().by
        self.assertNotIn(worked, [name.__name__ for name in reflexes.REFLEXES])
        before = (r.turn.goal.key, r.op, dict(r.blocks), r.asked_for)
        short(w)
        d = r.decide()
        self.assertEqual(d.by, "flee")
        self.assertEqual((r.turn.goal.key, r.op, dict(r.blocks), r.asked_for), before)  # no goal set
        w.entities, w.sightings, w.health = [], {}, 10
        r.mem.pending_intents = None  # the flee queue ran out
        self.assertEqual(r.decide().by, worked)  # the next walk of the tree decides

    def test_a_hung_planner_call_never_blocks_a_flee(self):
        w = open_ground(world())
        fake = HeldLLM('{"op": {"op": "explore_area", "x": 1, "y": 1}}')
        self.addCleanup(fake.go.set)
        r = self.runner(w, Planner(client=fake, clock=Clock(), tokens_per_min=10**9))
        r.decide()
        self.assertIsNotNone(r.call)  # the question is out, and does not come back
        short(w)
        began = time.monotonic()
        d = r.decide()
        self.assertLess(time.monotonic() - began, 1.0)
        self.assertEqual(d.by, "flee")
        self.assertTrue(d.intents)
        self.assertFalse(r.call.back)

    def test_a_walk_onto_water_arms_the_ford_first(self):
        w = lake(open_ground(world()))
        r = self.runner(w)
        r.op = {"op": "travel", "to": "point", "x": 10, "y": 5}
        with mock.patch.object(r, "goal_op", return_value=r.op):
            d = r.decide()
        arm = d.intents.index({"verb": "Arm", "supply_id": 21})  # the water gate's arm
        pos = w.pos
        for i in d.intents[:arm]:
            if i["verb"] == "Step":
                pos = step_landing(pos, i["direction"])
                self.assertNotEqual(w.view.tiles[pos], "water", "a step onto water before the raft is armed")
        self.assertEqual(w.view.tiles[step_landing(pos, next(i["direction"] for i in d.intents[arm:] if i["verb"] == "Step"))], "water")

    def test_a_reflex_the_water_gate_cuts_to_nothing_yields(self):
        w = lake(open_ground(world(held_supplies=[InventorySupply(3, "large_potion")])))
        w.pos, w.armed_code, w.armed_id = (10, 5), "raft", 21  # afloat, the raft in hand
        w.held_supplies = [s for s in w.held_supplies if s.id != 21]
        w.health, w.max_health = 2, 20
        for p in w.neighbours(w.pos):
            w.entities.append(Entity("character", 100 + len(w.entities), p))  # boxed in: no step away
        hostile(w, 50, (12, 5))
        r = self.runner(w)
        planned = reflexes.reflex(r.ctx())  # ungated
        self.assertEqual(planned.intents[0], {"verb": "Arm", "supply_id": 3})  # a drink
        d = r.decide()
        self.assertNotEqual(d.by, planned.name)  # its Arm would strand her afloat: cut, and it yields
        self.assertNotIn({"verb": "Arm", "supply_id": 3}, d.intents or [])

    def test_a_planner_op_she_cannot_carry_out_is_refused_with_why(self):
        w = open_ground(geared())
        fake = HeldLLM('{"op": {"op": "enter_level"}}')
        fake.go.set()
        r = self.runner(w, Planner(client=fake, clock=Clock(), tokens_per_min=10**9))
        lines = []
        r.trace.out = lines.append
        r.decide()
        if r.call is not None:
            r.call.wait()
        d = r.decide()
        self.assertEqual(d.by, "enter_level")  # the tree's own op, with its door
        self.assertTrue(any("enter_level needs the door's x and y" in line for line in lines))

    def test_a_goal_whose_every_op_is_over_is_blocked_and_the_walk_moves_on(self):
        w = open_ground(geared(pos=DOOR))  # on the level's door, and it did not take her
        r = self.runner(w)
        r.decide()
        self.assertIn("level", r.blocks)
        self.assertIn("every op for it is over", r.blocks["level"].reason)

    def test_a_reply_about_a_goal_the_walk_has_left_is_let_go(self):
        w, kb = short_of_a_raft()
        fake = HeldLLM('{"op": {"op": "fetch_item", "code": "raft", "x": 1, "y": 1}}')
        self.addCleanup(fake.go.set)
        r = self.runner(w, Planner(client=fake, clock=Clock(), tokens_per_min=10**9))
        r.knowledge = kb
        r.goal_op()
        asked = r.call.node.key
        r.blocks.mark(asked, "set aside", w.tick)  # the walk moves on before the reply is back
        fake.go.set()
        r.call.wait()
        op = r.goal_op()
        self.assertNotEqual(r.turn.goal.key, asked)
        self.assertEqual(op, r.turn.op())  # the new goal's own op, not the reply about the old one

    def test_a_queue_replaced_from_where_she_may_not_stand_stops_first(self):
        w = open_ground(world())
        w.view.tiles[(7, 5)] = "stone"  # where the new Step lands if the held one already ran
        r = self.runner(w)
        r.mem.pending_intents, r.mem.pending_queue, r.mem.queue_sent_tick = [{"verb": "Step", "direction": "right"}], "q", w.tick
        w.tick += 2
        d = r.send([{"verb": "Step", "direction": "right"}], "walk", "travel")
        self.assertEqual(d.intents, [])
        self.assertTrue(r.mem.need_position)

    def test_under_attack_a_doubtful_walk_takes_the_open_step_instead(self):
        w = open_ground(world())
        w.view.tiles[(7, 5)] = "stone"
        rat = hostile(w, 50, (3, 5))
        w.attacked_tick, w.attacker, w.attacker_tick = w.tick, ("npc", rat.id), w.tick
        r = self.runner(w)
        r.mem.pending_intents, r.mem.pending_queue, r.mem.queue_sent_tick = [{"verb": "Step", "direction": "right"}], "q", w.tick
        w.tick += 2
        d = r.send([{"verb": "Step", "direction": "right"}], "walk", "travel")
        self.assertEqual(len(d.intents), 1)
        self.assertEqual(d.intents[0]["verb"], "Step")
        self.assertNotEqual(d.intents[0]["direction"], "right")

    def test_a_door_step_reads_position_and_records_the_passage(self):
        w = open_ground(world())
        w.view.tiles[(6, 5)] = "framed_door"
        r = self.runner(w)
        queue = [{"verb": "Step", "direction": "right"}]
        r.apply({"tick": 3, "queue_id": "q1"}, mock.Mock(intents=queue))
        r.apply({"tick": 4, "intent_results": [{"queue_id": "q1", "index": 0, "outcome": "applied", "tick": 4}]},
                mock.Mock(intents=None))
        self.assertTrue(r.mem.need_position)
        self.assertIsNone(r.mem.pending_intents)
        r.client = mock.Mock(position=mock.Mock(return_value={"map_id": 9, "x": 2, "y": 2, "tick": 5}))
        r.step("position")
        self.assertEqual((w.passage.from_map, w.passage.from_pos, w.passage.to_map), (MAP, (6, 5), 9))

    def test_a_step_result_moves_her_and_a_finished_queue_is_let_go(self):
        w = open_ground(world())
        r = self.runner(w)
        queue = [{"verb": "Step", "direction": "right"}]
        r.apply({"tick": 3, "queue_id": "q1"}, mock.Mock(intents=queue))
        r.apply({"tick": 4, "intent_results": [{"queue_id": "q1", "index": 0, "outcome": "applied", "tick": 4}],
                 "finished_queue": {"queue_id": "q1"}}, mock.Mock(intents=None))
        self.assertEqual(w.pos, (6, 5))
        self.assertIsNone(r.mem.pending_intents)


if __name__ == "__main__":
    unittest.main()
