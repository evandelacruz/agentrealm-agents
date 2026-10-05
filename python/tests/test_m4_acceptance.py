"""A36: M4 acceptance metrics and smoke script (no server)."""

from __future__ import annotations

import importlib.util
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from agentrealm_agent import config
from agentrealm_agent.client import ApiError
from agentrealm_agent.config import Policy
from agentrealm_agent.directives import PARAM_DEFAULTS
from agentrealm_agent.m4_acceptance import GATE_OPS, M4AcceptanceMetrics, gate_key, op_matches
from agentrealm_agent.memory import Memory
from agentrealm_agent.world import WorldModel

REPO = Path(__file__).resolve().parents[2]
SMOKE_PATH = REPO / "scripts" / "smoke_m4_strategist.py"


def load_smoke():
    spec = importlib.util.spec_from_file_location("smoke_m4_strategist", SMOKE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def metrics(**kw) -> M4AcceptanceMetrics:
    return M4AcceptanceMetrics(**kw)


def open_world() -> WorldModel:
    w = WorldModel(character_id=1, map_id=1, pos=(0, 0), perception=5, tick=1)
    for y in range(-2, 3):
        for x in range(-2, 3):
            w.view.tiles[(x, y)] = "dirt"
    w.terrain_center, w.terrain_map = (0, 0), 1
    return w


STEP = [{"verb": "Step", "direction": "right"}]


def tick(m, w, *, state="Shop", plan_op=None, intents=STEP):
    m.before_tick(
        w,
        Memory(),
        state=state,
        reason="test",
        intents=intents,
        policy=Policy(hostile=["npc"]),
        params=dict(PARAM_DEFAULTS),
        knowledge=None,
        plan_op=plan_op,
    )


class OpMatchTest(unittest.TestCase):
    def test_gate_ops_match_the_playable_plan_example(self):
        buy, travel, brk = GATE_OPS
        self.assertTrue(op_matches(buy, {"op": "buy", "code": "torch", "why": "dark"}))
        self.assertTrue(
            op_matches(travel, {"op": "travel", "to": "entrance", "x": 120, "y": 40, "map_id": 7})
        )
        self.assertTrue(
            op_matches(brk, {"op": "break_block", "x": 118, "y": 41, "capability": "burn"})
        )


class PlanningGateTest(unittest.TestCase):
    def test_fails_until_all_gate_ops_are_planned(self):
        m = metrics()
        self.assertTrue(any("never planned" in f for f in m.failures()))

    def test_strategist_applied_records_matching_ops(self):
        m = metrics()
        m.on_strategist_applied(
            [
                {"op": "buy", "code": "torch"},
                {"op": "travel", "to": "entrance", "x": 120, "y": 40},
                {"op": "break_block", "x": 118, "y": 41, "capability": "burn"},
            ]
        )
        self.assertEqual(len(m.planned), 3)
        self.assertEqual(m.failures(full=False), [])


class ExecutionGateTest(unittest.TestCase):
    def test_wrong_state_does_not_count_as_executed(self):
        m = metrics()
        buy = GATE_OPS[0]
        tick(m, open_world(), state="Explore", plan_op=buy)
        self.assertNotIn(gate_key(buy), m.executed)
        self.assertTrue(m.wrong_state)

    def test_owning_state_with_intents_marks_executed(self):
        m = metrics()
        m.on_strategist_applied(list(GATE_OPS))
        m.clue_triggers = 1
        w = open_world()
        for op, state in (
            (GATE_OPS[0], "Shop"),
            (GATE_OPS[1], "Travel"),
            (GATE_OPS[2], "Break"),
        ):
            tick(m, w, state=state, plan_op=op)
        self.assertEqual(len(m.executed), 3)
        self.assertEqual(m.failures(), [])

    def test_held_queue_does_not_count(self):
        m = metrics()
        m.before_tick(
            open_world(),
            Memory(),
            state="Shop",
            reason="queue held",
            intents=None,
            policy=Policy(hostile=["npc"]),
            params=dict(PARAM_DEFAULTS),
            knowledge=None,
            plan_op=GATE_OPS[0],
        )
        self.assertEqual(m.executed, set())


class ClueAndApiTest(unittest.TestCase):
    def test_full_run_needs_a_clue_trigger(self):
        m = metrics()
        m.on_strategist_applied(list(GATE_OPS))
        for op, state in zip(GATE_OPS, ("Shop", "Travel", "Break")):
            tick(m, open_world(), state=state, plan_op=op)
        self.assertTrue(any("no clue trigger" in f for f in m.failures()))

    def test_api_errors_fail_the_run(self):
        class Reads:
            def tick(self, *a, **k):
                raise ApiError(429, "rate_limited")

        m = metrics()
        client = m.wrap(Reads())
        with self.assertRaises(ApiError):
            client.tick(1, None)
        self.assertTrue(any("API error" in f for f in m.failures(full=False)))


class SmokeScriptTest(unittest.TestCase):
    def setUp(self):
        self.smoke = load_smoke()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.object(config, "STATE_DIR", Path(tmp.name))
        patch.start()
        self.addCleanup(patch.stop)

    def main(self, argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        base = {
            "AGENTREALM_API_KEY": "game",
            "AGENTREALM_STRATEGIST_MODEL": "test-model",
            "AGENTREALM_STRATEGIST_API_KEY": "llm",
        }
        if env:
            base.update(env)
        with mock.patch.dict("os.environ", base, clear=False), redirect_stdout(out), redirect_stderr(err):
            code = self.smoke.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_no_game_api_key_exits_2(self):
        code, _, err = self.main([], env={"AGENTREALM_API_KEY": ""})
        self.assertEqual(code, 2)
        self.assertIn("AGENTREALM_API_KEY", err)

    def test_no_strategist_config_exits_2(self):
        code, _, err = self.main(
            ["--api-key", "k", "--character-id", "9"],
            env={"AGENTREALM_STRATEGIST_MODEL": "", "AGENTREALM_STRATEGIST_API_KEY": ""},
        )
        self.assertEqual(code, 2)
        self.assertIn("STRATEGIST", err)

    def run_main(self, seconds: float, played):
        def run_smoke(client, cfg, cid, metrics, *, timeout_s):
            played(metrics)
            return metrics, seconds

        with mock.patch.object(self.smoke, "Client"), \
                mock.patch.object(self.smoke, "resolve_character_id", return_value=9), \
                mock.patch.object(self.smoke, "run_smoke", side_effect=run_smoke):
            return self.main(["--api-key", "k", "--character-id", "9", "--seconds", str(seconds)])

    def test_short_run_passes_without_full_gate(self):
        code, out, _ = self.run_main(10, lambda m: None)
        self.assertEqual(code, 0)
        self.assertIn("PASS", out)

    def test_full_run_fails_without_planning_or_execution(self):
        code, _, err = self.run_main(3600, lambda m: None)
        self.assertEqual(code, 1)
        self.assertIn("never planned", err)

    def test_full_run_passes_when_fixture_ops_are_planned_and_executed(self):
        def played(m):
            m.clue_triggers = 1
            m.on_strategist_applied(list(GATE_OPS))
            for op, state in zip(GATE_OPS, ("Shop", "Travel", "Break")):
                tick(m, open_world(), state=state, plan_op=op)

        code, out, err = self.run_main(3600, played)
        self.assertEqual(code, 0, err or out)
        self.assertIn("PASS", out)


if __name__ == "__main__":
    unittest.main()
