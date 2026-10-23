"""A blocked goal never leaves Lorraine idle, and occupants never end a walk (A169)."""

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agentrealm_adk import config
from agentrealm_adk.config import CharacterConfig, Policy
from agentrealm_adk.executor import step_landing
from agentrealm_adk.navigation import stuck
from agentrealm_adk.world import Entity
from ladder import act, ask
from ladder.ask import Planner, seen
from ladder.play import LadderRunner
from tests.test_ladder_act import ctx, open_ground
from tests.test_ladder_ask import Clock, FakeLLM
from tests.test_ladder_goals import MAP, knowledge, world
from tests.test_ladder_hold import no_step_to
from tests.test_starter_agent import FakeClient

SELLER = (20, 20)  # where the knowledge base knows large potions are sold, out of sight
CANNOT = '{"cannot": "the walk to the potion seller was given up", "until": "sellers"}'


def short_of_risk(**kw):
    """Fresh gear, gems for a large potion, grass to cut: "ready for its risk" is the goal."""
    w = open_ground(world(gems=40, **kw), width=40)
    w.view.tiles[(2, 5)] = "grass"
    return w


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.object(config, "STATE_DIR", Path(tmp.name))
        patch.start()
        self.addCleanup(patch.stop)

    def runner(self, w, planner=None) -> LadderRunner:
        cfg = CharacterConfig("T", "sandbox", Policy(), Path("t.toml"))
        r = LadderRunner(cfg, FakeClient(), 42, threading.Event(), planner or Planner.off(),
                         knowledge=knowledge(sells={"large_potion": 20}), out=lambda *_: None)
        self.addCleanup(r.trace.close)
        r.world = w
        return r


def settle(r: LadderRunner):
    """A round, and another for each planner call that comes back."""
    d = r.decide()
    while r.call is not None:
        r.call.wait()
        d = r.decide()
    return d


class BlockedGoalTest(Base):
    def test_a_cannot_for_risk_with_gather_targets_works_gather_not_nothing(self):
        """(a) The planner's cannot for "ready for its risk": the next ticks gather."""
        fake = FakeLLM(CANNOT, RuntimeError("the gather question fails: the tree's own op runs"))
        r = self.runner(short_of_risk(), Planner(client=fake, clock=Clock(), tokens_per_min=10**9))
        settle(r)
        for _ in range(4):
            d = settle(r)
            self.assertNotIn("no op to work", d.why)
            self.assertEqual(r.turn.goal.key, "gather")
            self.assertEqual(r.op["op"], "gather_gems")
            self.assertTrue(d.intents)
            r.mem.pending_intents, r.queue_of = None, None
            r.world.tick += 1
        self.assertIn("risk", r.blocks)
        self.assertIn("goal: ready for its risk", fake.messages[0][1]["content"])

    def test_the_tick_record_names_the_blocked_goal_and_why_and_the_goal_worked(self):
        """Rule 4: which goal is blocked and why, and which is worked instead."""
        r = self.runner(short_of_risk())
        r.blocks.mark("risk", "the walk to the potion seller was given up", r.world.tick)
        r.tick()
        r.trace.close()
        ticks = [json.loads(x) for x in Path(r.cfg.trace_path(42)).read_text().splitlines()]
        rec = next(x for x in ticks if x["call"] == "tick")
        self.assertEqual(rec["goal"], "gather")
        self.assertIn({"goal": "risk", "why": "the walk to the potion seller was given up"}, rec["blocked"])

    def blocked_by_a_given_up_walk(self) -> LadderRunner:
        """The tree's buy for the risk has no step to the seller: given up, the risk blocked, gather worked."""
        r = self.runner(short_of_risk())
        with no_step_to(SELLER):
            r.decide()
        self.assertIn("risk", r.blocks)
        self.assertEqual(r.turn.goal.key, "gather")
        self.assertTrue(stuck.backed_off(r.mem, "lorraine:buy", MAP, SELLER, r.world.tick))
        r.mem.pending_intents, r.queue_of = None, None
        return r

    def test_the_blocked_goal_is_retried_once_its_walks_backoff_ends(self):
        """(b) The backed-off walk target's backoff expires: the risk is worked again."""
        r = self.blocked_by_a_given_up_walk()
        give_up = r.mem.nav_stuck.given_up[stuck.goal_key("lorraine:buy", MAP, SELLER)]
        r.world.tick = give_up.until - 1
        r.decide()
        self.assertEqual(r.turn.goal.key, "gather")  # not before
        r.mem.pending_intents, r.queue_of = None, None
        r.world.tick = give_up.until
        d = r.decide()
        self.assertEqual((r.turn.goal.key, d.by), ("risk", "buy"))  # the walk to the seller goes again
        self.assertTrue(d.intents)

    def test_the_blocked_goal_is_retried_once_the_state_it_was_blocked_in_changes(self):
        """(b) Her health, what she carries or where she stands changes: the risk is asked about again."""
        for name, change in (("health", lambda w: setattr(w, "health", w.health - 1)),
                             ("region", lambda w: setattr(w, "pos", (w.pos[0] + ask.REGION_CELLS, w.pos[1])))):
            with self.subTest(change=name):
                r = self.blocked_by_a_given_up_walk()
                was = r.blocks["risk"]
                r.decide()
                self.assertIs(r.blocks["risk"], was)  # the same state: held
                change(r.world)
                with mock.patch.object(r, "log", wraps=r.log) as log:
                    r.decide()
                self.assertIn(mock.call("planner", "risk: block lifted, what it waited on came or its blocker may "
                                                   "have changed"), log.call_args_list)
                # Retried: the buy ran again (its walk still backed off) and blocked it from the new state.
                self.assertEqual(r.blocks["risk"].seen, seen(r.world))

    def test_with_nothing_to_work_she_explores_then_asks_with_why(self):
        """Rule 2: no goal while a block stands is never a silent hold."""
        fake = FakeLLM('{"cannot": "nothing to do"}')
        r = self.runner(short_of_risk(), Planner(client=fake, clock=Clock(), tokens_per_min=10**9))
        for key in ("risk", "gather"):
            r.blocks.mark(key, f"{key}: cannot", r.world.tick)
        d = r.decide()
        self.assertIsNone(r.turn.goal)
        self.assertEqual((d.by, bool(d.intents)), ("explore_area", True))  # the ground left to see
        self.assertIsNone(r.call)
        with mock.patch.object(act, "explore_targets", return_value=set()):
            r.mem.pending_intents, r.queue_of = None, None
            d = r.decide()
        self.assertIn("no op to work: ready for its risk blocked, risk: cannot", d.why)
        if r.call is not None:
            r.call.wait()
        self.assertEqual(len(fake.messages), 1)  # nothing left to explore: the planner is asked, told why
        self.assertIn("every way down is blocked; ready for its risk: risk: cannot", fake.messages[0][1]["content"])
        with mock.patch.object(act, "explore_targets", return_value=set()):
            for _ in range(3):
                r.mem.pending_intents, r.queue_of = None, None
                r.decide()
        self.assertEqual(len(fake.messages), 1)  # its cannot is its answer: not asked again on every round


