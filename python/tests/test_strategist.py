"""A35: the AI planner: triggers and cadence, the per-minute budget, providers, failures, and plan application."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agentrealm_agent import config
from agentrealm_agent.config import CharacterConfig, Policy
from agentrealm_agent.directives import Directives, PARAM_DEFAULTS
from agentrealm_agent.gem_yield import GemYieldTracker
from agentrealm_agent.memory import Memory, queue_signal
from agentrealm_agent.plan import Plan
from agentrealm_agent.runner import Runner
from agentrealm_agent import __main__ as cli
from agentrealm_agent.pathing import path_owned_by, plan_op_goal
from agentrealm_agent.navigation.walk import Walk
from agentrealm_agent.plan import OP_FIELDS, OP_STATE, validate_goal_op
from agentrealm_agent.strategist import (
    repeats_pinned,
    BACKOFF_BASE_S,
    BACKOFF_CAP_S,
    DEFAULT_ANTHROPIC_MODEL,
    PlannerAuthError,
    ProviderHTTPError,
    AnthropicClient,
    OpenAIChatClient,
    PlannerConfigError,
    Strategist,
    StrategistConfig,
    build_prompt,
    drain_triggers,
    default_tokens_per_min,
    estimate_tokens,
    system_prompt,
    trace_messages,
    parse_reply,
    tokens_used,
)
from agentrealm_agent.world import Entity, WorldModel
from agentrealm_agent.zone_discovery import apply_zone

WAIT_ANSWER = {"goals": [{"op": "wait", "seconds": 1, "why": "test"}], "notes": "from model"}


class FakeLLM:
    """Replies with each item of ``replies`` in turn; an Exception is raised instead."""

    def __init__(self, *replies, usage: dict | None = None) -> None:
        self.replies = list(replies)
        self.usage = {"prompt_tokens": 100, "completion_tokens": 50} if usage is None else usage
        self.calls = 0
        self.messages: list[list[dict]] = []

    def complete(self, messages):
        self.calls += 1
        self.messages.append(messages)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return (reply if isinstance(reply, str) else json.dumps(reply)), self.usage


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def fake_runner(goals: list[str] | None = None) -> SimpleNamespace:
    """The parts of a Runner the strategist reads and writes."""
    w = WorldModel(character_id=1, map_id=7, pos=(0, 0), tick=10)
    w.alive = True
    plan = Plan.from_directives(directive_goals=goals or [], directive_params=dict(PARAM_DEFAULTS))
    return SimpleNamespace(
        world=w,
        mem=Memory(),
        plan=plan or Plan([{"op": "explore_area", "x": 0, "y": 0, "radius": 9999}], dict(PARAM_DEFAULTS)),
        directives=SimpleNamespace(directives=Directives(params=dict(PARAM_DEFAULTS), goals=goals or [])),
        knowledge=None,
        tick_hz=10,
        server_tick=10,
        log=mock.MagicMock(),
        acceptance=None,
        gem_cuts=GemYieldTracker(),
    )


def make(client=None, **cfg) -> Strategist:
    return Strategist(config=StrategistConfig(provider="openai", model="m", api_key="k", **cfg), client=client, clock=Clock())


def no_planner_env(**extra: str) -> dict[str, str]:
    """The environment without any planner setting or key, plus ``extra``."""
    keys = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")  # the AGENTREALM_PLANNER_* names go with the prefix
    env = {k: v for k, v in os.environ.items() if not k.startswith("AGENTREALM_PLANNER") and k not in keys}
    env.pop("AGENTREALM_NO_PLANNER", None)
    return {**env, **extra}


def sent_triggers(runner, call: int = 0) -> list[str]:
    asks = [c.args[2]["strategist"] for c in runner.log.call_args_list if c.args[2]["strategist"]["event"] == "ask"]
    return [t["trigger"] for t in asks[call]["triggers"]]


def logged_events(runner) -> list[str]:
    return [c.args[2]["strategist"]["event"] for c in runner.log.call_args_list]


def round_trip(s: Strategist, runner) -> None:
    """One window that sends, the background answer, and the window that settles it."""
    s.on_window(runner)
    s.serve_one(timeout=0)
    s.on_window(runner)


class TriggerTest(unittest.TestCase):
    def test_drain_takes_clue_stuck_and_strategist_signals(self):
        m = Memory()
        m.clue_signals.append({"trigger": "clue", "text": "go north"})
        m.nav_stuck.stuck_signals.append({"trigger": "stuck", "goal": "goto"})
        queue_signal(m, {"trigger": "death", "tick": 1})
        self.assertEqual([t["trigger"] for t in drain_triggers(m)], ["clue", "stuck", "death"])
        self.assertEqual((m.clue_signals, m.nav_stuck.stuck_signals, m.strategist_signals), ([], [], []))

    def test_map_trigger_on_every_change_of_map_or_level(self):
        s, r = make(), fake_runner()
        s._collect(r)
        s._collect(r)
        r.world.map_id, r.world.map_level = 8, 2
        s._collect(r)
        r.world.map_id, r.world.map_level = 7, None  # died: back on the start map
        s._collect(r)
        r.world.map_id, r.world.map_level = 8, 2  # re-entering the level is an event too
        s._collect(r)
        self.assertEqual([(t["trigger"], t["map_id"]) for t in s.inbox], [("map", 7), ("map", 8), ("map", 7), ("map", 8)])
        self.assertEqual(s.inbox[1]["level"], 2)

    def test_hurt_trigger_once_per_drop_below_the_threshold(self):
        s, r = make(hurt_fraction=0.5), fake_runner()
        s._last_map = (7, None)
        r.world.health, r.world.max_health = 12, 20
        s._collect(r)
        self.assertEqual(s.inbox, [])
        r.world.health = 9
        s._collect(r)
        s._collect(r)
        self.assertEqual([t["trigger"] for t in s.inbox], ["hurt"])
        r.world.health = 15  # healed, then hurt again
        s._collect(r)
        r.world.health = 5
        s._collect(r)
        self.assertEqual([t["trigger"] for t in s.inbox], ["hurt", "hurt"])

    def test_idle_trigger_once_per_quiet_spell(self):
        s, r = make(idle_minutes=0.5), fake_runner()  # 300 ticks at 10 Hz
        s._last_map = (7, None)
        r.mem.strategist_progress_tick = 10
        r.world.tick = 309
        s._collect(r)
        self.assertEqual(s.inbox, [])
        r.world.tick = 310
        s._collect(r)
        s._collect(r)
        self.assertEqual([t["trigger"] for t in s.inbox], ["idle"])
        r.mem.strategist_progress_tick = 310  # a Step lands, then another quiet spell
        r.world.tick = 610
        s._collect(r)
        self.assertEqual([t["trigger"] for t in s.inbox], ["idle", "idle"])

    def test_idle_clock_starts_at_the_first_real_tick(self):
        """A16 Walk run 3: seeded at tick 0, the first real tick looked like 6000 idle ticks."""
        s, r = make(idle_minutes=0.5), fake_runner()
        s._last_map = (7, None)
        r.world.tick, r.server_tick = 0, None  # no tick heard yet
        s._collect(r)
        self.assertEqual(r.mem.strategist_progress_tick, -1)
        r.world.tick = r.server_tick = 3_933_174
        s._collect(r)
        self.assertEqual(s.inbox, [], "no idle trigger on the first real tick")
        self.assertEqual(r.mem.strategist_progress_tick, 3_933_174)
        r.world.tick += 300
        s._collect(r)
        self.assertEqual([t["trigger"] for t in s.inbox], ["idle"])

    def test_plan_pops_queue_goal_done_and_failed(self):
        m = Memory()
        plan = Plan([{"op": "wait", "seconds": 0, "why": "test"}, {"op": "explore_area", "x": 0, "y": 0, "radius": 1}], dict(PARAM_DEFAULTS))
        plan.finish_current("done", memory=m)
        plan.drop_current("no path", memory=m)
        self.assertEqual([(t["trigger"], t["op"]["op"]) for t in m.strategist_signals], [("goal_done", "wait"), ("goal_failed", "explore_area")])

    def test_died_event_queues_death(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(config, "STATE_DIR", Path(tmp)):
            cfg = CharacterConfig("T", "sandbox", Policy(kind="scripted", goals=[]), Path("t.toml"))
            runner = Runner(cfg, None, 1, threading.Event(), out=lambda _: None)
            self.addCleanup(runner.trace.close)
            runner.on_events([{"kind": "Died", "cause": "lava", "chest_id": 4}])
        self.assertEqual(runner.mem.strategist_signals[-1]["trigger"], "death")
        self.assertEqual(runner.mem.strategist_signals[-1]["cause"], "lava")


class CadenceTest(unittest.TestCase):
    """Replan on every event, and on the timer when nothing happens."""

    def test_first_window_with_a_position_asks_at_once(self):
        llm = FakeLLM(WAIT_ANSWER)
        s, r = make(llm), fake_runner()
        r.world.pos = None
        s.on_window(r)
        self.assertEqual(llm.calls + s._requests.qsize(), 0)  # nothing to plan from yet
        r.world.pos = (0, 0)
        s.on_window(r)
        self.assertEqual(sent_triggers(r), ["map", "timer"])

    def test_timer_replans_after_replan_s_without_events(self):
        llm = FakeLLM(WAIT_ANSWER, WAIT_ANSWER, WAIT_ANSWER)
        s, r = make(llm, replan_s=15), fake_runner()
        round_trip(s, r)
        self.assertEqual(llm.calls, 1)
        s.clock.now += 14.9
        s.on_window(r)
        s.serve_one(timeout=0)
        self.assertEqual(llm.calls, 1)
        s.clock.now += 0.1
        s.on_window(r)
        s.serve_one(timeout=0)
        self.assertEqual(llm.calls, 2)
        self.assertEqual(sent_triggers(r, 1), ["timer"])

    def test_event_replans_before_the_timer(self):
        llm = FakeLLM(WAIT_ANSWER, WAIT_ANSWER)
        s, r = make(llm, replan_s=15), fake_runner()
        round_trip(s, r)
        s.clock.now += 1
        for trigger in ("goal_done", "goal_failed", "death", "clue", "stuck"):
            queue_signal(r.mem, {"trigger": trigger})
        s.on_window(r)
        s.serve_one(timeout=0)
        self.assertEqual(llm.calls, 2)
        self.assertEqual(sent_triggers(r, 1), ["goal_done", "goal_failed", "death", "clue", "stuck"])

    def test_one_call_in_flight_at_a_time(self):
        llm = FakeLLM(WAIT_ANSWER)
        s, r = make(llm), fake_runner()
        s.on_window(r)
        queue_signal(r.mem, {"trigger": "goal_done"})
        s.on_window(r)  # the first call has not answered; the new trigger waits
        self.assertEqual(s.calls, 1)
        self.assertEqual([t["trigger"] for t in s.inbox], ["goal_done"])


class AnswerTest(unittest.TestCase):
    def test_answer_replaces_plan_and_logs_prompt(self):
        llm = FakeLLM({"goals": [{"op": "travel", "to": "town", "x": 0, "y": 0}], "params": {"curiosity": 0.4}, "notes": "n"})
        s, r = make(llm), fake_runner()
        r.mem.path = [(1, 1)]
        queue_signal(r.mem, {"trigger": "death", "tick": 3})
        round_trip(s, r)
        self.assertEqual(r.plan.current()["op"], "travel")
        self.assertEqual(r.plan.notes, "n")
        self.assertEqual(r.mem.path, [])
        self.assertEqual(logged_events(r), ["ask", "applied"])
        ask = r.log.call_args_list[0].args[2]["strategist"]
        self.assertEqual(ask["messages"], trace_messages(llm.messages[0]))
        self.assertIn("<cached prefix: ", ask["messages"][0]["content"])  # the trace skips the reference text
        self.assertIn({"trigger": "death", "tick": 3}, ask["triggers"])

    def test_prompt_has_whole_plan_every_clue_and_the_op_table(self):
        kb = SimpleNamespace(lock=threading.Lock(), clues=[{"kind": "sign", "text": f"clue {i}"} for i in range(20)], extra={})
        plan = Plan([{"op": "wait", "seconds": 0, "why": "test"}, {"op": "explore_area", "x": 3, "y": 4, "radius": 5}], dict(PARAM_DEFAULTS))
        messages = build_prompt(
            triggers=[{"trigger": "clue", "text": "torch"}],
            w=WorldModel(character_id=1, map_id=1, pos=(2, 3), tick=5),
            plan=plan,
            directives=Directives(params=dict(PARAM_DEFAULTS), instructions="be bold"),
            knowledge=kb,
        )
        user = messages[1]["content"]
        for text in ("torch", "be bold", "clue 0", "clue 19", "explore_area"):
            self.assertIn(text, user)
        for op in ("explore_area", "travel", "gather_gems", "buy", "fight_boss", "compose", "use_block", "equip", "wait"):
            self.assertIn(f"- {op}: ", messages[0]["content"])

    def test_prompt_state_marks_pinned_and_planner_ops(self):
        # Second live Walk run: the planner could not see pins and spent 17 of
        # 21 calls trying to drop the pinned target. State now marks each op.
        pinned = {"op": "travel", "to": "point", "x": 5, "y": 6, "map_id": 1}
        mine = {"op": "explore_area", "x": 3, "y": 4, "radius": 5}
        plan = Plan([pinned, mine], dict(PARAM_DEFAULTS), directive_end=1)
        messages = build_prompt(
            triggers=[],
            w=WorldModel(character_id=1, map_id=1, pos=(2, 3), tick=5),
            plan=plan,
            directives=Directives(params=dict(PARAM_DEFAULTS)),
            knowledge=None,
            given_up_travel={(1, (8, 9)): 4},
        )
        state = messages[1]["content"].split("State:\n", 1)[1].split("\n\n", 1)[0]
        self.assertIn(f"stack (top first):\n  pinned {json.dumps(pinned, sort_keys=True)}\n  planner {json.dumps(mine, sort_keys=True)}", state)
        self.assertIn('given_up_travel=["1:8,9"]', state)
        system = messages[0]["content"]
        self.assertIn("You cannot remove, reorder or replace them", system)
        self.assertIn("Plan around them", system)
        self.assertIn("only your own part of the stack", system)

    def test_prompt_state_shows_an_empty_stack(self):
        messages = build_prompt(
            triggers=[],
            w=WorldModel(character_id=1, map_id=1, pos=(2, 3), tick=5),
            plan=Plan([], dict(PARAM_DEFAULTS)),
            directives=Directives(params=dict(PARAM_DEFAULTS)),
            knowledge=None,
        )
        self.assertIn("stack: (empty)", messages[1]["content"])
        self.assertNotIn("given_up_travel", messages[1]["content"])

    def test_reply_in_a_code_fence_is_read(self):
        s, r = make(FakeLLM("```json\n" + json.dumps(WAIT_ANSWER) + "\n```")), fake_runner()
        round_trip(s, r)
        self.assertEqual(r.plan.current()["op"], "wait")

    def test_reply_without_goals_key_keeps_the_stack(self):
        s, r = make(FakeLLM({"notes": "carry on"})), fake_runner()
        round_trip(s, r)
        self.assertEqual(r.plan.current()["op"], "explore_area")
        self.assertEqual(logged_events(r), ["ask", "kept"])

    def test_a_resent_given_up_travel_never_reaches_the_stack_or_raises_a_trigger(self):
        # Review on A16: a re-send dropped after it reached the stack queued
        # goal_failed, which set off another call at once: a replan loop.
        given_up = {"op": "travel", "to": "point", "x": 5, "y": 6, "map_id": 7}
        s = make(FakeLLM({"goals": [given_up, WAIT_ANSWER["goals"][0]]}, {"goals": [given_up]}))
        r = fake_runner()
        r.mem.nav_stuck.given_up_travel[(7, (5, 6))] = 1
        round_trip(s, r)
        self.assertEqual([g["op"] for g in r.plan.goals[r.plan.index :]], ["wait"])
        self.assertEqual(r.mem.strategist_signals, [])
        s.on_window(r)
        self.assertEqual(s.inbox, [], "no new trigger, so no new call")
        self.assertIsNone(s.in_flight)

    def test_a_resent_given_up_shop_is_filtered_whatever_its_to(self):
        shop = {"op": "travel", "to": "shop", "x": 5, "y": 6}
        s, r = make(FakeLLM({"goals": [shop, WAIT_ANSWER["goals"][0]]})), fake_runner()
        r.mem.nav_stuck.given_up_travel[(7, (5, 6))] = 1
        round_trip(s, r)
        self.assertEqual([g["op"] for g in r.plan.goals[r.plan.index :]], ["wait"])

    def test_directives_goals_override_the_planner(self):
        # Directives ops stay on top; the planner's goals go below them.
        s, r = make(FakeLLM(WAIT_ANSWER)), fake_runner(goals=["gather_gems:5"])
        head = r.plan.current()
        round_trip(s, r)
        self.assertIs(r.plan.current(), head)
        self.assertEqual([g["op"] for g in r.plan.goals], ["gather_gems", "wait"])
        self.assertEqual(r.plan.directive_ops(), [head])
        self.assertEqual(logged_events(r), ["ask", "applied"])

    def test_planner_owns_the_stack_once_directives_ops_are_done(self):
        llm = FakeLLM(WAIT_ANSWER, {"goals": [{"op": "buy", "code": "torch"}]})
        s, r = make(llm, replan_s=15), fake_runner(goals=["travel:point:7:5:0"])
        round_trip(s, r)
        self.assertEqual([g["op"] for g in r.plan.goals], ["travel", "wait"])
        r.world.pos = (5, 0)
        r.plan.advance(r.world, r.mem)  # stood on the target: the travel op pops
        self.assertEqual(r.plan.directive_ops(), [])
        s.clock.now += 15
        round_trip(s, r)
        self.assertEqual(r.plan.goals, [{"op": "buy", "code": "torch"}])

    def test_snapshot_and_restore_keep_the_directives_boundary(self):
        r = fake_runner(goals=["gather_gems:5", "buy:torch"])
        saved = r.plan.snapshot()
        r.plan.goals, r.plan.directive_end, r.plan.index = [{"op": "wait", "seconds": 1, "why": "probe"}], 0, 0
        r.plan.restore(saved)
        self.assertEqual(r.plan.directive_end, 2)
        self.assertEqual([g["op"] for g in r.plan.directive_ops()], ["gather_gems", "buy"])

    def test_a_reply_repeating_the_directives_op_does_not_stack_it_twice(self):
        travel = {"op": "travel", "to": "point", "x": 5, "y": 0, "map_id": 7}
        s, r = make(FakeLLM({"goals": [{**travel, "why": "the target"}, WAIT_ANSWER["goals"][0]]})), fake_runner(goals=["travel:point:7:5:0"])
        round_trip(s, r)
        self.assertEqual([g["op"] for g in r.plan.goals], ["travel", "wait"])

    def test_a_gather_under_the_pinned_one_that_only_moves_x_y_is_unchanged(self):
        """A63 run 1: the planner re-sent its own gather_gems with a new x, y each call."""
        for count in (5, 3):
            reply = {"goals": [{"op": "gather_gems", "count": count, "x": 32, "y": 48, "why": "sample here"}]}
            s, r = make(FakeLLM(reply)), fake_runner(goals=["gather_gems:5"])
            before = r.plan
            round_trip(s, r)
            self.assertIs(r.plan, before, count)
            self.assertEqual(logged_events(r), ["ask", "unchanged"], count)

    def test_a_gather_with_a_higher_count_than_the_pinned_one_is_kept(self):
        more = {"op": "gather_gems", "count": 9, "x": 32, "y": 48}
        s, r = make(FakeLLM({"goals": [more]})), fake_runner(goals=["gather_gems:5"])
        round_trip(s, r)
        self.assertEqual(r.plan.goals, [{"op": "gather_gems", "count": 5}, more])
        self.assertEqual(logged_events(r), ["ask", "applied"])

    def test_a_new_head_clears_the_old_gather_status(self):
        s, r = make(FakeLLM({"goals": [{"op": "buy", "code": "torch"}]})), fake_runner()
        r.plan = Plan([{"op": "gather_gems", "count": 9}], dict(PARAM_DEFAULTS))
        r.mem.gather_status = "region barren"
        round_trip(s, r)
        self.assertEqual(r.mem.gather_status, "")

    def test_repeats_pinned_needs_the_same_op(self):
        pinned = [{"op": "travel", "to": "point", "x": 5, "y": 0}]
        self.assertTrue(repeats_pinned({**pinned[0], "why": "again"}, pinned))
        self.assertFalse(repeats_pinned({"op": "travel", "to": "point", "x": 6, "y": 0}, pinned))
        self.assertFalse(repeats_pinned({"op": "gather_gems", "count": 1}, pinned))


class ProgressTest(unittest.TestCase):
    """A timer reply that re-sends the stack must not restart the op on top."""

    def test_a_new_head_drops_every_walk(self):
        """The new head walks a path of its own: no walk the old stack committed to is kept (A15)."""
        llm = FakeLLM({"goals": [{"op": "travel", "to": "town", "x": 0, "y": 0}]})
        s, r = make(llm), fake_runner()
        r.mem.walks["explore"] = Walk("explore", 1, (5, 0), [(0, 0), (1, 0)])
        round_trip(s, r)
        self.assertEqual(r.mem.walks, {})

    def test_identical_reply_keeps_the_plan_and_its_progress(self):
        stack = [{"op": "wait", "seconds": 20, "why": "boss spawns"}, {"op": "buy", "code": "torch"}]
        s, r = make(FakeLLM({"goals": stack}), replan_s=15), fake_runner()
        r.plan = Plan(list(stack), dict(PARAM_DEFAULTS), tick_hz=10)
        r.plan.wait_started_tick, r.plan.stalled_since_tick = 5, 7
        r.mem.path = [(1, 1), (2, 2)]
        before = r.plan
        round_trip(s, r)
        self.assertIs(r.plan, before)
        self.assertEqual((r.plan.wait_started_tick, r.plan.stalled_since_tick), (5, 7))
        self.assertEqual(r.mem.path, [(1, 1), (2, 2)])
        self.assertEqual(logged_events(r), ["ask", "unchanged"])

    def test_identical_to_the_remaining_stack_after_a_pop(self):
        stack = [{"op": "buy", "code": "torch"}, {"op": "break_block", "x": 3, "y": 0, "capability": "burn"}]
        s, r = make(FakeLLM({"goals": stack[1:]})), fake_runner()
        r.plan = Plan(list(stack), dict(PARAM_DEFAULTS), index=1, block_before="hedge")
        before = r.plan
        round_trip(s, r)
        self.assertIs(r.plan, before)
        self.assertEqual(r.plan.block_before, "hedge")  # not re-snapshotted after the block changed

    def test_same_head_keeps_its_progress_when_the_rest_changes(self):
        head = {"op": "use_block", "x": 3, "y": 0, "code": "key"}
        s, r = make(FakeLLM({"goals": [head, {"op": "buy", "code": "rope"}]})), fake_runner()
        r.plan = Plan([head, {"op": "buy", "code": "torch"}], dict(PARAM_DEFAULTS), block_before="gate", stalled_since_tick=4)
        r.mem.path = [(1, 0)]
        round_trip(s, r)
        self.assertEqual(r.plan.goals[1]["code"], "rope")
        self.assertEqual((r.plan.block_before, r.plan.stalled_since_tick), ("gate", 4))
        self.assertEqual(r.mem.path, [(1, 0)])
        self.assertEqual(logged_events(r), ["ask", "applied"])

    def test_empty_stack_and_empty_reply_keep_the_fallback_walk(self):
        for reply in ({"goals": []}, {"goals": [{"op": "nonsense"}]}, "not json"):
            s, r = make(FakeLLM(reply)), fake_runner()
            r.plan = Plan([], dict(PARAM_DEFAULTS))
            r.mem.path, r.mem.goal = [(1, 1), (2, 2)], "explore"
            before = r.plan
            round_trip(s, r)
            self.assertIs(r.plan, before, reply)
            self.assertEqual((r.mem.path, r.mem.goal), ([(1, 1), (2, 2)], "explore"), reply)
            self.assertEqual(logged_events(r), ["ask", "unchanged"], reply)

    def test_reworded_why_does_not_restart_a_wait(self):
        s, r = make(FakeLLM({"goals": [{"op": "wait", "seconds": 20, "why": "boss is about to spawn"}, {"op": "buy", "code": "rope"}]})), fake_runner()
        r.plan = Plan([{"op": "wait", "seconds": 20, "why": "boss spawns"}], dict(PARAM_DEFAULTS), tick_hz=10)
        r.plan.wait_started_tick = 5
        round_trip(s, r)
        self.assertEqual(r.plan.wait_started_tick, 5)  # same head, new tail
        self.assertEqual(r.plan.goals[1]["code"], "rope")
        s2, r2 = make(FakeLLM({"goals": [{"op": "wait", "seconds": 20, "why": "reworded"}]})), fake_runner()
        r2.plan = Plan([{"op": "wait", "seconds": 20, "why": "boss spawns"}], dict(PARAM_DEFAULTS), tick_hz=10)
        r2.plan.wait_started_tick = 5
        before = r2.plan
        round_trip(s2, r2)
        self.assertIs(r2.plan, before)  # same stack but for the reason

    def test_reworded_head_keeps_its_path_owned(self):
        head = {"op": "travel", "to": "point", "x": 5, "y": 0, "why": "the gate"}
        reply = {"goals": [{**head, "why": "reworded"}, {"op": "buy", "code": "rope"}]}
        s, r = make(FakeLLM(reply)), fake_runner()
        r.plan = Plan([head], dict(PARAM_DEFAULTS))
        r.mem.path, r.mem.goal, r.mem.goal_op = [(1, 0), (2, 0)], plan_op_goal(head), head
        self.assertTrue(path_owned_by(r.plan.current(), r.mem))
        round_trip(s, r)
        self.assertIs(r.plan.current(), head)
        self.assertEqual(r.plan.goals[1]["code"], "rope")
        self.assertTrue(path_owned_by(r.plan.current(), r.mem))
        self.assertEqual(r.mem.path, [(1, 0), (2, 0)])

    def test_a_longer_wait_is_a_new_wait(self):
        s, r = make(FakeLLM({"goals": [{"op": "wait", "seconds": 25, "why": "boss spawns"}]})), fake_runner()
        r.plan = Plan([{"op": "wait", "seconds": 20, "why": "boss spawns"}], dict(PARAM_DEFAULTS), tick_hz=10)
        r.plan.wait_started_tick = 5
        round_trip(s, r)
        self.assertIsNone(r.plan.wait_started_tick)

    def test_a_stalled_head_still_times_out_under_timer_replans(self):
        op = {"op": "explore_area", "x": 0, "y": 0, "radius": 9999}
        s, r = make(FakeLLM(*[{"goals": [op]}] * 5), replan_s=15), fake_runner()
        r.plan.tick_hz = 10
        r.plan.note_stalled(0)
        for _ in range(5):  # 75 s of timer replans re-sending the same op
            round_trip(s, r)
            s.clock.now += 15
        self.assertTrue(r.plan.note_stalled(300))  # 30 s at 10 Hz since the first stall


class OpTableTest(unittest.TestCase):
    def test_prompt_ops_are_exactly_the_executable_ops(self):
        """The model is offered an op only if a state carries it out (or, for
        set_param, Plan.advance applies it), so it can never plan a dead op."""
        executable = {op for op, state in OP_STATE.items() if state is not None} | {"set_param"}
        self.assertEqual(set(OP_FIELDS), executable)
        self.assertNotIn("hunt", OP_FIELDS)
        self.assertIn("equip", OP_FIELDS)

    def test_prompt_lists_every_op(self):
        messages = build_prompt(
            triggers=[], w=WorldModel(character_id=1), plan=Plan([], dict(PARAM_DEFAULTS)),
            directives=Directives(params=dict(PARAM_DEFAULTS)), knowledge=None,
        )
        for op in OP_FIELDS:
            self.assertIn(f"- {op}: ", messages[0]["content"])


class SafeDefaultTest(unittest.TestCase):
    """With no valid plan the planner layer emits nothing: the stack is cleared and
    the dispatcher's safe default runs."""

    def assert_cleared(self, reply):
        s, r = make(FakeLLM(reply)), fake_runner()
        r.mem.path = [(1, 1)]
        round_trip(s, r)
        self.assertIsNone(r.plan.current())
        self.assertEqual(r.mem.path, [])
        self.assertEqual(logged_events(r), ["ask", "cleared"])
        self.assertEqual(s.inbox, [])  # answered, not retried

    def test_invalid_ops_clear_the_stack(self):
        self.assert_cleared({"goals": [{"op": "nonsense"}, {"op": "travel", "to": "moon"}]})

    def test_empty_goals_clear_the_stack(self):
        self.assert_cleared({"goals": []})

    def test_reply_that_is_not_json_clears_the_stack(self):
        self.assert_cleared("I think you should explore")

    def test_reply_that_is_not_an_object_clears_the_stack(self):
        self.assert_cleared([{"op": "wait", "seconds": 1, "why": "x"}])

    def test_long_or_unexplained_wait_is_not_a_plan(self):
        self.assert_cleared({"goals": [{"op": "wait", "seconds": 600, "why": "rest"}, {"op": "wait", "seconds": 5}]})

    def test_wait_needs_a_reason_and_a_short_bound(self):
        self.assertIsNotNone(validate_goal_op({"op": "wait", "seconds": 30, "why": "let the boss come"}))
        self.assertIsNone(validate_goal_op({"op": "wait", "seconds": 31, "why": "too long"}))
        self.assertIsNone(validate_goal_op({"op": "wait", "seconds": 5}))
        self.assertIsNone(validate_goal_op({"op": "wait", "seconds": 5, "why": ""}))

    def test_directives_goals_survive_an_invalid_reply(self):
        s, r = make(FakeLLM({"goals": []})), fake_runner(goals=["gather_gems:5"])
        round_trip(s, r)
        self.assertEqual(r.plan.current()["op"], "gather_gems")


