"""Lorraine's one planner question (A140): the prompt, the reply, blocks, and the budget shared with Gary."""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agentrealm_adk import config, llm
from agentrealm_adk.config import CharacterConfig, Policy
from agentrealm_adk.item_table import InventorySupply
from agentrealm_adk.travel.knowledge import iter_shop_cells, record_shop_cell, save_our_drops
from agentrealm_adk.world import Entity, WorldModel
from agentrealm_agent.strategist import Strategist, StrategistConfig
from ladder import goals
from ladder import ask
from ladder.ask import CONTRACT, Planner, Reply, answer, lift_changed, node_state, question, read_reply
from ladder.goals import Blocks, find, walk
from ladder.play import LadderRunner
from tests.test_ladder_goals import geared, hostile, knowledge, world
from tests.test_starter_agent import FakeClient


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeLLM:
    """Answers each call with the next reply; an exception is raised as a failed call."""

    def __init__(self, *replies, usage=None):
        self.replies, self.usage = list(replies), usage or {}
        self.messages = []

    def complete(self, messages):
        self.messages.append(messages)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply, self.usage

    def check(self):
        pass


class HeldLLM(FakeLLM):
    """A provider whose call does not return until ``go`` is set: a slow or hung call."""

    def __init__(self, *replies, **kw):
        super().__init__(*replies, **kw)
        self.go = threading.Event()

    def complete(self, messages):
        self.go.wait()
        return super().complete(messages)


def planner(*replies, **kw) -> tuple[Planner, FakeLLM]:
    fake = FakeLLM(*replies)
    return Planner(client=fake, clock=Clock(), tokens_per_min=10**9, **kw), fake


def short_of_a_raft():
    """Health low, gems for a raft the level's entrance needs: the goal is to buy it."""
    w = world(health=2, gems=40)
    hostile(w, 50, (6, 5))
    return w, knowledge(door_need="fords", sells={"raft": 30})


class PromptTest(unittest.TestCase):
    def test_the_prompt_holds_only_the_nodes_state_and_ops(self):
        w, kb = short_of_a_raft()
        turn = walk(w, kb)
        self.assertEqual(turn.goal.key, "requires")
        system, user = question(turn.goal, turn.reading)
        self.assertTrue(system["cache"])
        self.assertIn(CONTRACT, system["content"])
        body = user["content"]
        self.assertIn("goal: hold what it requires", body)
        for k, v in node_state(turn.goal, turn.reading).items():
            self.assertIn(f"- {k}: {v}", body)
        self.assertIn("ops: buy, fetch_item, explore_area, travel", body)
        others = [n.title for n in (find(k, goals.tree()) for k in ("world", "level", "risk", "gems", "gather", "alive"))]
        for title in others:
            self.assertNotIn(title, body)
        # No stack, no stage prose: none of Gary's planner contract or progression.
        for prose in ("stack", "# Progression", "Planner contract"):
            self.assertNotIn(prose, body)
            self.assertNotIn(prose, CONTRACT)
        self.assertNotIn("# Progression", system["content"])

    def test_the_game_reference_is_the_cached_prefix(self):
        w, kb = short_of_a_raft()
        turn = walk(w, kb)
        with mock.patch("ladder.ask.reference_text", return_value="# The reference"):
            system, _ = question(turn.goal, turn.reading)
        self.assertTrue(system["content"].startswith("# The reference"))


