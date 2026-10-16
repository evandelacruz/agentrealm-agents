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
from tests.test_ladder_ask import Clock, HeldLLM
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

    def test_an_op_she_does_not_run_is_over(self):
        self.assertTrue(act.act({"op": "say", "text": "hi"}, ctx(open_ground(world()))).over)

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
        w.respawn_anchors = [(MAP, (1, 5))]  # the town, as a fallback refuge
        with mock.patch("ladder.reflexes.safe_goals", return_value=[(1, 5)]):
            r = reflexes.reflex(ctx(w))
        self.assertEqual(r.name, "retreat")
        self.assertTrue(nearer(w, Act(r.intents, r.why), (1, 5)))

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