class ParamsTest(unittest.TestCase):
    def test_params_only_reply_applies_params_and_keeps_stack(self):
        s, r = make(FakeLLM({"params": {"retreat_hits": 4, "risk": 0.9}})), fake_runner()
        round_trip(s, r)
        self.assertEqual(r.plan.current()["op"], "explore_area")
        self.assertEqual(r.plan.params["retreat_hits"], 4)  # tightened
        self.assertEqual(r.plan.params["risk"], PARAM_DEFAULTS["risk"])  # loosening dropped
        self.assertEqual(logged_events(r), ["ask", "kept"])

    def test_params_apply_while_directives_own_stack(self):
        reply = {**WAIT_ANSWER, "params": {"retreat_hits": 3}}
        s, r = make(FakeLLM(reply)), fake_runner(goals=["gather_gems:5"])
        round_trip(s, r)
        self.assertEqual(r.plan.current()["op"], "gather_gems")
        self.assertEqual(r.plan.params["retreat_hits"], 3)

    def test_params_carry_into_a_replaced_stack(self):
        reply = {**WAIT_ANSWER, "params": {"retreat_hits": 3}}
        s, r = make(FakeLLM(reply)), fake_runner()
        round_trip(s, r)
        self.assertEqual(r.plan.current()["op"], "wait")
        self.assertEqual(r.plan.params["retreat_hits"], 3)

    def test_one_key_reply_keeps_the_other_keys(self):
        s, r = make(FakeLLM({"params": {"curiosity": 0.5}})), fake_runner()
        r.plan.params.update(retreat_hits=3, fight_margin=2.0, risk=0.2)
        round_trip(s, r)
        self.assertEqual(r.plan.params["curiosity"], 0.5)
        self.assertEqual((r.plan.params["retreat_hits"], r.plan.params["fight_margin"], r.plan.params["risk"]), (3, 2.0, 0.2))

    def test_prompt_shows_current_params(self):
        llm = FakeLLM(WAIT_ANSWER)
        s, r = make(llm), fake_runner()
        r.plan.params["retreat_hits"] = 3
        s.on_window(r)
        s.serve_one(timeout=0)
        self.assertIn('"retreat_hits": 3', llm.messages[0][1]["content"])

    def test_reply_without_params_keeps_earlier_ones(self):
        s, r = make(FakeLLM(WAIT_ANSWER)), fake_runner()
        r.plan.params["retreat_hits"] = 3
        round_trip(s, r)
        self.assertEqual(r.plan.params["retreat_hits"], 3)


