"""A4: M6 acceptance metrics and runner hook (no server)."""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agentrealm_agent import config, runner
from agentrealm_agent.brain import Memory
from agentrealm_agent.config import CharacterConfig, Policy
from agentrealm_agent.m6_acceptance import (
    CALM_BUDGET_FRACTION,
    M6AcceptanceMetrics,
    TARGET_STEPS,
)
from agentrealm_agent.world import WorldModel


class M6AcceptanceMetricsTest(unittest.TestCase):
    def test_failures_when_under_steps_or_over_budget(self):
        m = M6AcceptanceMetrics(target_steps=200, steps_applied=199)
        self.assertIn("steps", m.failures()[0])
        m.steps_applied = 200
        m.on_rejection("movement_cooldown", verb="Step")
        self.assertTrue(any("movement_cooldown" in f for f in m.failures()))
        m.movement_cooldown_rejections = 0
        m.on_rejection("movement_cooldown", verb="Wait")
        self.assertFalse(m.failures())
        m.movement_cooldown_rejections = 0
        m.calm_windows = 100
        m.calm_tick_calls = int(100 * CALM_BUDGET_FRACTION)
        self.assertTrue(m.failures(), "at budget limit should fail")
        m.calm_tick_calls = int(100 * CALM_BUDGET_FRACTION) - 1
        self.assertFalse(m.failures())

    def test_calm_fraction(self):
        m = M6AcceptanceMetrics()
        m.on_window(urgent=False, call="skip")
        m.on_window(urgent=False, call="tick")
        self.assertAlmostEqual(m.calm_call_fraction, 0.5)


class FakeAcceptanceServer:
    """Minimal server: paced walks with applied Step results."""

    def __init__(self, windows: int, stop: threading.Event):
        self.tick_now = 100
        self.windows = windows
        self.stop = stop
        self.queue_id = "q-smoke"
        self.step_idx = 0

    def wait(self, not_before: float = 0.0) -> None:
        self.windows -= 1
        if self.windows < 0:
            self.stop.set()
        self.tick_now += 1

    def world(self, cid):
        return {"tick_rate_hz": 10, "code": "sandbox", "status": "live"}

    def tick(self, cid, intents, *, snapshot_version=None):
        results = []
        if intents:
            for i, intent in enumerate(intents):
                if intent.get("verb") == "Step":
                    results.append(
                        {
                            "tick": self.tick_now + i,
                            "queue_id": self.queue_id,
                            "index": i,
                            "outcome": "applied",
                        }
                    )
        return {
            "tick": self.tick_now,
            "window_remaining_ms": 0,
            "queue_id": self.queue_id,
            "intent_results": results,
        }


class RunnerAcceptanceHookTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.object(config, "STATE_DIR", Path(tmp.name))
        patch.start()
        self.addCleanup(patch.stop)

    def test_stops_after_target_steps(self):
        target = 5
        metrics = M6AcceptanceMetrics(target_steps=target)
        stop = threading.Event()
        server = FakeAcceptanceServer(500, stop)
        cfg = CharacterConfig(
            "T",
            "default",
            "test",
            "sandbox",
            Policy(goals=["goto"], goto=(40, 0), pickup=False, entity_refresh=1000),
            Path("t.toml"),
        )
        r = runner.Runner(cfg, server, 1, stop, out=lambda _: None, acceptance=metrics)
        w = WorldModel(character_id=1, map_id=7, pos=(0, 0), perception=25, tick=100)
        for y in range(-3, 4):
            for x in range(-3, 50):
                w.view.tiles[(x, y)] = "dirt"
        w.terrain_center, w.terrain_map, w.entities_tick = (0, 0), 7, 100
        r.world, r.mem = w, Memory(need_self=False, need_position=False)
        with mock.patch.object(runner.Pacer, "wait_next_window", lambda _self, nb=0.0: server.wait(nb)):
            r.run()
        self.assertGreaterEqual(metrics.steps_applied, target)
        # Stopped before burning every window (a batch of Step results can pass the goal).
        self.assertGreater(server.windows, 0)


if __name__ == "__main__":
    unittest.main()
