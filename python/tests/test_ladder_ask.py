"""Lorraine's one planner question (A140): the prompt, the reply, blocks, and the budget shared with Gary."""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agentrealm_adk import config, llm
from agentrealm_adk.config import CharacterConfig, Policy
from agentrealm_adk.item_table import InventorySupply
from agentrealm_agent.strategist import Strategist, StrategistConfig
from ladder import goals
from ladder.ask import CONTRACT, Planner, Reply, answer, lift_changed, node_state, question, read_reply
from ladder.goals import Blocks, find, walk
from ladder.play import LadderRunner, path_to
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
        self.assertEqual(after.goal.key, "alive")
        self.assertEqual([(n.key, b.reason) for n, b in after.skipped][0], ("requires", "the shop is shut"))

    def test_an_op_blocks_nothing(self):
        w, kb = short_of_a_raft()
        blocks = Blocks()
        turn = walk(w, kb, blocks)
        self.assertEqual(answer(turn.goal, Reply(op={"op": "travel", "to": "town"}), blocks, turn.reading), {"op": "travel", "to": "town"})
        self.assertEqual(blocks, {})

    def test_a_block_lifts_when_the_state_it_named_changes(self):
        w, kb = short_of_a_raft()
        blocks, root = Blocks(), goals.tree()
        turn = walk(w, kb, blocks, root)
        answer(turn.goal, Reply(cannot="not enough gems for the raft and a potion", until="gems"), blocks, turn.reading)
        w.pos = (6, 6)  # another part of the state changing lifts nothing
        self.assertEqual(lift_changed(blocks, w, kb, root), [])
        self.assertIn("requires", blocks)
        w.gems = 70
        self.assertEqual(lift_changed(blocks, w, kb, root), ["requires"])
        self.assertEqual(walk(w, kb, blocks, root).goal.key, "requires")

    def test_a_block_naming_no_state_watches_the_nodes_readiness(self):
        w, kb = short_of_a_raft()
        blocks, root = Blocks(), goals.tree()
        turn = walk(w, kb, blocks, root)
        answer(turn.goal, Reply(cannot="no way to the shop", until="the weather"), blocks, turn.reading)
        self.assertEqual(blocks["requires"].named, "readiness")
        w.gems = 70  # the need is the same
        self.assertEqual(lift_changed(blocks, w, kb, root), [])
        w.held_supplies = [InventorySupply(9, "raft")]  # the need is met: its readiness reads otherwise
        self.assertEqual(lift_changed(blocks, w, kb, root), ["requires"])


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


class PlayTest(unittest.TestCase):
    """``run`` walks the tree, asks the one question at its goal, and runs the op (until A141)."""

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

    def test_the_planners_op_for_the_goal_runs(self):
        w, kb = short_of_a_raft()
        p, fake = planner('{"op": {"op": "travel", "to": "town"}}')
        r = self.runner(p, w, kb)
        self.assertEqual(r.goal_op(), {"op": "travel", "to": "town"})
        self.assertIn("goal: hold what it requires", fake.messages[0][1]["content"])
        r.goal_op()  # the same goal: not asked again
        self.assertEqual(len(fake.messages), 1)

    def test_a_cannot_moves_the_next_walk_on(self):
        w, kb = short_of_a_raft()
        p, fake = planner('{"cannot": "the shop is shut"}', '{"op": {"op": "travel", "to": "town"}}')
        r = self.runner(p, w, kb)
        self.assertIsNone(r.goal_op())
        self.assertEqual(r.goal_op(), {"op": "travel", "to": "town"})
        self.assertIn("goal: stay alive", fake.messages[1][1]["content"])

    def test_with_no_answer_the_trees_op_runs(self):
        w, kb = short_of_a_raft()
        r = self.runner(Planner.off(), w, kb)
        self.assertEqual(r.goal_op(), {"op": "buy", "code": "raft"})

    def test_an_op_naming_a_cell_is_walked_to(self):
        w = geared()
        for x in range(0, 15):
            for y in range(0, 10):
                w.view.tiles[(x, y)] = "grass"
        r = self.runner(Planner.off(), w, knowledge())  # the tree's op: enter the level at its door
        d = r.decide()
        self.assertEqual(d.mode, "Goal")
        self.assertEqual(d.intents, [{"verb": "SetPosition", "x": 6, "y": 4}])
        self.assertEqual(path_to(w, (10, 4), set())[-1], (10, 4))


if __name__ == "__main__":
    unittest.main()
