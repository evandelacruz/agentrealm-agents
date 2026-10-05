"""A35: strategist triggers, limits, failures, and plan application."""

from __future__ import annotations

import json
import os
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
from agentrealm_agent.strategist import (
    Strategist,
    StrategistConfig,
    build_prompt,
    drain_triggers,
)
from agentrealm_agent.world import Entity, WorldModel
from agentrealm_agent.zone_discovery import apply_zone

WAIT_ANSWER = {"goals": [{"op": "wait", "seconds": 1}], "notes": "from model"}


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
    cfg.setdefault("min_interval_s", 0)
    return Strategist(config=StrategistConfig(model="m", api_key="k", **cfg), client=client, clock=Clock())


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

    def test_level_trigger_once_per_level(self):
        s, r = make(), fake_runner()
        r.world.map_level = 2
        s._collect(r)
        s._collect(r)
        self.assertEqual([t["trigger"] for t in s.inbox], ["level"])

    def test_idle_trigger_once_per_quiet_spell(self):
        s, r = make(idle_ticks=50), fake_runner()
        r.mem.strategist_progress_tick = 10
        r.world.tick = 59
        s._collect(r)
        self.assertEqual(s.inbox, [])
        r.world.tick = 60
        s._collect(r)
        s._collect(r)
        self.assertEqual([t["trigger"] for t in s.inbox], ["idle"])
        r.mem.strategist_progress_tick = 60  # a Step lands, then another quiet spell
        r.world.tick = 110
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
        self.assertEqual(ask["triggers"], [{"trigger": "death", "tick": 3}])

    def test_prompt_has_whole_plan_and_every_clue(self):
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

    def test_empty_answer_keeps_plan(self):
        s, r = make(FakeLLM({"goals": [{"op": "nonsense"}]})), fake_runner()
        queue_signal(r.mem, {"trigger": "death"})
        round_trip(s, r)
        self.assertEqual(r.plan.current()["op"], "explore_area")
        self.assertEqual(logged_events(r), ["ask", "kept"])

    def test_directives_goals_keep_the_stack(self):
        s, r = make(FakeLLM(WAIT_ANSWER)), fake_runner(goals=["gather_gems:5"])
        queue_signal(r.mem, {"trigger": "death"})
        round_trip(s, r)
        self.assertEqual(r.plan.current()["op"], "explore_area")
        self.assertEqual(logged_events(r), ["ask", "kept"])


class ParamsTest(unittest.TestCase):
    def test_params_only_reply_applies_params_and_keeps_stack(self):
        s, r = make(FakeLLM({"goals": [], "params": {"retreat_hits": 4, "risk": 0.9}})), fake_runner()
        queue_signal(r.mem, {"trigger": "death"})
        round_trip(s, r)
        self.assertEqual(r.plan.current()["op"], "explore_area")
        self.assertEqual(r.plan.params["retreat_hits"], 4)  # tightened
        self.assertEqual(r.plan.params["risk"], PARAM_DEFAULTS["risk"])  # loosening dropped
        self.assertEqual(logged_events(r), ["ask", "kept"])

    def test_params_apply_while_directives_own_stack(self):
        reply = {"goals": [{"op": "wait", "seconds": 1}], "params": {"retreat_hits": 3}}
        s, r = make(FakeLLM(reply)), fake_runner(goals=["gather_gems:5"])
        queue_signal(r.mem, {"trigger": "death"})
        round_trip(s, r)
        self.assertEqual(r.plan.current()["op"], "explore_area")
        self.assertEqual(r.plan.params["retreat_hits"], 3)

    def test_params_carry_into_a_replaced_stack(self):
        reply = {"goals": [{"op": "wait", "seconds": 1}], "params": {"retreat_hits": 3}}
        s, r = make(FakeLLM(reply)), fake_runner()
        queue_signal(r.mem, {"trigger": "death"})
        round_trip(s, r)
        self.assertEqual(r.plan.current()["op"], "wait")
        self.assertEqual(r.plan.params["retreat_hits"], 3)

    def test_one_key_reply_keeps_the_other_keys(self):
        s, r = make(FakeLLM({"goals": [], "params": {"curiosity": 0.5}})), fake_runner()
        r.plan.params.update(retreat_hits=3, fight_margin=2.0, risk=0.2)
        queue_signal(r.mem, {"trigger": "death"})
        round_trip(s, r)
        self.assertEqual(r.plan.params["curiosity"], 0.5)
        self.assertEqual((r.plan.params["retreat_hits"], r.plan.params["fight_margin"], r.plan.params["risk"]), (3, 2.0, 0.2))

    def test_prompt_shows_current_params(self):
        llm = FakeLLM(WAIT_ANSWER)
        s, r = make(llm), fake_runner()
        r.plan.params["retreat_hits"] = 3
        queue_signal(r.mem, {"trigger": "death"})
        s.on_window(r)
        s.serve_one(timeout=0)
        self.assertIn('"retreat_hits": 3', llm.messages[0][1]["content"])

    def test_reply_without_params_keeps_earlier_ones(self):
        s, r = make(FakeLLM(WAIT_ANSWER)), fake_runner()
        r.plan.params["retreat_hits"] = 3
        queue_signal(r.mem, {"trigger": "death"})
        round_trip(s, r)
        self.assertEqual(r.plan.params["retreat_hits"], 3)