class RunnerParamsTest(unittest.TestCase):
    """The states read the plan's params, so a planner reply changes what the agent does."""

    def runner(self) -> Runner:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.object(config, "STATE_DIR", Path(tmp.name))
        patch.start()
        self.addCleanup(patch.stop)
        policy = Policy(kind="scripted", goals=[], on_hostile="flee", hostile=["npc"], hostile_range=2)
        cfg = CharacterConfig("T", "sandbox", policy, Path("t.toml"))
        r = Runner(cfg, None, 1, threading.Event(), out=lambda _: None)
        self.addCleanup(r.trace.close)
        # A gnawer hits for 5; at 12 health two hits cannot kill, four can.
        w = WorldModel(character_id=1, map_id=7, pos=(2, 0), perception=3)
        for y in range(3):
            for x in range(5):
                w.view.tiles[(x, y)] = "dirt"
        w.terrain_center, w.terrain_map = (2, 0), 7
        w.health, w.max_health, w.lives, w.alive = 12, 20, 6, True
        w.entities = [Entity("npc", 1, (3, 0), code="gnawer")]
        w.threat.record(("npc", "gnawer"), 5)
        apply_zone(w, 7, 0, 2, {"safe": True, "brightness": 1})
        r.world = w
        r.mem = Memory(need_self=False, need_position=False)
        return r

    def test_planner_retreat_hits_changes_the_decision(self):
        r = self.runner()
        before = r._decide(r.world, r.mem, plan=r.plan)
        self.assertFalse(before.reason.startswith("retreat"), before.reason)
        s = make(FakeLLM({"params": {"retreat_hits": 4}}))
        round_trip(s, r)
        self.assertEqual(r.plan.params["retreat_hits"], 4)
        after = r._decide(r.world, r.mem, plan=r.plan)
        self.assertTrue(after.reason.startswith("retreat"), after.reason)


