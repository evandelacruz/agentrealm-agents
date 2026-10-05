"""A16: M7 acceptance metrics, navigation fixture, and smoke script (no server)."""

from __future__ import annotations

import importlib.util
import io
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from agentrealm_agent import config
from agentrealm_agent.m7_acceptance import (
    LOOP_SAME_REASON_LIMIT,
    M7AcceptanceMetrics,
    TARGET_DISTANCE,
)
from tests.fixtures.navigation import grids, sim

REPO = Path(__file__).resolve().parents[2]
SMOKE_PATH = REPO / "scripts" / "smoke_m7_olympuff.py"


def load_smoke():
    spec = importlib.util.spec_from_file_location("smoke_m7_olympuff", SMOKE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class M7AcceptanceMetricsTest(unittest.TestCase):
    def test_navigation_ok_at_distance_or_give_up(self):
        m = M7AcceptanceMetrics(target_distance=150)
        self.assertFalse(m.navigation_ok())
        m.max_distance = 150
        self.assertTrue(m.navigation_ok())
        m.max_distance = 0
        m.give_up_reasons.append("no_path")
        self.assertTrue(m.navigation_ok())

    def test_loop_detection(self):
        from agentrealm_agent.brain import Memory
        from agentrealm_agent.config import Policy
        from agentrealm_agent.world import WorldModel

        m = M7AcceptanceMetrics()
        w = WorldModel(character_id=1, map_id=7, pos=(0, 0), perception=5, tick=1)
        mem = Memory()
        policy = Policy()
        params = {"retreat_hits": 2, "risk": 0.5, "lives_floor": 3, "fight_margin": 1.5}
        m.note_overworld(7)
        for _ in range(LOOP_SAME_REASON_LIMIT):
            m.on_decision(
                w,
                mem,
                reason="explore → (1,0)",
                state="Explore",
                policy=policy,
                params=params,
                intents=[{"verb": "Step", "direction": "east"}],
            )
        self.assertTrue(m.loop_detected)
        self.assertIn("loop", m.failures()[0])

    def test_api_errors_fail_the_run(self):
        from agentrealm_agent.client import ApiError

        class Reads:
            def tick(self, *a, **k):
                raise ApiError(429, "rate_limited")

        metrics = M7AcceptanceMetrics()
        client = metrics.wrap(Reads())
        with self.assertRaises(ApiError):
            client.tick(1, None)
        metrics.on_window(urgent=False)
        self.assertTrue(any("API error" in f for f in metrics.failures()))


class NavigationFixtureTest(unittest.TestCase):
    def test_open_corridor_reaches_150_blocks(self):
        sc = grids.OPEN_CORRIDOR_150
        policy = sim.scripted(goals=["goto"], goto=sc.goal)
        r = sim.run(sc, policy, max_decisions=500)
        self.assertEqual(r.outcome, "reached")
        dist = abs(r.world.pos[0] - sc.start[0])
        self.assertGreaterEqual(dist, TARGET_DISTANCE)

    def test_hedge_gives_up_with_reason_not_loop(self):
        sc = grids.HEDGE_LINE
        policy = sim.scripted(goals=["goto"], goto=sc.goal)
        r = sim.run(sc, policy)
        self.assertEqual(r.outcome, "abandoned")
        self.assertTrue(r.memory.nav_stuck.stuck_signals)
        self.assertIn("escalation", r.signal or {})


class SmokeScriptTest(unittest.TestCase):
    def setUp(self):
        self.smoke = load_smoke()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        patch = mock.patch.object(config, "STATE_DIR", self.tmp)
        patch.start()
        self.addCleanup(patch.stop)

    def main(self, argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict("os.environ", env or {}, clear=True), redirect_stdout(out), redirect_stderr(err):
            code = self.smoke.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_no_api_key_exits_2(self):
        code, _, err = self.main([])
        self.assertEqual(code, 2)
        self.assertIn("AGENTREALM_API_KEY", err)

    def exit_code_for(self, metrics, elapsed: float = 3600.0):
        stop = threading.Event()
        stop.set()
        metrics.stop = stop
        with mock.patch.object(self.smoke, "Client") as Client, \
                mock.patch.object(self.smoke, "ensure_character", return_value=9), \
                mock.patch.object(self.smoke, "run_smoke", return_value=(metrics, elapsed)):
            client = Client.return_value
            client.self_.return_value = {"alive": True}
            return self.main(["--api-key", "k", "--seconds", "10"])

    def test_pass_when_criteria_hold(self):
        m = M7AcceptanceMetrics(target_distance=10)
        m.max_distance = 20
        code, out, _ = self.exit_code_for(m, elapsed=10.0)
        self.assertEqual(code, 0)
        self.assertIn("PASS", out)

    def test_fail_when_navigation_missing(self):
        m = M7AcceptanceMetrics(target_distance=150)
        code, _, err = self.exit_code_for(m, elapsed=3600.0)
        self.assertEqual(code, 1)
        self.assertIn("FAIL", err)


if __name__ == "__main__":
    unittest.main()
