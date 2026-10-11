"""A129: an executor that finds no step opens the block on its way before it asks, and says only what it tried.

A tree stands on the way to a target past unexplored ground, a chopping
weapon is held, and the walk has given up its destination, so it has no
step though a way round through unexplored ground is still priced. The
block on the only known way is chopped before any ask; ``tried`` names only
the searches, ``Use``s and recorded breaks; and once the give-up lapses with
the op still on top, the op walks again.
"""

from __future__ import annotations

import random
import unittest

from agentrealm_agent.config import Policy
from agentrealm_agent.directives import PARAM_DEFAULTS, default_directives
from agentrealm_agent.item_table import InventorySupply
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.break_memory import record_attempt
from agentrealm_agent.memory import Memory
from agentrealm_agent.navigation import stuck as nav_stuck
from agentrealm_agent.plan import Plan
from agentrealm_agent.states import PlayContext, dispatch
from agentrealm_agent.world import WorldModel

TREE = (5, 5)
DEST = (5, -20)  # past unexplored ground
OP = {"op": "travel", "to": "point", "x": DEST[0], "y": DEST[1]}
GOAL = "travel:point"
CHOP = {"verb": "Use", "target": {"kind": "block", "x": TREE[0], "y": TREE[1]}}


def clearing() -> WorldModel:
    """Known ground south of a row of water with one tree in it; north of the
    row nothing is seen yet, and the sides are open to unexplored ground.
    A bronze sword (it chops) is held and armed."""
    w = WorldModel(character_id=1, map_id=7, pos=(5, 7), perception=11)
    for x in range(11):
        w.view.tiles[(x, 5)] = "tree" if (x, 5) == TREE else "water"
        for y in range(6, 11):
            w.view.tiles[(x, y)] = "dirt"
    w.terrain_center, w.terrain_map = (5, 7), 7
    w.alive, w.health, w.max_health, w.lives = True, 10, 10, 5
    w.held_supplies = [InventorySupply(4, "bronze_sword")]
    w.armed_code, w.armed_id = "bronze_sword", 4
    return w


def given_up(c: PlayContext, w: WorldModel) -> nav_stuck.GiveUp:
    """The walk to the destination gave up: no step until its backoff ends."""
    return nav_stuck.remember_give_up(c.memory.nav_stuck, nav_stuck.goal_key(GOAL, w.map_id, DEST), GOAL, w.tick)


def ctx() -> PlayContext:
    plan = Plan([dict(OP)], dict(PARAM_DEFAULTS))
    return PlayContext(
        Memory(), Policy(kind="scripted"), random.Random(0), knowledge=KnowledgeBase("sandbox"), plan=plan,
        directives=default_directives(),
    )


def play(w: WorldModel, c: PlayContext, decisions: int) -> list:
    outs = []
    for _ in range(decisions):
        out = dispatch(w, c)
        outs.append(out)
        for i in out.intents or []:
            if i.get("verb") == "SetPosition":
                w.pos = (i["x"], i["y"])
                if c.memory.path[:1] == [w.pos]:
                    c.memory.path = c.memory.path[1:]
        w.tick += 10
    return outs


def asks(c: PlayContext) -> list[dict]:
    return [s for s in c.memory.strategist_signals if s["trigger"] == "no_way_on"]


class ChopBeforeAskingTest(unittest.TestCase):
    def test_the_tree_on_the_only_known_way_is_chopped_before_any_ask(self):
        w, c = clearing(), ctx()
        given_up(c, w)
        outs = play(w, c, 3)
        sent = [i for o in outs for i in o.intents or []]
        self.assertEqual(sent[0]["verb"], "SetPosition", "a step toward the tree")
        self.assertIn(CHOP, sent, [o.reason for o in outs])
        self.assertEqual(asks(c), [], "the way was being cleared: no ask")


class TriedTest(unittest.TestCase):
    def ask_from_beside_the_tree(self, c: PlayContext, w: WorldModel) -> dict:
        w.pos = (5, 6)
        given_up(c, w)
        play(w, c, 1)
        (ask,) = asks(c)
        return ask

    def test_nothing_attempted_but_the_search_is_reported_as_the_search(self):
        w, c = clearing(), ctx()
        w.held_supplies, w.armed_code, w.armed_id = [], None, None  # nothing held opens the tree
        ask = self.ask_from_beside_the_tree(c, w)
        self.assertEqual(ask["tried"], "a way round")
        self.assertIn("tree at 5,5, which nothing held opens", ask["blocked_by"])

    def test_a_recorded_break_is_reported(self):
        w, c = clearing(), ctx()
        record_attempt(c.knowledge, map_id=w.map_id, pos=TREE, capability="chop", result="failed", tick=0)
        ask = self.ask_from_beside_the_tree(c, w)
        self.assertEqual(ask["tried"], "a way round, then chop on tree at 5,5: failed")


class RetryAfterLapseTest(unittest.TestCase):
    def test_the_op_walks_again_once_its_give_up_lapses(self):
        w, c = clearing(), ctx()
        w.view.tiles[TREE] = "dirt"  # the way is open now
        rec = given_up(c, w)
        held = play(w, c, 2)
        self.assertEqual([o.intents for o in held], [None, None], [o.reason for o in held])
        self.assertEqual(len(asks(c)), 1, "asked once while backed off")
        w.tick = rec.until
        (out,) = play(w, c, 1)
        self.assertEqual(out.state, "Travel")
        self.assertEqual([i["verb"] for i in out.intents or []], ["SetPosition"], out.reason)
        self.assertIn("travel:point →", out.reason)


if __name__ == "__main__":
    unittest.main()