class RunnerPlanTest(unittest.TestCase):
    """The planner owns the goal stack; the built-in plan is the --no-planner test mode."""

    def runner(self, strategist=None, goals: list[str] | None = None) -> Runner:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.object(config, "STATE_DIR", Path(tmp.name))
        patch.start()
        self.addCleanup(patch.stop)
        path = Path(tmp.name) / "t.toml"
        if goals is not None:
            (Path(tmp.name) / "t.directives.toml").write_text(f"goals = {json.dumps(goals)}\n")
        cfg = CharacterConfig("t", "sandbox", Policy(kind="scripted", goals=["explore", "doors"]), path)
        r = Runner(cfg, None, 1, threading.Event(), out=lambda _: None, strategist=strategist)
        self.addCleanup(r.trace.close)
        return r

    def test_planner_on_starts_with_an_empty_stack(self):
        r = self.runner(make(FakeLLM()))
        self.assertIsNone(r.plan.current())

    def test_no_planner_keeps_the_built_in_plan(self):
        r = self.runner()
        self.assertEqual([op["op"] for op in r.plan.goals], ["explore_area", "travel"])

    def test_directives_goals_own_the_stack_with_the_planner_on(self):
        r = self.runner(make(FakeLLM()), goals=["buy:torch"])
        self.assertEqual(r.plan.current(), {"op": "buy", "code": "torch"})


