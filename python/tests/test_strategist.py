"""A35: strategist triggers, limits, and plan application."""

from __future__ import annotations

import json
import threading
import unittest
from unittest.mock import MagicMock

from agentrealm_agent.directives import Directives, PARAM_DEFAULTS
from agentrealm_agent.memory import Memory
from agentrealm_agent.plan import Plan
from agentrealm_agent.strategist import (
    Strategist,
    StrategistConfig,
    build_prompt,
    drain_triggers,
    queue_signal,
)
from agentrealm_agent.world import WorldModel


class FakeLLM:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls = 0

    def complete(self, messages: list[dict[str, str]]) -> tuple[str, dict]:
        self.calls += 1
        return json.dumps(self.payload), {"prompt_tokens": 100, "completion_tokens": 50}


class StrategistTriggerTest(unittest.TestCase):
    def test_drain_clue_and_stuck(self):
        m = Memory()
        m.clue_signals.append({"trigger": "clue", "text": "go north"})
        m.nav_stuck.stuck_signals.append({"trigger": "stuck", "goal": "goto"})
        queue_signal(m, {"trigger": "death", "tick": 1})
        out = drain_triggers(m)
        self.assertEqual(len(out), 3)
        self.assertEqual(m.clue_signals, [])
        self.assertEqual(m.nav_stuck.stuck_signals, [])
        self.assertEqual(m.strategist_signals, [])


class StrategistApplyTest(unittest.TestCase):
    def test_apply_replaces_plan_when_no_directives_goals(self):
        lock = threading.Lock()
        cfg = StrategistConfig(model="test", api_key="key", min_interval_s=0, max_calls=10, max_usd=10)
        s = Strategist(config=cfg, lock=lock, _client=FakeLLM({"goals": [{"op": "wait", "seconds": 1}], "notes": "hi"}))
        runner = MagicMock()
        runner.directives.directives = Directives(params=dict(PARAM_DEFAULTS), goals=[])
        runner.plan = Plan([{"op": "explore_area", "x": 0, "y": 0, "radius": 9999}], dict(PARAM_DEFAULTS))
        runner.tick_hz = 10
        runner.mem = Memory()
        runner.mem.path = [(1, 1)]
        runner.mem.goal = "explore"
        s._pending = ([{"op": "wait", "seconds": 1}], dict(PARAM_DEFAULTS), "hi")
        self.assertTrue(s.apply_pending(runner))
        self.assertEqual(runner.plan.current()["op"], "wait")
        self.assertEqual(runner.plan.notes, "hi")
        self.assertEqual(runner.mem.path, [])

    def test_apply_skipped_when_directives_own_stack(self):
        lock = threading.Lock()
        cfg = StrategistConfig(model="test", api_key="key")
        s = Strategist(config=cfg, lock=lock)
        runner = MagicMock()
        runner.directives.directives = Directives(params=dict(PARAM_DEFAULTS), goals=["gather_gems:5"])
        runner.plan = Plan([{"op": "explore_area", "x": 0, "y": 0, "radius": 1}], dict(PARAM_DEFAULTS))
        s._pending = ([{"op": "wait", "seconds": 0}], dict(PARAM_DEFAULTS), "")
        self.assertFalse(s.apply_pending(runner))
        self.assertEqual(runner.plan.current()["op"], "explore_area")


class StrategistThreadTest(unittest.TestCase):
    def test_background_call_applies_answer(self):
        lock = threading.Lock()
        payload = {
            "goals": [{"op": "travel", "to": "town", "x": 0, "y": 0}],
            "params": {"curiosity": 0.4},
            "notes": "from model",
        }
        fake = FakeLLM(payload)
        cfg = StrategistConfig(model="m", api_key="k", min_interval_s=0, max_calls=5, max_usd=5)
        s = Strategist(config=cfg, lock=lock, _client=fake)
        runner = MagicMock()
        runner.world = WorldModel(character_id=1, map_id=7, pos=(0, 0), tick=10)
        runner.world.alive = True
        runner.directives.directives = Directives(params=dict(PARAM_DEFAULTS))
        runner.plan = Plan([{"op": "explore_area", "x": 0, "y": 0, "radius": 1}], dict(PARAM_DEFAULTS))
        runner.knowledge = None
        runner.tick_hz = 10
        runner.mem = Memory()
        runner.log = MagicMock()
        s.start(runner)
        queue_signal(runner.mem, {"trigger": "idle", "tick": 10})
        s.notify()
        for _ in range(50):
            with lock:
                if s._pending is not None:
                    break
            threading.Event().wait(0.05)
        s.stop()
        self.assertEqual(fake.calls, 1)
        with lock:
            s.apply_pending(runner)
        self.assertEqual(runner.plan.current()["op"], "travel")

    def test_no_model_drains_without_calling(self):
        lock = threading.Lock()
        cfg = StrategistConfig(model="", api_key="")
        fake = FakeLLM({"goals": []})
        s = Strategist(config=cfg, lock=lock, _client=fake)
        runner = MagicMock()
        runner.mem = Memory()
        runner.log = MagicMock()
        s.start(runner)
        queue_signal(runner.mem, {"trigger": "death", "tick": 3})
        s.notify()
        threading.Event().wait(0.2)
        s.stop()
        self.assertEqual(fake.calls, 0)
        self.assertEqual(runner.mem.strategist_signals, [])


class StrategistPromptTest(unittest.TestCase):
    def test_build_prompt_includes_triggers(self):
        w = WorldModel(character_id=1, map_id=1, pos=(2, 3), tick=5)
        plan = Plan([{"op": "wait", "seconds": 0}], dict(PARAM_DEFAULTS))
        messages = build_prompt(
            triggers=[{"trigger": "clue", "text": "torch"}],
            w=w,
            plan=plan,
            directives=Directives(params=dict(PARAM_DEFAULTS), instructions="be bold"),
            knowledge=None,
        )
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("torch", messages[1]["content"])
        self.assertIn("be bold", messages[1]["content"])


if __name__ == "__main__":
    unittest.main()