class OccupantsTest(Base):
    def crowd(self, w, around, keep=()):
        """Town NPCs on every cell next to ``around`` but ``keep``."""
        npc = 100
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                p = (around[0] + dx, around[1] + dy)
                if (dx or dy) and p not in keep:
                    w.entities.append(Entity("npc", npc, p, "villager"))
                    npc += 1

    def walk_out(self, r, d):
        """The server runs the queue: each Step moves her, up to the first occupied cell."""
        w = r.world
        for i in d.intents or []:
            if i["verb"] != "Step":
                continue
            to = step_landing(w.pos, i["direction"])
            if to in w.occupied():
                break
            w.pos = to
        r.mem.pending_intents, r.queue_of = None, None
        w.tick += 10

    def test_a_seller_ringed_by_npcs_is_waited_on_then_reached_by_interaction_range(self):
        """(c) The last cells to the seller are occupied: she waits, never gives the walk up, and buys once one clears."""
        w = open_ground(world(gems=40, pos=(2, 5)), width=20)
        shelf = Entity("supply", 7, (12, 5), "large_potion", gem_price=20)
        w.entities.append(shelf)
        self.crowd(w, shelf.pos)
        r = self.runner(w)
        r.op = {"op": "buy", "code": "large_potion"}
        waited = 0
        with mock.patch.object(r, "goal_op", return_value=r.op):
            for _ in range(12):
                d = r.decide()
                self.assertFalse(d.stalled, d.why)
                waited += "held by occupants" in d.why
                self.walk_out(r, d)
            self.assertGreater(waited, 2)
            self.assertEqual(r.done, [])  # the buy never over on the wait
            self.assertFalse(stuck.backed_off(r.mem, "lorraine:buy", MAP, shelf.pos, w.tick))
            w.entities = [e for e in w.entities if e.pos != (11, 5)]  # one steps aside
            for _ in range(3):
                d = r.decide()
                if any(i["verb"] == "Take" for i in d.intents or []):
                    break
                self.walk_out(r, d)
        self.assertIn({"verb": "Take", "supply_id": 7}, d.intents)
        self.assertLessEqual(max(abs(w.pos[0] - 12), abs(w.pos[1] - 5)), 1)  # within reach, not on the shelf

    def test_a_walk_shut_by_terrain_is_still_given_up(self):
        """(d) Only terrain makes a target unreachable: a walled-in cell is given up for the backoff."""
        w = open_ground(world(pos=(2, 5)))
        for x in range(8, 11):
            for y in range(4, 7):
                if (x, y) != (9, 5):
                    w.view.tiles[(x, y)] = "wall"
        w.entities.append(Entity("npc", 100, (7, 5), "villager"))  # an occupant by the wall changes nothing
        c = ctx(w)
        a = None
        for _ in range(200):
            a = act.act({"op": "travel", "to": "point", "x": 9, "y": 5}, c)
            if a.over:
                break
            for i in a.intents or []:  # each Step the server runs moves her
                if i["verb"] == "Step" and (to := step_landing(w.pos, i["direction"])) not in w.occupied():
                    w.pos = to
                    stuck.on_step(c.mem, w)
            w.tick += 10
        self.assertTrue(a.over)
        self.assertTrue(stuck.backed_off(c.mem, "lorraine:travel:point", MAP, (9, 5), w.tick))


if __name__ == "__main__":
    unittest.main()