class FailureTest(unittest.TestCase):
    def test_failed_call_keeps_the_plan_requeues_triggers_and_retries(self):
        llm = FakeLLM(RuntimeError("openai http 500"), WAIT_ANSWER)
        s, r = make(llm), fake_runner()
        queue_signal(r.mem, {"trigger": "death", "tick": 3})
        round_trip(s, r)
        self.assertEqual(r.plan.current()["op"], "explore_area")
        self.assertEqual(logged_events(r), ["ask", "error"])  # backing off: no retry in the same window
        s.clock.now += BACKOFF_BASE_S
        s.on_window(r)
        self.assertEqual(logged_events(r), ["ask", "error", "ask"])
        s.serve_one(timeout=0)
        self.assertEqual(llm.messages[1], llm.messages[0])  # the same triggers again
        s.on_window(r)
        self.assertEqual(r.plan.current()["op"], "wait")


class BackoffTest(unittest.TestCase):
    """Live: 30 calls failed with 401, six of them in 1.5 s. Failures now back off."""

    def test_failures_back_off_exponentially_up_to_the_cap(self):
        llm = FakeLLM(*[RuntimeError("anthropic http 401")] * 12)
        s, r = make(llm, calls_per_min=1000), fake_runner()
        delays = []
        for _ in range(10):
            s.on_window(r)
            s.serve_one(timeout=0)
            s.on_window(r)  # settles the failure
            delays.append(s.retry_at - s.clock.now)
            s.on_window(r)  # still backing off: nothing sent
            self.assertEqual(llm.calls, len(delays))
            s.clock.now = s.retry_at
        self.assertEqual(delays[:4], [BACKOFF_BASE_S, 2 * BACKOFF_BASE_S, 4 * BACKOFF_BASE_S, 8 * BACKOFF_BASE_S])
        self.assertEqual(max(delays), BACKOFF_CAP_S)

    def test_a_good_reply_resets_the_backoff(self):
        s, r = make(FakeLLM(RuntimeError("down"), WAIT_ANSWER)), fake_runner()
        round_trip(s, r)
        self.assertEqual(s.failures_in_a_row, 1)
        s.clock.now += BACKOFF_BASE_S
        round_trip(s, r)
        self.assertEqual((s.failures_in_a_row, s.retry_at), (0, 0.0))


