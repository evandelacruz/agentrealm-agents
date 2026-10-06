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
from agentrealm_agent.memory import Memory, queue_signal
from agentrealm_agent.plan import Plan
from agentrealm_agent.runner import Runner
from agentrealm_agent import __main__ as cli
from agentrealm_agent.plan import validate_goal_op
from agentrealm_agent.strategist import (
    DEFAULT_ANTHROPIC_MODEL,
    AnthropicClient,
    OpenAIChatClient,
    PlannerConfigError,
    Strategist,
    StrategistConfig,
    build_prompt,
    drain_triggers,
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
    return SimpleNamespace(
        world=w,
        mem=Memory(),
        plan=Plan([{"op": "explore_area", "x": 0, "y": 0, "radius": 9999}], dict(PARAM_DEFAULTS)),
        directives=SimpleNamespace(directives=Directives(params=dict(PARAM_DEFAULTS), goals=goals or [])),
        knowledge=None,
        tick_hz=10,
        log=mock.MagicMock(),
        acceptance=None,
    )


def make(client=None, **cfg) -> Strategist:
    return Strategist(config=StrategistConfig(provider="openai", model="m", api_key="k", **cfg), client=client, clock=Clock())


def no_planner_env(**extra: str) -> dict[str, str]:
    """The environment without any planner setting or key, plus ``extra``."""
    keys = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")
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

    def test_map_trigger_once_per_new_map(self):
        s, r = make(), fake_runner()
        s._collect(r)
        s._collect(r)
        r.world.map_id, r.world.map_level = 8, 2
        s._collect(r)
        r.world.map_id = 7  # back on a map already seen
        s._collect(r)
        self.assertEqual([(t["trigger"], t["map_id"]) for t in s.inbox], [("map", 7), ("map", 8)])
        self.assertEqual(s.inbox[1]["level"], 2)

    def test_hurt_trigger_once_per_drop_below_the_threshold(self):
        s, r = make(hurt_fraction=0.5), fake_runner()
        s._maps_seen.add(7)
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
        s._maps_seen.add(7)
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

    def test_plan_pops_queue_goal_done_and_failed(self):
        m = Memory()
        plan = Plan([{"op": "wait", "seconds": 0}, {"op": "explore_area", "x": 0, "y": 0, "radius": 1}], dict(PARAM_DEFAULTS))
        plan.finish_current("done", memory=m)
        plan.drop_current("no path", memory=m)
        self.assertEqual([(t["trigger"], t["op"]["op"]) for t in m.strategist_signals], [("goal_done", "wait"), ("goal_failed", "explore_area")])

    def test_died_event_queues_death(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(config, "STATE_DIR", Path(tmp)):
            cfg = CharacterConfig("T", "sandbox", Policy(kind="scripted", goals=["hold"]), Path("t.toml"))
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
        self.assertEqual(ask["messages"], llm.messages[0])
        self.assertIn({"trigger": "death", "tick": 3}, ask["triggers"])

    def test_prompt_has_whole_plan_every_clue_and_the_op_table(self):
        kb = SimpleNamespace(lock=threading.Lock(), clues=[{"kind": "sign", "text": f"clue {i}"} for i in range(20)])
        plan = Plan([{"op": "wait", "seconds": 0}, {"op": "explore_area", "x": 3, "y": 4, "radius": 5}], dict(PARAM_DEFAULTS))
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
        for op in ("explore_area", "travel", "gather_gems", "buy", "fight_boss", "compose", "use_block", "wait"):
            self.assertIn(f"- {op}: ", messages[0]["content"])

    def test_reply_in_a_code_fence_is_read(self):
        s, r = make(FakeLLM("```json\n" + json.dumps(WAIT_ANSWER) + "\n```")), fake_runner()
        round_trip(s, r)
        self.assertEqual(r.plan.current()["op"], "wait")

    def test_reply_without_goals_key_keeps_the_stack(self):
        s, r = make(FakeLLM({"notes": "carry on"})), fake_runner()
        round_trip(s, r)
        self.assertEqual(r.plan.current()["op"], "explore_area")
        self.assertEqual(logged_events(r), ["ask", "kept"])

    def test_directives_goals_override_the_planner(self):
        s, r = make(FakeLLM(WAIT_ANSWER)), fake_runner(goals=["gather_gems:5"])
        round_trip(s, r)
        self.assertEqual(r.plan.current()["op"], "explore_area")
        self.assertEqual(logged_events(r), ["ask", "kept"])


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
        self.assertEqual(r.plan.current()["op"], "explore_area")


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
        self.assertEqual(r.plan.current()["op"], "explore_area")
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
        policy = Policy(kind="scripted", goals=["hold"], on_hostile="flee", hostile=["npc"], hostile_range=2)
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
        cfg = CharacterConfig("t", "sandbox", Policy(kind="scripted", goals=["explore", "hold"]), path)
        r = Runner(cfg, None, 1, threading.Event(), out=lambda _: None, strategist=strategist)
        self.addCleanup(r.trace.close)
        return r

    def test_planner_on_starts_with_an_empty_stack(self):
        r = self.runner(make(FakeLLM()))
        self.assertIsNone(r.plan.current())

    def test_no_planner_keeps_the_built_in_plan(self):
        r = self.runner()
        self.assertEqual([op["op"] for op in r.plan.goals], ["explore_area", "wait"])

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
        self.assertEqual(logged_events(r), ["ask", "error", "ask"])  # retried in the same window
        s.serve_one(timeout=0)
        self.assertEqual(llm.messages[1], llm.messages[0])  # the same triggers again
        s.on_window(r)
        self.assertEqual(r.plan.current()["op"], "wait")


class BudgetTest(unittest.TestCase):
    """Calls and tokens per minute of play: a long session never runs dry."""

    def test_calls_per_minute_count_failed_calls_and_refill(self):
        llm = FakeLLM(*[RuntimeError("down")] * 5)
        s, r = make(llm, calls_per_min=2), fake_runner()
        for _ in range(5):
            s.on_window(r)
            s.serve_one(timeout=0)
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

    def __init__(self, reply: str = '{"goals": []}', stop_reason: str = "end_turn") -> None:
        self.requests: list[dict] = []
        self.reply, self.stop_reason = reply, stop_reason
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
            usage=SimpleNamespace(input_tokens=30, output_tokens=12),
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
        self.assertEqual((req["model"], req["system"], req["messages"]), ("claude-sonnet-5-5", "sys", [{"role": "user", "content": "state"}]))
        self.assertNotIn("temperature", req)
        self.assertEqual(req["extra_body"]["fallbacks"], "default")

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
        self.assertIn("ANTHROPIC_API_KEY", self.raises())
        self.assertIn("OPENAI_API_KEY", self.raises(AGENTREALM_PLANNER_PROVIDER="openai"))

    def test_openai_needs_a_model(self):
        self.assertIn("AGENTREALM_PLANNER_MODEL", self.raises(OPENAI_API_KEY="o"))

    def test_unknown_provider(self):
        self.assertIn("AGENTREALM_PLANNER_PROVIDER", self.raises(AGENTREALM_PLANNER_PROVIDER="llama"))

    def test_anthropic_sdk_not_installed(self):
        self.assertIn("pip install anthropic", self.raises(ANTHROPIC_API_KEY="a"))

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