class RunnerParamsTest(unittest.TestCase):
    """The states read the plan's params, so a strategist reply changes what the agent does."""

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

    def test_strategist_retreat_hits_changes_the_decision(self):
        r = self.runner()
        before = r._decide(r.world, r.mem, plan=r.plan)
        self.assertFalse(before.reason.startswith("retreat"), before.reason)
        s = make(FakeLLM({"goals": [], "params": {"retreat_hits": 4}}))
        queue_signal(r.mem, {"trigger": "death"})
        round_trip(s, r)
        self.assertEqual(r.plan.params["retreat_hits"], 4)
        after = r._decide(r.world, r.mem, plan=r.plan)
        self.assertTrue(after.reason.startswith("retreat"), after.reason)


class FailureTest(unittest.TestCase):
    def test_failed_call_requeues_triggers_and_retries(self):
        llm = FakeLLM(RuntimeError("openai http 500"), WAIT_ANSWER)
        s, r = make(llm), fake_runner()
        queue_signal(r.mem, {"trigger": "death", "tick": 3})
        round_trip(s, r)
        self.assertEqual(logged_events(r), ["ask", "error", "ask"])  # retried in the same window
        s.serve_one(timeout=0)
        self.assertEqual(llm.messages[1], llm.messages[0])  # the same triggers again
        s.on_window(r)
        self.assertEqual(r.plan.current()["op"], "wait")

    def test_bad_json_is_an_error_and_keeps_plan(self):
        s, r = make(FakeLLM("not json"), max_calls=1), fake_runner()
        queue_signal(r.mem, {"trigger": "death"})
        round_trip(s, r)
        self.assertEqual(logged_events(r), ["ask", "error"])
        self.assertEqual(r.plan.current()["op"], "explore_area")
        self.assertEqual([t["trigger"] for t in s.inbox], ["death"])