class StartupCheckTest(unittest.TestCase):
    """A refused key stops the run before play (exit 2, one line)."""

    class Refusing:
        def __init__(self, error):
            self.error = error

        def check(self):
            raise self.error

    def test_401_and_403_are_fatal(self):
        for status in (401, 403):
            s = make(self.Refusing(ProviderHTTPError(f"openai http {status}: bad key", status)))
            with self.assertRaises(PlannerAuthError) as cm:
                s.check()
            self.assertIn(f"HTTP {status}", str(cm.exception))
            self.assertNotIn("\n", str(cm.exception))

    def test_sdk_errors_with_status_code_are_fatal(self):
        err = RuntimeError("authentication_error")
        err.status_code = 401  # what the anthropic SDK's AuthenticationError carries
        with self.assertRaises(PlannerAuthError):
            make(self.Refusing(err)).check()

    def test_a_bad_model_is_fatal(self):
        # A typo'd model is a 404; any 4xx but 429 stops the run before play.
        for status in (400, 404, 422):
            s = make(self.Refusing(ProviderHTTPError(f"openai http {status}: model not found", status)))
            with self.assertRaises(PlannerConfigError) as cm:
                s.check()
            self.assertNotIsInstance(cm.exception, PlannerAuthError)
            self.assertIn("AGENTREALM_PLANNER_MODEL", str(cm.exception))

    def test_other_failures_are_left_to_the_run(self):
        make(self.Refusing(ProviderHTTPError("openai http 429: rate limited", 429))).check()
        make(self.Refusing(ProviderHTTPError("openai http 500", 500))).check()
        make(self.Refusing(RuntimeError("openai network: timeout"))).check()
        Strategist.off().check()  # test mode: nothing to check

    def test_smoke_scripts_exit_2_on_a_refused_key(self):
        from agentrealm_agent import acceptance_smoke

        env = no_planner_env(AGENTREALM_PLANNER_PROVIDER="openai", AGENTREALM_PLANNER_MODEL="m", OPENAI_API_KEY="bad")
        refused = ProviderHTTPError("openai http 401: invalid key", 401)
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(OpenAIChatClient, "check", side_effect=refused):
            with self.assertRaises(PlannerAuthError):
                acceptance_smoke.planner_for(False)
            self.assertFalse(acceptance_smoke.planner_for(True).enabled)

    def test_openai_check_reports_the_http_status(self):
        import urllib.error

        client = OpenAIChatClient("bad", "m")
        refused = urllib.error.HTTPError("u", 401, "Unauthorized", {}, io.BytesIO(b"{}"))
        with mock.patch("urllib.request.urlopen", side_effect=refused):
            with self.assertRaises(ProviderHTTPError) as cm:
                client.check()
        self.assertEqual(cm.exception.status_code, 401)


class AcceptanceCountTest(unittest.TestCase):
    """Planner errors and accepted plans reach the acceptance metrics."""

    def runner_with_metrics(self):
        from agentrealm_agent.acceptance_run import TimedRunHooks

        r = fake_runner()
        r.acceptance = TimedRunHooks()
        r.acceptance.on_planner(enabled=True)
        return r

    def test_failures_in_a_row_fail_the_run(self):
        llm = FakeLLM(*[RuntimeError("openai http 500")] * 3)
        s, r = make(llm, calls_per_min=1000), self.runner_with_metrics()
        for _ in range(3):
            round_trip(s, r)
            s.clock.now = s.retry_at
        self.assertEqual((r.acceptance.planner_errors, r.acceptance.plans_accepted), (3, 0))
        self.assertIn("3 planner errors in a row (3 in all)", r.acceptance.base_failures())
        self.assertIn("planner on but no plan accepted", r.acceptance.base_failures())
        self.assertEqual(r.acceptance.planner_summary_line(), "planner: 0 plan(s) accepted, 3 error(s)")

    def test_a_transient_error_the_backoff_recovers_from_passes(self):
        llm = FakeLLM(RuntimeError("anthropic http 529: overloaded"), WAIT_ANSWER, RuntimeError("timeout"), WAIT_ANSWER)
        s, r = make(llm, calls_per_min=1000, replan_s=15), self.runner_with_metrics()
        for _ in range(4):
            round_trip(s, r)
            s.clock.now = max(s.retry_at, s.clock.now + 15)
        self.assertEqual((r.acceptance.planner_errors, r.acceptance.plans_accepted), (2, 2))
        self.assertEqual(r.acceptance.base_failures(), [])
        self.assertEqual(r.acceptance.planner_summary_line(), "planner: 2 plan(s) accepted, 2 error(s)")

    def test_a_refused_key_mid_run_fails_at_once(self):
        llm = FakeLLM(WAIT_ANSWER, ProviderHTTPError("openai http 401: invalid key", 401))
        s, r = make(llm, calls_per_min=1000, replan_s=15), self.runner_with_metrics()
        round_trip(s, r)
        s.clock.now += 15
        round_trip(s, r)
        self.assertEqual(r.acceptance.base_failures(), ["1 planner auth error(s) (401/403)"])

    def test_accepted_plans_count_and_pass(self):
        s, r = make(FakeLLM(WAIT_ANSWER, {"goals": [{"op": "buy", "code": "torch"}]}), replan_s=15), self.runner_with_metrics()
        round_trip(s, r)
        s.clock.now += 15
        round_trip(s, r)
        self.assertEqual((r.acceptance.plans_accepted, r.acceptance.planner_errors), (2, 0))
        self.assertEqual(r.acceptance.base_failures(), [])

    def test_only_a_reply_with_a_valid_op_or_an_empty_stack_is_accepted(self):
        replies = [
            {"notes": "thinking"},  # notes only: neither
            {"params": {"retreat_hits": 3}},  # params only: neither
            {"goals": [{"op": "fly", "why": "no such op"}]},  # every op invalid: an error
            {"goals": []},  # an explicit empty stack: accepted
            WAIT_ANSWER,  # a valid op: accepted
        ]
        s, r = make(FakeLLM(*replies), replan_s=15), self.runner_with_metrics()
        for _ in replies:
            round_trip(s, r)
            s.clock.now += 15
        self.assertEqual((r.acceptance.plans_accepted, r.acceptance.planner_errors), (2, 1))

    def test_invalid_ops_count_toward_the_streak(self):
        bad = {"goals": [{"op": "fly"}]}
        s, r = make(FakeLLM(bad, bad, bad), replan_s=15), self.runner_with_metrics()
        for _ in range(3):
            round_trip(s, r)
            s.clock.now += 15
        self.assertIn("3 planner errors in a row (3 in all)", r.acceptance.base_failures())

    def test_a_reply_that_is_not_a_plan_is_an_error(self):
        s, r = make(FakeLLM("not json at all")), self.runner_with_metrics()
        round_trip(s, r)
        self.assertEqual((r.acceptance.plans_accepted, r.acceptance.planner_errors), (0, 1))

    def test_planner_off_is_not_judged(self):
        from agentrealm_agent.acceptance_run import TimedRunHooks

        m = TimedRunHooks()
        m.on_planner(enabled=False)
        self.assertEqual(m.base_failures(), [])
        self.assertEqual(m.planner_summary_line(), "planner: off (--no-planner)")


