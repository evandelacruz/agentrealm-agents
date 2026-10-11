"""A116: a reach-limited intent (a Take, a chest verb, a Use on a target) is
sent only from a position the server has confirmed within reach: one check
for every such intent (``reach.in_reach_from``), applied where a reflex
replaces a held walk whose Steps may have run unseen."""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agentrealm_agent import config
from agentrealm_agent.brain import Memory, choose_call
from agentrealm_agent.config import CharacterConfig, Policy
from agentrealm_agent.item_table import InventorySupply
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.reach import NEAR, in_reach_from, intent_reach
from agentrealm_agent.runner import Runner
from agentrealm_agent.states.intents import arm, take, use_block, use_npc, use_self, withdraw
from agentrealm_agent.world import Entity, WorldModel


def world() -> WorldModel:
    w = WorldModel(character_id=1, map_id=7, pos=(0, 0), perception=3)
    w.entities = [
        Entity("supply", 5, (1, 1), "torch"),
        Entity("chest", 6, (-1, 0)),
        Entity("npc", 9, (2, 0), "gnawer"),
    ]
    return w


class InReachTest(unittest.TestCase):
    def test_take_and_chest_verbs_reach_one_block(self):
        w = world()
        self.assertEqual(intent_reach(take(5), w, None), ((1, 1), NEAR))
        self.assertTrue(in_reach_from([take(5)], [(0, 0)], w, None))
        self.assertFalse(in_reach_from([take(5)], [(0, 0), (-1, 0)], w, None))
        self.assertTrue(in_reach_from([withdraw(6, [1])], [(0, 0)], w, None))
        self.assertFalse(in_reach_from([withdraw(6, [1])], [(1, 0)], w, None))

    def test_a_use_reaches_the_armed_weapons_range(self):
        w = world()
        self.assertFalse(in_reach_from([use_npc(w.entities[2])], [(0, 0)], w, None))
        w.attack_range = 2
        self.assertTrue(in_reach_from([use_npc(w.entities[2])], [(0, 0)], w, None))
        self.assertFalse(in_reach_from([use_block((3, 0))], [(0, 0)], w, None))

    def test_an_arm_in_the_queue_sets_the_use_reach(self):
        w = world()
        w.attack_range = 3  # the weapon armed now
        w.held_supplies = [InventorySupply(4, "pickaxe")]
        kb = KnowledgeBase.empty("sandbox")
        self.assertFalse(in_reach_from([arm(4), use_block((2, 0))], [(0, 0)], w, kb), "a tool reaches one block")
        kb.items["pickaxe"] = {"attack_range": 2}
        self.assertTrue(in_reach_from([arm(4), use_block((2, 0))], [(0, 0)], w, kb))

    def test_an_armed_weapon_reaches_its_published_range_until_play_learns_it(self):
        # A123: the Supplies reference's ``attacks`` reach, before get_self or a refusal says.
        w = world()
        w.held_supplies = [InventorySupply(4, "iron_whip")]
        self.assertTrue(in_reach_from([arm(4), use_npc(w.entities[2])], [(0, 0)], w, None))
        kb = KnowledgeBase.empty("sandbox")
        kb.items["iron_whip"] = {"attack_range": 1}
        self.assertFalse(in_reach_from([arm(4), use_npc(w.entities[2])], [(0, 0)], w, kb), "what play learned wins")

    def test_intents_with_no_reach_limit_always_pass(self):
        w = world()
        intents = [{"verb": "Step", "direction": "right"}, use_self(), arm(4), {"verb": "Wait"}]
        self.assertTrue(in_reach_from(intents, [(0, 0), (9, 9)], w, None))

    def test_a_target_we_cannot_place_is_not_judged(self):
        self.assertTrue(in_reach_from([take(99)], [(0, 0)], world(), None))


class HeldWalkTest(unittest.TestCase):
    """The Pickup reflex fires while a walk queue is held: its Steps may have
    run before their results reached us, so where we stand is not confirmed."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.object(config, "STATE_DIR", Path(tmp.name))
        patch.start()
        self.addCleanup(patch.stop)

    def runner(self, fake) -> Runner:
        cfg = CharacterConfig("T", "sandbox", Policy(goals=["goto"], goto=(4, 0), pickup=True), Path("t.toml"))
        r = Runner(cfg, fake, 1, threading.Event(), out=lambda _: None)
        self.addCleanup(r.trace.close)
        w = WorldModel(character_id=1, map_id=7, pos=(0, 0), perception=3)
        for y in range(2):
            for x in range(5):
                w.view.tiles[(x, y)] = "dirt"
        r.world, r.mem = w, Memory(need_self=False, need_position=False)
        return r

    def test_a_take_out_of_reach_of_where_the_walk_may_be_stops_and_rereads_position(self):
        fake = _Fake([{"tick": 10, "window_remaining_ms": 0}] * 3)
        r = self.runner(fake)
        r.tick()
        self.assertIsNotNone(r.mem.held_queue)
        # In reach of the tracked cell, not of (1, 0), where the first Step may have taken us.
        r.world.entities = [Entity("supply", 5, (-1, 1))]
        r.tick()
        self.assertNotIn(take(5), fake.sent[1] or [])
        self.assertIn("out of reach", r.mem.last_decision)
        self.assertTrue(r.mem.need_position)
        self.assertEqual(choose_call(r.world, r.mem, r.cfg.policy), "position")
        fake.position = lambda cid: {"map_id": 7, "x": 0, "y": 0}
        r.step("position")
        r.tick()
        self.assertEqual(fake.sent[2], [take(5)], "sent once the server confirmed where we stand")

    def test_a_take_in_reach_of_every_cell_the_walk_may_be_on_goes_at_once(self):
        fake = _Fake([{"tick": 10, "window_remaining_ms": 0}] * 2)
        r = self.runner(fake)
        r.tick()
        r.world.entities = [Entity("supply", 5, (0, 1))]
        r.tick()
        self.assertEqual(fake.sent[1], [take(5)])


class _Fake:
    def __init__(self, ticks):
        self.ticks, self.sent = list(ticks), []

    def tick(self, cid, intents, *, snapshot_version=None):
        self.sent.append(intents)
        r = dict(self.ticks.pop(0))
        if intents is not None:
            r["queue_id"] = f"q{len(self.sent)}"
        return r


if __name__ == "__main__":
    unittest.main()
