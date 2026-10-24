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
from agentrealm_adk.item_table import InventorySupply
from agentrealm_adk.navigation import stuck
from agentrealm_adk.world import Entity
from ladder import act, ask
from ladder.ask import Planner, seen
from ladder.play import GIVEN_UP_NOTE, LadderRunner
from tests.test_give_ups import see_new_ground
from tests.test_ladder_act import ctx, open_ground
from tests.test_ladder_ask import Clock, FakeLLM, HeldLLM
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

    def test_a_hit_while_gathering_never_lifts_the_block_or_asks_again(self):
        """Her health is watched only where it crosses her risk budget's line (review of A169)."""
        fake = FakeLLM(CANNOT, '{"op": {"op": "gather_gems", "count": 50, "x": 2, "y": 5}}')
        r = self.runner(short_of_risk(), Planner(client=fake, clock=Clock(), tokens_per_min=10**9))
        settle(r)
        settle(r)
        asked, block = len(fake.messages), r.blocks["risk"]
        r.planner.clock.now += 60  # the call budget has room again
        for _ in range(3):
            r.world.health -= 1
            r.mem.pending_intents, r.queue_of = None, None
            settle(r)
            self.assertEqual(r.turn.goal.key, "gather")
        self.assertIs(r.blocks["risk"], block)
        self.assertEqual(len(fake.messages), asked)

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

    def test_the_blocked_goal_is_retried_once_its_walks_backoff_ends_and_its_ground_changed(self):
        """(b) The backed-off walk target's backoff expires and new ground is
        seen round it (A171): the risk is worked again; time alone never retries it."""
        r = self.blocked_by_a_given_up_walk()
        give_up = r.mem.nav_stuck.given_up[stuck.goal_key("lorraine:buy", MAP, SELLER)]
        for tick in (give_up.until - 1, give_up.until, give_up.until + 10 * stuck.BACKOFF_BASE_TICKS):
            r.world.tick = tick
            r.decide()
            self.assertEqual(r.turn.goal.key, "gather", tick)  # nothing round the seller changed
            r.mem.pending_intents, r.queue_of = None, None
        see_new_ground(r.world, give_up)  # the decision's ground check sees it
        d = r.decide()
        self.assertEqual((r.turn.goal.key, d.by), ("risk", "buy"))  # the walk to the seller goes again
        self.assertTrue(d.intents)

    def test_a_breaking_capability_gained_lifts_the_given_up_walk(self):
        """A171: a new tool that breaks a blocker may open the way, so the walk goes again."""
        r = self.blocked_by_a_given_up_walk()
        give_up = r.mem.nav_stuck.given_up[stuck.goal_key("lorraine:buy", MAP, SELLER)]
        r.world.tick = give_up.until
        r.decide()
        self.assertEqual(r.turn.goal.key, "gather")
        r.mem.pending_intents, r.queue_of = None, None
        r.world.held_supplies = [*r.world.held_supplies, InventorySupply(9, "bomb")]
        d = r.decide()
        self.assertEqual(give_up.lifted, "capabilities changed")
        self.assertEqual((r.turn.goal.key, d.by), ("risk", "buy"))

    def test_a_goal_blocked_on_a_given_up_walk_is_asked_about_once_not_every_round(self):
        """A171: while the walk stays given up the goal stays blocked, her
        moving to another region included, and the planner is asked once for another way."""
        fake = FakeLLM('{"cannot": "the seller is boxed in"}')
        r = self.blocked_by_a_given_up_walk()
        r.planner = Planner(client=fake, clock=Clock(), tokens_per_min=10**9)
        r.blocks.mark("gather", "gather: cannot", r.world.tick)  # nothing else to work
        settle(r)
        self.assertEqual(len(fake.messages), 1)
        self.assertIn(GIVEN_UP_NOTE, fake.messages[0][1]["content"])
        for i in range(5):
            r.world.tick += stuck.BACKOFF_BASE_TICKS
            r.world.pos = (r.world.pos[0] + (ask.REGION_CELLS if i % 2 == 0 else -ask.REGION_CELLS), r.world.pos[1])
            r.mem.pending_intents, r.queue_of = None, None
            r.planner.clock.now += 60
            settle(r)
            self.assertIn("risk", r.blocks)
        self.assertEqual(len(fake.messages), 1, "asked once, not every round")

    def test_the_blocked_goal_is_retried_once_the_state_it_was_blocked_in_changes(self):
        """(b) Her health, what she carries or where she stands changes: the risk is asked about again."""
        # Her region alone changes nothing while the walk given up for it holds (A171).
        for name, change in (("held", lambda w: setattr(w, "held_supplies", [InventorySupply(9, "raft")])),):
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
                self.assertEqual(r.blocks["risk"].seen, seen(r.world, r.knowledge))

    def test_with_nothing_to_work_she_always_asks_and_explores_only_alongside(self):
        """Rule 2 (Evan): no op to work always asks the planner, with why; exploring fills the wait."""
        fake = HeldLLM('{"cannot": "nothing to do"}', '{"cannot": "still nothing"}')
        self.addCleanup(fake.go.set)
        r = self.runner(short_of_risk(), Planner(client=fake, clock=Clock(), tokens_per_min=10**9))
        for key in ("risk", "gather"):
            r.blocks.mark(key, f"{key}: cannot", r.world.tick)
        d = r.decide()
        self.assertIsNone(r.turn.goal)
        self.assertIsNotNone(r.call)  # asked at once, with ground still left to explore
        self.assertEqual(r.call.node.key, "risk")
        self.assertIn("every way down is blocked; ready for its risk: risk: cannot", r.call.note)
        self.assertEqual((d.by, bool(d.intents)), ("explore_area", True))  # exploring alongside the ask
        fake.go.set()
        r.call.wait()
        r.mem.pending_intents, r.queue_of = None, None
        r.decide()  # the cannot taken: blocked again, and not asked again for this block
        for _ in range(3):
            r.mem.pending_intents, r.queue_of = None, None
            r.decide()
        self.assertEqual(len(fake.messages), 1)
        r.blocks.lift("risk")  # a new episode: the risk worked, blocked again by its op, nothing to work again
        r.blocks.mark("risk", "risk: cannot, again", r.world.tick + 1)
        r.world.tick += 1
        with mock.patch.object(act, "explore_targets", return_value=set()):  # nothing left to explore either
            r.mem.pending_intents, r.queue_of = None, None
            d = r.decide()
        self.assertIn("no op to work: ready for its risk blocked", d.why)
        if r.call is not None:
            r.call.wait()
        self.assertEqual(len(fake.messages), 2)  # every time there is nothing to work, the planner is asked

    def test_a_blocked_node_leaves_its_siblings_workable(self):
        """Blocking "hold what it requires" leaves "ready for its risk" to work."""
        r = self.runner(short_of_risk())
        r.knowledge.entrances[next(iter(r.knowledge.entrances))]["clue_needs"] = "fords"
        r.blocks.mark("requires", "no seller of a raft", r.world.tick)
        r.decide()
        self.assertEqual(r.turn.goal.key, "risk")


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

    def test_past_the_waits_cap_the_buy_is_over_its_seller_not_given_up_and_the_tree_moves_on(self):
        """The ADK's cap on the wait: the op is over, the target not backed off, and no loop on it."""
        w = short_of_risk()
        shelf = Entity("supply", 7, (12, 5), "large_potion", gem_price=20)
        w.entities.append(shelf)
        self.crowd(w, shelf.pos)
        r = self.runner(w)
        whys = []
        for _ in range(2 * stuck.OCCUPANT_WAIT_TICKS // 10):
            d = r.decide()
            whys.append(d.why)
            self.walk_out(r, d)
            if "risk" in r.blocks:
                break
        self.assertIn("stayed held by occupants", r.blocks["risk"].reason)  # the buy over at the cap
        self.assertGreater(sum("held by occupants, waiting" in x for x in whys), 10)
        self.assertFalse(stuck.backed_off(r.mem, "lorraine:buy", MAP, shelf.pos, w.tick))
        self.assertFalse(r.mem.nav_stuck.given_up)  # nothing given up: only terrain does that
        self.assertIn("risk", r.blocks)
        for _ in range(3):  # the tree works gather now, never the same buy again
            d = r.decide()
            self.assertEqual((r.turn.goal.key, d.by), ("gather", "gather_gems"))
            self.walk_out(r, d)

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


class GivenUpTest(Base):
    """A given-up walk target is not chosen again until its backoff ends, by the tree or the planner (A169)."""

    def given_up_seller(self):
        r = self.runner(short_of_risk())
        c = ctx(r.world, r.knowledge)
        act.give_up_walk(c, "lorraine:buy", stuck.goal_key("lorraine:buy", MAP, SELLER))
        r.mem = c.mem
        return r, c

    def test_a_buy_never_walks_back_to_a_seller_it_gave_up(self):
        r, c = self.given_up_seller()
        a = act.act({"op": "buy", "code": "large_potion"}, c)
        self.assertTrue(a.over)
        self.assertIn("given up (no open step toward it); retried after tick", a.why)
        self.assertEqual(a.given_up, (stuck.goal_key("lorraine:buy", MAP, SELLER),))

    def test_the_planner_is_told_the_targets_given_up_and_an_answer_back_to_one_is_not_run(self):
        fake = FakeLLM('{"op": {"op": "buy", "code": "large_potion"}}')
        r, _ = self.given_up_seller()
        r.planner = Planner(client=fake, clock=Clock(), tokens_per_min=10**9)
        settle(r)
        sent = fake.messages[0][1]["content"]
        self.assertIn("goal: ready for its risk", sent)
        self.assertIn(f"- given_up: buy to map {MAP} at {SELLER[0]},{SELLER[1]}: walk given up (no open step toward it); "
                      "retried after tick", sent)
        self.assertNotIn("unreachable", sent)
        self.assertNotEqual(r.op, {"op": "buy", "code": "large_potion"})  # the planner's buy was not run
        said = r.note + "".join(m[1]["content"] for m in fake.messages[1:])
        self.assertIn("was not run: every seller of large_potion is given up", said)  # the next ask says why
        self.assertIn("risk", r.blocks)  # the tree's own buy is over at once too: the goal blocked till the backoff ends
        self.assertEqual(r.blocks["risk"].given_up, (stuck.goal_key("lorraine:buy", MAP, SELLER),))


    def test_the_planner_is_told_why_a_target_is_given_up_and_what_lifts_it(self):
        """A171: a give-up is shown with its reason in game terms and what
        lifts it, never as a bare "unreachable"; past its backoff, only the ground."""
        r, _ = self.given_up_seller()
        rec = r.mem.nav_stuck.given_up[stuck.goal_key("lorraine:buy", MAP, SELLER)]
        rec.reasons = ("no_path",)
        r.world.tick = rec.until
        line = act.given_up_lines(r.mem, r.world.tick)[0]
        self.assertEqual(line, f"buy to map {MAP} at {SELLER[0]},{SELLER[1]}: walk given up (no path over known ground); "
                               "retried once new ground is seen near it, a block there opens, an occupant or hostile "
                               "there is gone, a capability is gained or her health rises")

    def test_a_cannot_resting_on_a_given_up_target_lapses_when_the_give_up_lifts(self):
        """A171: the planner answered cannot on a give-up it was shown; once
        new ground is seen near it the give-up lifts, so the cannot does too."""
        fake = FakeLLM('{"cannot": "the seller is given up"}')
        r, _ = self.given_up_seller()
        r.goal_since = (None, r.world.tick + 1)  # given up before the goal came: only shown to the planner
        r.planner = Planner(client=fake, clock=Clock(), tokens_per_min=10**9)
        settle(r)
        block = r.blocks["risk"]
        key = stuck.goal_key("lorraine:buy", MAP, SELLER)
        self.assertIn(key, block.shown)
        rec = r.mem.nav_stuck.given_up[key]
        r.world.tick = rec.until
        r.mem.pending_intents, r.queue_of = None, None
        settle(r)
        self.assertIs(r.blocks.get("risk"), block, "the give-up still holds: so does the cannot")
        see_new_ground(r.world, rec)
        r.mem.pending_intents, r.queue_of = None, None
        with mock.patch.object(r, "log", wraps=r.log) as log:
            r.decide()
        self.assertIn(mock.call("planner", "risk: block lifted, what it waited on came or its blocker may "
                                           "have changed"), log.call_args_list)


class RejectionTest(Base):
    def test_a_step_refused_for_an_occupant_is_kept_off_for_one_decision_only(self):
        """The cell an occupant refused her step on is avoided for that decision, not for good (A14)."""
        r = self.runner(open_ground(world(pos=(2, 5)), width=20))
        r.op = {"op": "travel", "to": "point", "x": 9, "y": 5}
        with mock.patch.object(r, "goal_op", return_value=r.op):
            d = r.decide()
            r.apply({"tick": r.world.tick, "queue_id": 1, "intent_results": [
                {"queue_id": 1, "index": 0, "tick": r.world.tick, "outcome": "rejected",
                 "rejection": {"code": "block_occupied"}}]}, d)
        self.assertIsNotNone(r.mem.nav.wait_tile)
        with mock.patch.object(r, "goal_op", return_value=r.op):
            r.decide()
        self.assertIsNone(r.mem.nav.wait_tile)  # without this a seller's cell stayed shut and the walk gave up

    def test_an_occupants_rejection_counts_toward_no_streak(self):
        w = open_ground(world(pos=(2, 5)))
        m = ctx(w).mem
        att = stuck.track(m, w, "goto", (9, 5))
        m.goal = "goto"
        for _ in range(stuck.REJECT_STREAK_LIMIT):
            stuck.on_rejection(m, "block_occupied")
        self.assertEqual(att.reject_streak, 0)
        self.assertIsNone(stuck.stuck_reason(att, w.tick))
        for _ in range(stuck.REJECT_STREAK_LIMIT):
            stuck.on_rejection(m, "not_traversable")
        self.assertEqual(stuck.stuck_reason(att, w.tick), "rejections")  # terrain still counts


if __name__ == "__main__":
    unittest.main()