class BudgetTest(unittest.TestCase):
    """Calls and tokens per minute of play: a long session never runs dry."""

    def test_calls_per_minute_count_failed_calls_and_refill(self):
        llm = FakeLLM(*[RuntimeError("down")] * 5)
        s, r = make(llm, calls_per_min=2), fake_runner()
        for _ in range(5):
            s.on_window(r)
            s.serve_one(timeout=0)
            s.clock.now += 5  # past each backoff (2 s, then 4 s), well inside the minute
        self.assertEqual(llm.calls, 2)
        self.assertEqual(s.limit_reached(), "calls_per_min")
        s.clock.now += 60
        self.assertEqual(s.limit_reached(), "")
        s.on_window(r)
        s.serve_one(timeout=0)
        self.assertEqual(llm.calls, 3)

    def test_tokens_per_minute_use_reported_usage(self):
        llm = FakeLLM(WAIT_ANSWER, WAIT_ANSWER, usage={"input_tokens": 900, "output_tokens": 200})
        s, r = make(llm, tokens_per_min=1000), fake_runner()
        round_trip(s, r)
        self.assertEqual(sum(t for _, t in s.spent), 1100)
        queue_signal(r.mem, {"trigger": "death"})
        s.on_window(r)
        self.assertEqual(llm.calls, 1)
        self.assertEqual(s.limit_reached(), "tokens_per_min")
        s.clock.now += 60  # a minute of play later the death trigger goes out
        s.on_window(r)
        s.serve_one(timeout=0)
        self.assertEqual(llm.calls, 2)
        self.assertIn("death", sent_triggers(r, 1))

    def test_failed_call_charges_estimated_prompt_tokens(self):
        s, r = make(FakeLLM(RuntimeError("timeout")), calls_per_min=1), fake_runner()
        round_trip(s, r)
        self.assertGreater(s.spent[0][1], 100)  # the system prompt alone is over 400 characters

    def test_a_long_session_keeps_asking(self):
        llm = FakeLLM(*[WAIT_ANSWER] * 480)
        s, r = make(llm, replan_s=15), fake_runner()
        for _ in range(4 * 60 * 2):  # two hours, a window every 15 s of play
            round_trip(s, r)
            s.clock.now += 15
        self.assertEqual(llm.calls, 480)  # the timer every 15 s, all the way to the end

    def test_usage_names_from_both_providers(self):
        self.assertEqual(tokens_used({"prompt_tokens": 3, "completion_tokens": 4}), 7)
        self.assertEqual(tokens_used({"input_tokens": 3, "output_tokens": 4}), 7)
        self.assertIsNone(tokens_used({}))


class FakeAnthropicSDK:
    """Stands in for the ``anthropic`` package: records each request, answers ``reply``."""

    def __init__(self, reply: str = '{"goals": []}', stop_reason: str = "end_turn", **usage: int) -> None:
        self.requests: list[dict] = []
        self.reply, self.stop_reason = reply, stop_reason
        self.usage = {"input_tokens": 30, "output_tokens": 12, **usage}
        sdk = self

        class Anthropic:
            def __init__(self, api_key, max_retries):
                sdk.api_key = api_key
                self.beta = SimpleNamespace(messages=SimpleNamespace(create=sdk.create))

        self.module = SimpleNamespace(Anthropic=Anthropic)

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(
            stop_reason=self.stop_reason,
            content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=self.reply)],
            usage=SimpleNamespace(**self.usage),
        )


class ProviderTest(unittest.TestCase):
    def from_env(self, sdk: FakeAnthropicSDK | None = None, **env: str) -> Strategist:
        modules = {"anthropic": sdk.module} if sdk is not None else {"anthropic": None}  # None: import fails
        with mock.patch.dict(os.environ, no_planner_env(**env), clear=True), mock.patch.dict(sys.modules, modules):
            return Strategist.from_env()

    def test_anthropic_by_default_with_the_latest_sonnet(self):
        s = self.from_env(FakeAnthropicSDK(), ANTHROPIC_API_KEY="a-key")
        self.assertIsInstance(s.client, AnthropicClient)
        self.assertEqual((s.config.provider, s.config.model), ("anthropic", DEFAULT_ANTHROPIC_MODEL))
        self.assertEqual(DEFAULT_ANTHROPIC_MODEL, "claude-sonnet-5-5")

    def test_openai_when_only_its_key_is_set(self):
        s = self.from_env(OPENAI_API_KEY="o-key", AGENTREALM_PLANNER_MODEL="some-model")
        self.assertIsInstance(s.client, OpenAIChatClient)
        self.assertEqual((s.client.api_key, s.client.model), ("o-key", "some-model"))

    def test_provider_and_model_from_env(self):
        env = {"AGENTREALM_PLANNER_PROVIDER": "openai", "AGENTREALM_PLANNER_MODEL": "m2", "ANTHROPIC_API_KEY": "a", "OPENAI_API_KEY": "o"}
        self.assertIsInstance(self.from_env(**env).client, OpenAIChatClient)
        env["AGENTREALM_PLANNER_PROVIDER"] = "anthropic"
        s = self.from_env(FakeAnthropicSDK(), **env)
        self.assertIsInstance(s.client, AnthropicClient)
        self.assertEqual(s.client.model, "m2")

    def test_key_lookup_order(self):
        both = {"AGENTREALM_PLANNER_ANTHROPIC_KEY": "own", "ANTHROPIC_API_KEY": "standard"}
        self.assertEqual(self.from_env(FakeAnthropicSDK(), **both).config.api_key, "own")
        self.assertEqual(self.from_env(FakeAnthropicSDK(), ANTHROPIC_API_KEY="standard").config.api_key, "standard")
        self.assertEqual(self.from_env(FakeAnthropicSDK(), AGENTREALM_PLANNER_ANTHROPIC_KEY="own").config.api_key, "own")
        model = {"AGENTREALM_PLANNER_MODEL": "m"}
        both = {"AGENTREALM_PLANNER_OPENAI_KEY": "own", "OPENAI_API_KEY": "standard", **model}
        self.assertEqual(self.from_env(**both).client.api_key, "own")
        self.assertEqual(self.from_env(OPENAI_API_KEY="standard", **model).client.api_key, "standard")
        # The planner's own OpenAI name alone also picks OpenAI as the provider.
        self.assertIsInstance(self.from_env(AGENTREALM_PLANNER_OPENAI_KEY="own", **model).client, OpenAIChatClient)

    def test_env_budget_and_cadence(self):
        s = self.from_env(
            FakeAnthropicSDK(),
            ANTHROPIC_API_KEY="a",
            AGENTREALM_PLANNER_REPLAN_S="12",
            AGENTREALM_PLANNER_CALLS_PER_MIN="3",
            AGENTREALM_PLANNER_TOKENS_PER_MIN="5000",
            AGENTREALM_PLANNER_HURT_FRACTION="0.4",
            AGENTREALM_PLANNER_IDLE_MINUTES="1",
        )
        c = s.config
        self.assertEqual((c.replan_s, c.calls_per_min, c.tokens_per_min, c.hurt_fraction, c.idle_minutes), (12, 3, 5000, 0.4, 1))

    def test_anthropic_request_and_reply(self):
        sdk = FakeAnthropicSDK(reply=json.dumps(WAIT_ANSWER))
        s = self.from_env(sdk, ANTHROPIC_API_KEY="a-key")
        text, usage = s.client.complete([{"role": "system", "content": "sys"}, {"role": "user", "content": "state"}])
        self.assertEqual(json.loads(text), WAIT_ANSWER)  # the thinking block is skipped
        self.assertEqual(tokens_used(usage), 42)
        req = sdk.requests[0]
        self.assertEqual(sdk.api_key, "a-key")
        self.assertEqual(
            (req["model"], req["system"], req["messages"]),
            ("claude-sonnet-5-5", [{"type": "text", "text": "sys"}], [{"role": "user", "content": "state"}]),
        )
        self.assertNotIn("temperature", req)
        self.assertEqual(req["extra_body"]["fallbacks"], "default")

    def test_anthropic_caches_the_marked_system_block(self):
        sdk = FakeAnthropicSDK(reply=json.dumps(WAIT_ANSWER))
        s = self.from_env(sdk, ANTHROPIC_API_KEY="a")
        s.client.complete([{"role": "system", "content": "ref", "cache": True}, {"role": "user", "content": "state"}])
        req = sdk.requests[0]
        self.assertEqual(req["system"], [{"type": "text", "text": "ref", "cache_control": {"type": "ephemeral"}}])
        self.assertEqual(req["messages"], [{"role": "user", "content": "state"}])

    def test_anthropic_cache_miss_counts_the_write_and_a_hit_skips_the_read(self):
        miss = FakeAnthropicSDK(reply=json.dumps(WAIT_ANSWER), cache_creation_input_tokens=60_000)
        _, usage = self.from_env(miss, ANTHROPIC_API_KEY="a").client.complete([{"role": "user", "content": "s"}])
        self.assertEqual(tokens_used(usage), 60_042)
        hit = FakeAnthropicSDK(reply=json.dumps(WAIT_ANSWER), cache_read_input_tokens=60_000)
        _, usage = self.from_env(hit, ANTHROPIC_API_KEY="a").client.complete([{"role": "user", "content": "s"}])
        self.assertEqual(tokens_used(usage), 42)

    def openai_usage(self, usage: dict) -> dict:
        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def urlopen(req, timeout):
            body = {"choices": [{"message": {"content": json.dumps(WAIT_ANSWER)}}], "usage": usage}
            return Resp(json.dumps(body).encode())

        with mock.patch("urllib.request.urlopen", urlopen):
            return OpenAIChatClient("k", "m").complete([{"role": "user", "content": "s"}])[1]

    def test_openai_cache_miss_counts_the_whole_prompt(self):
        miss = {"prompt_tokens": 60_000, "completion_tokens": 10, "prompt_tokens_details": {"cached_tokens": 0}}
        self.assertEqual(tokens_used(self.openai_usage(miss)), 60_010)
        self.assertEqual(tokens_used(self.openai_usage({"prompt_tokens": 60_000, "completion_tokens": 10})), 60_010)

    def test_default_budget_fits_one_prefix_write(self):
        prefix = estimate_tokens([{"role": "system", "content": system_prompt()}])
        self.assertGreaterEqual(default_tokens_per_min(), 1.5 * prefix)
        s = Strategist(config=StrategistConfig(provider="openai", model="m", api_key="k"), clock=Clock())
        self.assertEqual(s.token_budget(), default_tokens_per_min())
        s.spent.append([s.clock(), prefix + 2_000])  # a cache miss: the whole prefix written, plus the rest
        self.assertEqual(s.limit_reached(), "")
        self.assertEqual(make(tokens_per_min=5000).token_budget(), 5000)  # an explicit budget wins

    def test_openai_sends_a_cache_key_and_counts_only_uncached_tokens(self):
        sent = {}

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def urlopen(req, timeout):
            sent.update(json.loads(req.data))
            usage = {"prompt_tokens": 1000, "completion_tokens": 10, "prompt_tokens_details": {"cached_tokens": 900}}
            body = {"choices": [{"message": {"content": json.dumps(WAIT_ANSWER)}}], "usage": usage}
            return Resp(json.dumps(body).encode())

        with mock.patch("urllib.request.urlopen", urlopen):
            _, usage = OpenAIChatClient("k", "m").complete(
                [{"role": "system", "content": "ref", "cache": True}, {"role": "user", "content": "state"}]
            )
        self.assertEqual(sent["prompt_cache_key"], "agentrealm-planner")
        self.assertEqual(sent["messages"][0], {"role": "system", "content": "ref"})  # no extra keys on the wire
        self.assertEqual(tokens_used(usage), 110)

    def test_anthropic_refusal_is_a_failed_call(self):
        s = self.from_env(FakeAnthropicSDK(stop_reason="refusal"), ANTHROPIC_API_KEY="a")
        s.clock = Clock()
        r = fake_runner()
        round_trip(s, r)
        self.assertEqual(logged_events(r)[:2], ["ask", "error"])
        self.assertEqual(r.plan.current()["op"], "explore_area")