class LimitTest(unittest.TestCase):
    def test_failed_calls_count_toward_max_calls(self):
        llm = FakeLLM(RuntimeError("down"), RuntimeError("down"), RuntimeError("down"))
        s, r = make(llm, max_calls=2), fake_runner()
        queue_signal(r.mem, {"trigger": "death"})
        for _ in range(5):
            s.on_window(r)
            s.serve_one(timeout=0)
        self.assertEqual(llm.calls, 2)
        self.assertEqual(s.limit_reached(), "max_calls")

    def test_min_interval_counts_from_failed_call(self):
        llm = FakeLLM(RuntimeError("down"), WAIT_ANSWER)
        s, r = make(llm, min_interval_s=60), fake_runner()
        queue_signal(r.mem, {"trigger": "death"})
        round_trip(s, r)
        self.assertEqual(llm.calls, 1)
        self.assertEqual(s.limit_reached(), "min_interval")
        s.clock.now += 60
        s.on_window(r)
        s.serve_one(timeout=0)
        self.assertEqual(llm.calls, 2)

    def test_token_cap_uses_reported_usage(self):
        llm = FakeLLM(WAIT_ANSWER, WAIT_ANSWER, usage={"prompt_tokens": 900, "completion_tokens": 200})
        s, r = make(llm, max_tokens=1000), fake_runner()
        queue_signal(r.mem, {"trigger": "death"})
        round_trip(s, r)
        self.assertEqual(s.tokens, 1100)
        queue_signal(r.mem, {"trigger": "death"})
        s.on_window(r)
        self.assertEqual(llm.calls, 1)
        self.assertEqual(s.limit_reached(), "max_tokens")

    def test_failed_call_charges_estimated_prompt_tokens(self):
        s, r = make(FakeLLM(RuntimeError("timeout"))), fake_runner()
        queue_signal(r.mem, {"trigger": "death"})
        round_trip(s, r)
        self.assertGreater(s.tokens, 100)  # the system prompt alone is over 400 characters

    def test_one_call_in_flight_at_a_time(self):
        llm = FakeLLM(WAIT_ANSWER)
        s, r = make(llm), fake_runner()
        queue_signal(r.mem, {"trigger": "death"})
        s.on_window(r)
        queue_signal(r.mem, {"trigger": "goal_done"})
        s.on_window(r)  # the first call has not answered; the new trigger waits
        self.assertEqual(s.calls, 1)
        self.assertEqual([t["trigger"] for t in s.inbox], ["goal_done"])


class NoModelTest(unittest.TestCase):
    def test_no_model_drains_logs_and_starts_no_thread(self):
        s = Strategist(config=StrategistConfig())
        r = fake_runner()
        s.start()
        self.assertIsNone(s._thread)
        queue_signal(r.mem, {"trigger": "death", "tick": 3})
        s.on_window(r)
        self.assertEqual(r.mem.strategist_signals, [])
        self.assertEqual(s.inbox, [])
        self.assertEqual(logged_events(r), ["drain"])

    def test_off_by_default(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGENTREALM_STRATEGIST") and k != "OPENAI_API_KEY"}
        with mock.patch.dict(os.environ, env, clear=True):
            s = Strategist.from_env()
        self.assertFalse(s.config.enabled)
        self.assertIsNone(s.client)

    def test_env_limits(self):
        env = {
            "AGENTREALM_STRATEGIST_MODEL": "m",
            "AGENTREALM_STRATEGIST_API_KEY": "k",
            "AGENTREALM_STRATEGIST_MAX_TOKENS": "5000",
            "AGENTREALM_STRATEGIST_MAX_CALLS": "3",
            "AGENTREALM_STRATEGIST_IDLE_MINUTES": "1",
        }
        with mock.patch.dict(os.environ, env):
            cfg = StrategistConfig.from_env(tick_hz=2)
        self.assertTrue(cfg.enabled)
        self.assertEqual((cfg.max_tokens, cfg.max_calls, cfg.idle_ticks), (5000, 3, 120))


class ThreadTest(unittest.TestCase):
    def test_background_thread_answers_and_stops(self):
        s, r = make(FakeLLM(WAIT_ANSWER)), fake_runner()
        s.start()
        self.addCleanup(s.stop)
        queue_signal(r.mem, {"trigger": "death"})
        s.on_window(r)
        for _ in range(100):
            s.on_window(r)
            if s.in_flight is None:
                break
            threading.Event().wait(0.02)
        s.stop()
        self.assertFalse(s._thread.is_alive())
        self.assertEqual(r.plan.current()["op"], "wait")


if __name__ == "__main__":
    unittest.main()
