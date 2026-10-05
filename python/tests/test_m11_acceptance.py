"""A40: M11 acceptance metrics and smoke script (no server)."""

from __future__ import annotations

import importlib.util
import io
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from agentrealm_agent import config
from agentrealm_agent.m11_acceptance import TARGET_SECONDS, M11AcceptanceMetrics
from agentrealm_agent.m7_acceptance import OSCILLATION_ABORT_COUNT
from agentrealm_agent.memory import Memory
from agentrealm_agent.world import Entity, WorldModel
from tests.test_m7_acceptance import OVERWORLD, decide, gave_up, metrics as m7_metrics, open_world

REPO = Path(__file__).resolve().parents[2]
SMOKE_PATH = REPO / "scripts" / "smoke_m11_olympuff.py"


def load_smoke():
    spec = importlib.util.spec_from_file_location("smoke_m11_olympuff", SMOKE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def metrics(**kw) -> M11AcceptanceMetrics:
    kw.setdefault("overworld_map_id", OVERWORLD)
    return M11AcceptanceMetrics(**kw)


INTERIOR = OVERWORLD + 1


def level_world(level: int | None, pos=(0, 0)) -> WorldModel:
    """A level interior map; ``level=None`` before a read has named its level (A37)."""
    w = open_world(pos)
    w.map_id, w.terrain_map, w.map_level = INTERIOR, INTERIOR, level
    return w


class LevelGateTest(unittest.TestCase):
    def test_fails_without_a_clear_on_a_full_run(self):
        m = metrics()
        self.assertFalse(m.milestone_ok())
        self.assertIn("no level_clear_ceremony seen", m.failures())

    def test_clear_without_a_follow_on_attempt_fails(self):
        m = metrics()
        m.on_level_clear({"level_number": 1, "max_health_gain": 5})
        self.assertFalse(m.milestone_ok())
        msg = "; ".join(m.failures())
        self.assertIn("never attempted the next", msg)

    def test_re_entering_a_level_after_clear_passes(self):
        m = metrics()
        m.on_level_clear({"level_number": 1, "max_health_gain": 5})
        decide(m, open_world(), Memory())
        decide(m, level_world(2), Memory())
        self.assertTrue(m.milestone_ok())
        self.assertEqual(m.failures(), [])

    def test_enter_level_on_the_stack_after_clear_passes(self):
        m, mem = metrics(), Memory()
        m.on_level_clear({"level_number": 1, "max_health_gain": 5})
        mem.goal_op = {"op": "enter_level", "x": 3, "y": 4}
        decide(m, open_world(), mem)
        self.assertTrue(m.milestone_ok())

    def test_plan_op_inside_the_cleared_level_does_not_count(self):
        m, mem = metrics(), Memory()
        m.on_level_clear({"level_number": 1, "max_health_gain": 5})
        mem.goal_op = {"op": "fight_boss", "x": 3, "y": 4}
        decide(m, level_world(1), mem)
        self.assertFalse(m.milestone_ok())
        decide(m, open_world(), mem)
        self.assertTrue(m.milestone_ok(), "the same op counts once outside the level")

    def test_a_map_change_inside_the_cleared_level_is_not_leaving_it(self):
        m = metrics()
        m.on_level_clear({"level_number": 1, "max_health_gain": 5})
        decide(m, level_world(None), Memory())
        decide(m, level_world(1), Memory())
        self.assertFalse(m.milestone_ok())

    def test_plan_op_on_an_interior_map_with_no_level_yet_does_not_count(self):
        m, mem = metrics(), Memory()
        m.on_level_clear({"level_number": 1, "max_health_gain": 5})
        mem.goal_op = {"op": "fight_boss", "x": 3, "y": 4}
        decide(m, level_world(None), mem)
        self.assertFalse(m.milestone_ok())

    def test_short_run_skips_level_criteria(self):
        m = metrics()
        self.assertEqual(m.failures(full_run=False), [])


class SharedSurvivalGateTest(unittest.TestCase):
    def test_death_and_loop_fail(self):
        m = metrics()
        m.on_death()
        self.assertIn("death(s)", m.failures(full_run=False)[0])
        for _ in range(24):
            decide(m, open_world(), reason="explore → (1,0)")
        self.assertTrue(m.loop_detected)


def threatened(level: int | None = None) -> WorldModel:
    w = open_world()
    w.map_level = level
    w.health, w.lives = 3, 6
    w.entities = [Entity("npc", 1, (1, 0), code="gnawer")]
    w.threat.record(("npc", "gnawer"), 5)
    return w


class SurvivalStatesTest(unittest.TestCase):
    """A40 adds Boss to the survival states; A16 does not (PLAN.md)."""

    def test_boss_is_a_survival_state_for_m11(self):
        m = metrics()
        decide(m, threatened(level=1), state="Boss")
        self.assertEqual(m.retreat_misses, 0)
        decide(m, threatened(), state="Explore")
        self.assertEqual(m.retreat_misses, 1)

    def test_boss_is_a_retreat_miss_for_m7(self):
        m = m7_metrics()
        decide(m, threatened(), state="Boss")
        self.assertEqual(m.retreat_misses, 1)


class OscillationStopTest(unittest.TestCase):
    """The smoke scripts assign ``stop`` after the metrics are built."""

    def assert_abort_stops(self, m):
        stop = threading.Event()
        m.stop = stop
        for i in range(OSCILLATION_ABORT_COUNT + 1):
            m.on_oscillation(gave_up(i * 100))
        self.assertTrue(stop.is_set())
        self.assertIn("sustained oscillation", m.oscillation_abort)

    def test_m7_abort_stops_a_late_assigned_stop(self):
        self.assert_abort_stops(m7_metrics())

    def test_m11_abort_stops_a_late_assigned_stop(self):
        self.assert_abort_stops(metrics())


STRATEGIST_ENV = {"AGENTREALM_STRATEGIST_MODEL": "fake-model", "AGENTREALM_STRATEGIST_API_KEY": "fake-key"}


class SharedRunSmokeTest(unittest.TestCase):
    def test_oscillation_abort_ends_the_runner_through_run_smoke(self):
        common = sys.modules[load_smoke().run_smoke.__module__]
        seen = {}

        class FakeRunner:
            def __init__(self, cfg, client, cid, stop, out, *, knowledge, acceptance):
                seen["stop"] = stop
                self.acceptance = acceptance

            def run(self):
                for i in range(OSCILLATION_ABORT_COUNT + 1):
                    self.acceptance.on_oscillation(gave_up(i * 100))
                seen["stopped_during_run"] = seen["stop"].is_set()

        cfg = config.load(REPO / "python" / "characters" / "olympuff_m11.toml")
        with mock.patch.object(common, "Runner", FakeRunner), \
                mock.patch.object(common, "load_knowledge"), \
                mock.patch.object(common, "save_knowledge"), \
                redirect_stdout(io.StringIO()):
            m, _ = common.run_smoke(mock.Mock(), cfg, 9, metrics(), timeout_s=0)
        self.assertIs(m.stop, seen["stop"])
        self.assertTrue(seen["stopped_during_run"], "the abort must stop the runner, not wait for it to end")
        self.assertIsNotNone(m.oscillation_abort)


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

    def run_main(self, seconds: float, played):
        def run_smoke(client, cfg, cid, metrics, *, timeout_s):
            played(metrics)
            return metrics, seconds

        with mock.patch.object(self.smoke, "Client") as Client, \
                mock.patch.object(self.smoke, "resolve_character_id", return_value=9), \
                mock.patch.object(self.smoke.time, "sleep"), \
                mock.patch.object(self.smoke, "run_smoke", side_effect=run_smoke):
            client = Client.return_value
            client.world.return_value = {"town": {"map_id": OVERWORLD, "x": 0, "y": 0}}
            client.position.return_value = {"map_id": OVERWORLD, "x": 10, "y": 20}
            client.self_.return_value = {"alive": True}
            return self.main(["--api-key", "k", "--character-id", "9", "--seconds", str(seconds)], STRATEGIST_ENV)

    def test_no_api_key_exits_2(self):
        code, _, err = self.main([])
        self.assertEqual(code, 2)
        self.assertIn("AGENTREALM_API_KEY", err)

    def test_no_strategist_exits_2_before_any_call(self):
        with mock.patch.object(self.smoke, "Client") as Client:
            code, _, err = self.main(["--api-key", "k", "--character-id", "9"])
        self.assertEqual(code, 2)
        self.assertIn("AGENTREALM_STRATEGIST_MODEL", err)
        Client.assert_not_called()

    def test_short_run_passes_without_levels(self):
        code, out, _ = self.run_main(10, lambda m: None)
        self.assertEqual(code, 0)
        self.assertIn("PASS", out)

    def test_full_run_fails_without_milestone(self):
        code, _, err = self.run_main(TARGET_SECONDS, lambda m: None)
        self.assertEqual(code, 1)
        self.assertIn("level_clear_ceremony", err)

    def test_full_run_passes_when_gate_met(self):
        def played(m):
            m.on_level_clear({"level_number": 1, "max_health_gain": 2})
            m.next_level_attempted = True

        code, out, _ = self.run_main(TARGET_SECONDS, played)
        self.assertEqual(code, 0, out)


class RunnerHookTest(unittest.TestCase):
    def test_runner_forwards_level_clear_ceremony(self):
        from agentrealm_agent.runner import Runner

        cfg = config.load(REPO / "python" / "characters" / "olympuff_m11.toml")
        m = M11AcceptanceMetrics(overworld_map_id=OVERWORLD)
        stop = threading.Event()

        class Client:
            def tick(self, cid, intents, *, snapshot_version=None):
                return {
                    "tick": 4,
                    "level_clear_ceremony": {"level_number": 1, "max_health_gain": 2},
                    "intent_results": [],
                }

            def self_(self, cid):
                return {"alive": True, "perception_range": 5, "movement_range": 1}

            def world(self, cid):
                return {"town": {"map_id": OVERWORLD, "x": 0, "y": 0}}

            def position(self, cid):
                return {"map_id": OVERWORLD, "x": 0, "y": 0}

        r = Runner(cfg, Client(), 1, stop, lambda s: None, acceptance=m)
        r.world.apply_self({"alive": True, "perception_range": 5, "movement_range": 1})
        r.world.apply_position({"map_id": OVERWORLD, "x": 0, "y": 0})
        r.world.tick = 3
        r.mem.state = "Explore"
        r.tick()
        self.assertEqual(m.cleared_levels, {1})


if __name__ == "__main__":
    unittest.main()