class FailFastTest(unittest.TestCase):
    """The planner is on by default: no key means no run, unless --no-planner."""

    def raises(self, **env: str) -> str:
        with mock.patch.dict(os.environ, no_planner_env(**env), clear=True), mock.patch.dict(sys.modules, {"anthropic": None}), \
                self.assertRaises(PlannerConfigError) as e:
            Strategist.from_env()
        self.assertNotIn("\n", str(e.exception))
        return str(e.exception)

    def test_missing_key(self):
        self.assertIn("AGENTREALM_PLANNER_ANTHROPIC_KEY or ANTHROPIC_API_KEY", self.raises())
        self.assertIn("AGENTREALM_PLANNER_OPENAI_KEY or OPENAI_API_KEY", self.raises(AGENTREALM_PLANNER_PROVIDER="openai"))

    def test_openai_needs_a_model(self):
        self.assertIn("AGENTREALM_PLANNER_MODEL", self.raises(OPENAI_API_KEY="o"))

    def test_unknown_provider(self):
        self.assertIn("AGENTREALM_PLANNER_PROVIDER", self.raises(AGENTREALM_PLANNER_PROVIDER="llama"))

    def test_anthropic_sdk_not_installed(self):
        self.assertIn("make setup", self.raises(ANTHROPIC_API_KEY="a"))

    def test_no_planner_env_is_the_test_mode(self):
        with mock.patch.dict(os.environ, no_planner_env(AGENTREALM_NO_PLANNER="1"), clear=True):
            s = Strategist.from_env()
        self.assertFalse(s.enabled)

    def cli(self, *argv: str) -> tuple[int, str, mock.Mock]:
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "t.toml"
            profile.write_text('world = "sandbox"\n')
            err = io.StringIO()
            env = no_planner_env(AGENTREALM_API_KEY="k", AGENTREALM_CHARACTER_ID="5")
            with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(cli, "Client") as client, \
                    mock.patch.object(cli, "run", return_value=0) as run, mock.patch("sys.stderr", err):
                rc = cli.main(["run", str(profile), *argv])
        return rc, err.getvalue(), client, run

    def test_run_without_a_key_fails_before_any_call(self):
        rc, err, client, run = self.cli()
        self.assertEqual(rc, 2)
        self.assertEqual(err.count("\n"), 1)
        self.assertIn("ANTHROPIC_API_KEY", err)
        client.assert_not_called()
        run.assert_not_called()

    def test_run_no_planner_plays_without_one(self):
        rc, err, client, run = self.cli("--no-planner")
        self.assertEqual(rc, 0, err)
        planner = run.call_args.args[3]
        self.assertFalse(planner.enabled)


class NoPlannerTest(unittest.TestCase):
    def test_test_mode_drains_logs_and_starts_no_thread(self):
        s = Strategist.off()
        r = fake_runner()
        s.start()
        self.assertIsNone(s._thread)
        queue_signal(r.mem, {"trigger": "death", "tick": 3})
        s.on_window(r)
        self.assertEqual(r.mem.strategist_signals, [])
        self.assertEqual(s.inbox, [])
        self.assertEqual(logged_events(r), ["drain"])


class ThreadTest(unittest.TestCase):
    def test_background_thread_answers_and_stops(self):
        s, r = make(FakeLLM(WAIT_ANSWER)), fake_runner()
        s.start()
        self.addCleanup(s.stop)
        s.on_window(r)
        for _ in range(100):
            s.on_window(r)
            if s.in_flight is None:
                break
            threading.Event().wait(0.02)
        s.stop()
        self.assertFalse(s._thread.is_alive())
        self.assertEqual(r.plan.current()["op"], "wait")


class ParseReplyTest(unittest.TestCase):
    def test_plain_and_fenced(self):
        self.assertEqual(parse_reply('{"a": 1}'), {"a": 1})
        self.assertEqual(parse_reply('Here:\n```json\n{"a": 1}\n```'), {"a": 1})
        with self.assertRaises(ValueError):
            parse_reply("no json here")


if __name__ == "__main__":
    unittest.main()