class ReplyTest(unittest.TestCase):
    OPS = ("buy", "travel")

    def test_an_op(self):
        r = read_reply('{"op": {"op": "buy", "code": "raft"}, "why": "the door needs it"}', self.OPS)
        self.assertEqual(r, Reply(op={"op": "buy", "code": "raft"}, why="the door needs it"))

    def test_an_op_with_its_fields_beside_it(self):
        self.assertEqual(read_reply('{"op": "travel", "to": "town"}', self.OPS).op, {"op": "travel", "to": "town"})

    def test_a_cannot(self):
        r = read_reply('Thinking.\n```json\n{"cannot": "no seller known", "until": "gems"}\n```', self.OPS)
        self.assertEqual((r.op, r.cannot, r.until), (None, "no seller known", "gems"))

    def test_the_last_answer_of_several(self):
        r = read_reply('{"op": {"op": "buy", "code": "raft"}} no: {"cannot": "shut"}', self.OPS)
        self.assertEqual(r.cannot, "shut")

    def test_an_op_the_node_cannot_run_or_no_answer_is_unreadable(self):
        for raw in ('{"op": {"op": "fight"}}', '{"goals": []}', '{"cannot": ""}', "no json"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                read_reply(raw, self.OPS)


class BlockTest(unittest.TestCase):
    def test_a_cannot_blocks_the_node_and_the_walk_moves_on(self):
        w, kb = short_of_a_raft()
        blocks = Blocks()
        turn = walk(w, kb, blocks)
        self.assertIsNone(answer(turn.goal, Reply(cannot="the shop is shut"), blocks, turn.reading))
        self.assertEqual(blocks["requires"].reason, "the shop is shut")
        self.assertEqual(blocks["requires"].until, w.tick + goals.BLOCK_TICKS)
        after = walk(w, kb, blocks)
        self.assertEqual(after.goal.key, "risk")  # its sibling first, in tree order (A169)
        self.assertEqual([(n.key, b.reason) for n, b in after.skipped][0], ("requires", "the shop is shut"))

    def test_an_op_blocks_nothing(self):
        w, kb = short_of_a_raft()
        blocks = Blocks()
        turn = walk(w, kb, blocks)
        self.assertEqual(answer(turn.goal, Reply(op={"op": "travel", "to": "town"}), blocks, turn.reading), {"op": "travel", "to": "town"})
        self.assertEqual(blocks, {})

    def block(self, until: str):
        w, kb = short_of_a_raft()
        blocks = Blocks()
        turn = walk(w, kb, blocks)
        answer(turn.goal, Reply(cannot="not now", until=until), blocks, turn.reading)
        return w, kb, blocks

    def test_a_block_lifts_when_what_is_held_changes(self):
        w, kb, blocks = self.block("held")
        self.assertEqual(blocks["requires"].named, "held")
        w.held_supplies = [InventorySupply(9, "raft")]
        self.assertEqual(lift_changed(blocks, w, kb), ["requires"])
        self.assertEqual(blocks, {})

    def test_a_block_on_gems_lifts_once_that_many_are_held(self):
        w, kb, blocks = self.block("gems 70")
        w.gems = 69
        self.assertEqual(lift_changed(blocks, w, kb), [])
        w.gems = 70
        self.assertEqual(lift_changed(blocks, w, kb), ["requires"])

    def test_a_block_lifts_when_a_seller_becomes_known(self):
        w, kb, blocks = self.block("sellers")
        record_shop_cell(kb, 1, (30, 30), "small_potion", 5)
        self.assertEqual(lift_changed(blocks, w, kb), ["requires"])

    def test_a_block_never_waits_on_a_line_that_changes_every_step_or_hit(self):
        for until in ("where", "health", "readiness", "the weather", "gems", "gems lots", ""):
            with self.subTest(until=until):
                w, kb, blocks = self.block(until)
                self.assertEqual((blocks["requires"].named, blocks["requires"].until), ("", w.tick + goals.BLOCK_TICKS))
                w.pos, w.gems = (6, 6), 500  # a step within the region, and gems: neither is what the planner saw
                self.assertEqual(lift_changed(blocks, w, kb), [])  # held to its expiry
                self.assertIn("requires", blocks)

    def test_a_block_is_retried_once_the_state_the_planner_saw_changes(self):
        """A169: her region, what she carries, or her health."""
        changes = {
            "region": lambda w: setattr(w, "pos", (5 + ask.REGION_CELLS, 5)),
            "held": lambda w: setattr(w, "held_supplies", [InventorySupply(9, "raft")]),
            "health": lambda w: setattr(w, "health", w.health - 1),
        }
        for name, change in changes.items():
            with self.subTest(change=name):
                w, kb, blocks = self.block("ticks 2000")
                self.assertEqual(lift_changed(blocks, w, kb), [])
                change(w)
                self.assertEqual(lift_changed(blocks, w, kb), ["requires"])

    def test_a_block_is_retried_once_the_walk_given_up_for_it_may_be_walked_again(self):
        w, kb = short_of_a_raft()
        blocks = Blocks()
        turn = walk(w, kb, blocks)
        answer(turn.goal, Reply(cannot="the walk to the shop was given up"), blocks, turn.reading, retry_at=w.tick + 300)
        w.tick += 299
        self.assertEqual(lift_changed(blocks, w, kb), [])
        w.tick += 1
        self.assertEqual(lift_changed(blocks, w, kb), ["requires"])

    def test_a_gems_wait_already_met_is_no_wait(self):
        w, kb, blocks = self.block("gems 40")  # 40 held already
        self.assertEqual((blocks["requires"].named, blocks["requires"].until), ("", w.tick + goals.BLOCK_TICKS))
        self.assertEqual(lift_changed(blocks, w, kb), [])
        self.assertIn("requires", blocks)

    def test_a_block_on_ticks_is_a_shorter_expiry(self):
        w, kb, blocks = self.block("ticks 50")
        self.assertEqual(blocks["requires"].until, w.tick + 50)
        _, _, longer = self.block("ticks 99999")
        self.assertEqual(longer["requires"].until, w.tick + goals.BLOCK_TICKS)


class SharedPlumbingTest(unittest.TestCase):
    """The budget and backoff are Gary's, from the ADK (``agentrealm_adk.llm``)."""

    def test_both_agents_count_on_the_same_budget_and_backoff(self):
        gary = Strategist(config=StrategistConfig())
        lorraine, _ = planner()
        for agent in (gary, lorraine):
            self.assertIsInstance(agent.budget, llm.CallBudget)
            self.assertIsInstance(agent.backoff, llm.Backoff)

    def test_a_failed_call_backs_off_as_garys_does(self):
        w, kb = short_of_a_raft()
        turn = walk(w, kb)
        p, fake = planner(RuntimeError("down"), RuntimeError("down"), '{"cannot": "shut"}')
        self.assertIsNone(p.ask(turn.goal, turn.reading))
        self.assertEqual(p.backoff.retry_at - p.clock.now, llm.BACKOFF_BASE_S)
        self.assertIsNone(p.ask(turn.goal, turn.reading))  # still backing off: no call
        self.assertEqual(len(fake.messages), 1)
        p.clock.now = p.backoff.retry_at
        self.assertIsNone(p.ask(turn.goal, turn.reading))
        self.assertEqual(p.backoff.retry_at - p.clock.now, 2 * llm.BACKOFF_BASE_S)
        p.clock.now = p.backoff.retry_at
        self.assertEqual(p.ask(turn.goal, turn.reading).cannot, "shut")
        self.assertEqual(p.backoff.failures_in_a_row, 0)

    def test_an_unreadable_reply_is_a_failed_call(self):
        w, kb = short_of_a_raft()
        turn = walk(w, kb)
        p, _ = planner('{"op": {"op": "fight"}}')
        self.assertIsNone(p.ask(turn.goal, turn.reading))
        self.assertEqual(p.backoff.failures_in_a_row, 1)

    def test_calls_per_minute_bound_the_questions(self):
        w, kb = short_of_a_raft()
        turn = walk(w, kb)
        p, fake = planner(*['{"cannot": "shut"}'] * 3, calls_per_min=2)
        for _ in range(3):
            p.ask(turn.goal, turn.reading)
        self.assertEqual(len(fake.messages), 2)
        p.clock.now += llm.BUDGET_WINDOW_S
        self.assertIsNotNone(p.ask(turn.goal, turn.reading))

    def test_reported_tokens_settle_the_charge(self):
        w, kb = short_of_a_raft()
        turn = walk(w, kb)
        fake = FakeLLM('{"cannot": "shut"}', usage={"input_tokens": 900, "output_tokens": 200})
        p = Planner(client=fake, clock=Clock(), tokens_per_min=10**9)
        p.ask(turn.goal, turn.reading)
        self.assertEqual(p.budget.tokens(), 1100)

    def test_no_planner_makes_no_call(self):
        w, kb = short_of_a_raft()
        turn = walk(w, kb)
        self.assertIsNone(Planner.off().ask(turn.goal, turn.reading))
        with mock.patch.dict("os.environ", {"AGENTREALM_NO_PLANNER": "1"}):
            self.assertFalse(Planner.from_env().enabled)


def settle(r: LadderRunner):
    """A round of ``goal_op``, then more as each planner call comes back: the op once no call is out."""
    op = r.goal_op()
    while r.call is not None:
        r.call.wait()
        op = r.goal_op()
    return op


class PlayTest(unittest.TestCase):
    """``run`` walks the tree, asks the one question at its goal off the tick thread, and runs the op."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.object(config, "STATE_DIR", Path(tmp.name))
        patch.start()
        self.addCleanup(patch.stop)

    def runner(self, p: Planner, w, kb) -> LadderRunner:
        cfg = CharacterConfig("T", "sandbox", Policy(), Path("t.toml"))
        r = LadderRunner(cfg, FakeClient(), 42, threading.Event(), p, knowledge=kb, out=lambda *_: None)
        self.addCleanup(r.trace.close)
        r.world = w
        return r

    def test_our_drops_are_kept_across_runs(self):
        # A priced supply this character dropped in an earlier run is no shop (A158).
        w, kb = short_of_a_raft()
        earlier = WorldModel(character_id=42)
        earlier.note_our_drop(9)
        save_our_drops(kb, earlier)
        cfg = CharacterConfig("T", "sandbox", Policy(), Path("t.toml"))
        r = LadderRunner(cfg, FakeClient(), 42, threading.Event(), planner()[0], knowledge=kb, out=lambda *_: None)
        self.addCleanup(r.trace.close)
        self.assertTrue(r.world.is_our_drop(9))
        r.world.map_id = 1
        before = set(iter_shop_cells(kb))
        r.world.entities = [Entity("supply", 9, (33, 33), "bronze_sword", gem_price=15)]
        r.learn_shops()
        self.assertEqual(set(iter_shop_cells(kb)), before)

    def test_the_planners_op_for_the_goal_runs(self):
        w, kb = short_of_a_raft()
        p, fake = planner('{"op": {"op": "travel", "to": "town"}}')
        r = self.runner(p, w, kb)
        self.assertEqual(settle(r), {"op": "travel", "to": "town"})
        self.assertIn("goal: hold what it requires", fake.messages[0][1]["content"])
        for _ in range(5):
            settle(r)  # the same goal and state: not asked again
        self.assertEqual(len(fake.messages), 1)

    def test_the_trees_op_runs_while_the_call_is_out(self):
        w, kb = short_of_a_raft()
        fake = HeldLLM('{"op": {"op": "travel", "to": "town"}}')
        r = self.runner(Planner(client=fake, clock=Clock(), tokens_per_min=10**9), w, kb)
        self.addCleanup(fake.go.set)
        for _ in range(3):
            self.assertEqual(r.goal_op(), {"op": "buy", "code": "raft"})  # the tree's op meanwhile
        fake.go.set()
        r.call.wait()
        self.assertEqual(r.goal_op(), {"op": "travel", "to": "town"})
        self.assertEqual(len(fake.messages), 1)

    def test_a_cannot_moves_the_next_walk_on(self):
        w, kb = short_of_a_raft()
        p, fake = planner('{"cannot": "the shop is shut"}', '{"op": {"op": "travel", "to": "town"}}')
        r = self.runner(p, w, kb)
        self.assertIsNone(settle(r))
        self.assertEqual(settle(r), {"op": "travel", "to": "town"})
        self.assertIn("goal: ready for its risk", fake.messages[1][1]["content"])  # its sibling (A169)

    def test_a_cannot_is_not_asked_again_on_the_next_tick(self):
        w = geared()  # the level is the goal; under it only gathering is left (A169)
        gather = '{"op": {"op": "gather_gems", "count": 10, "x": 2, "y": 5}}'
        p, fake = planner('{"cannot": "the door will not open"}', gather)
        r = self.runner(p, w, knowledge())
        self.assertIsNone(settle(r))
        self.assertEqual(settle(r), {"op": "gather_gems", "count": 10, "x": 2, "y": 5})
        for _ in range(5):
            w.tick += 1
            settle(r)
        self.assertEqual(len(fake.messages), 2)  # the level once, then the gather worked instead
        self.assertIn("goal: beat the next level", fake.messages[0][1]["content"])
        self.assertIn("goal: gather", fake.messages[1][1]["content"])

    def test_a_new_state_asks_again(self):
        w, kb = short_of_a_raft()
        p, fake = planner('{"op": {"op": "travel", "to": "town"}}', '{"op": {"op": "buy", "code": "raft"}}')
        r = self.runner(p, w, kb)
        settle(r)
        record_shop_cell(kb, 1, (30, 30), "small_potion", 5)  # a seller known for another item
        self.assertEqual(settle(r), {"op": "buy", "code": "raft"})
        self.assertEqual(len(fake.messages), 2)

    def test_after_a_failed_ask_it_asks_again_only_past_the_backoff(self):
        w, kb = short_of_a_raft()
        p, fake = planner(RuntimeError("down"), '{"op": {"op": "travel", "to": "town"}}')
        r = self.runner(p, w, kb)
        self.assertEqual(settle(r), {"op": "buy", "code": "raft"})  # the tree's op
        settle(r)
        self.assertEqual(len(fake.messages), 1)
        p.clock.now = p.backoff.retry_at
        self.assertEqual(settle(r), {"op": "travel", "to": "town"})

    def test_a_cannot_already_met_is_asked_once_more_with_a_note(self):
        w, kb = short_of_a_raft()  # 40 gems held
        p, fake = planner('{"cannot": "short", "until": "gems 30"}', '{"op": {"op": "buy", "code": "raft"}}')
        r = self.runner(p, w, kb)
        self.assertEqual(settle(r), {"op": "buy", "code": "raft"})
        self.assertIn("note: your last answer was cannot until gems 30, which is already true", fake.messages[1][1]["content"])
        self.assertEqual(r.blocks, {})
        for _ in range(3):
            settle(r)
        self.assertEqual(len(fake.messages), 2)  # no loop

    def test_a_second_cannot_already_met_leaves_a_plain_block(self):
        w, kb = short_of_a_raft()
        p, fake = planner(*['{"cannot": "short", "until": "gems 30"}'] * 2, *['{"cannot": "hold on"}'] * 3)
        r = self.runner(p, w, kb)
        settle(r)
        self.assertEqual((r.blocks["requires"].named, r.blocks["requires"].until), ("", w.tick + goals.BLOCK_TICKS))
        for _ in range(3):
            settle(r)
        about = [m[1]["content"].splitlines()[0] for m in fake.messages]
        # once, then once more: no loop; and once all is blocked, asked about it with why (A169)
        self.assertEqual(about.count("goal: hold what it requires"), 3)
        self.assertIn("every way down is blocked", fake.messages[-1][1]["content"])
        self.assertEqual(len(about), len(set(about)) + 2)  # each goal the walk moves on to asked once

    def test_after_a_block_lifts_the_goal_is_asked_again(self):
        w, kb = short_of_a_raft()
        p, fake = planner('{"cannot": "short", "until": "gems 60"}', '{"cannot": "hold on"}',
                          '{"op": {"op": "buy", "code": "raft"}}')
        r = self.runner(p, w, kb)
        settle(r)  # requires blocked until 60 gems
        settle(r)  # stay alive asked, blocked
        w.gems = 60  # requires lifts; its watched lines are as they were
        self.assertEqual(settle(r), {"op": "buy", "code": "raft"})
        self.assertIn("goal: hold what it requires", fake.messages[2][1]["content"])

    def test_with_no_answer_the_trees_op_runs(self):
        w, kb = short_of_a_raft()
        r = self.runner(Planner.off(), w, kb)
        self.assertEqual(r.goal_op(), {"op": "buy", "code": "raft"})


if __name__ == "__main__":
    unittest.main()
