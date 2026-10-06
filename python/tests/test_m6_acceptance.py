"""A4: M6 acceptance metrics, the runner hook, walk pacing, and the smoke script (no server)."""

import importlib.util
import io
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from agentrealm_agent import config, runner
from agentrealm_agent.brain import Memory
from agentrealm_agent.config import CharacterConfig, Policy
from agentrealm_agent.executor import step_landing
from agentrealm_agent.m6_acceptance import CALM_BUDGET_FRACTION, M6AcceptanceMetrics
from agentrealm_agent.world import WorldModel

REPO = Path(__file__).resolve().parents[2]
SMOKE_PATH = REPO / "scripts" / "smoke_m6_olympuff.py"


def load_smoke():
    spec = importlib.util.spec_from_file_location("smoke_m6_olympuff", SMOKE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Reads:
    def terrain(self, *a):
        return {"tick": 1}

    def tick(self, *a, **k):
        return {"tick": 1}


class M6AcceptanceMetricsTest(unittest.TestCase):
    def test_every_call_a_window_makes_is_counted(self):
        m = M6AcceptanceMetrics()
        client = m.wrap(Reads())
        client.terrain(1)
        client.tick(1, None)
        m.on_window(urgent=False)
        m.on_window(urgent=False)  # a skipped window: nothing sent
        client.tick(1, None)
        m.on_window(urgent=True)
        self.assertEqual((m.calm_windows, m.calm_calls, m.calm_tick_calls), (2, 2, 1))
        self.assertEqual(m.urgent_windows, 1)
        self.assertAlmostEqual(m.calm_call_fraction, 1.0)
        self.assertAlmostEqual(m.calm_tick_fraction, 0.5)

    def test_calm_tick_budget_is_strictly_under_a_quarter(self):
        m = M6AcceptanceMetrics(target_steps=0)
        client = m.wrap(Reads())
        for i in range(100):
            if i < int(100 * CALM_BUDGET_FRACTION):
                client.tick(1, None)
            m.on_window(urgent=False)
        self.assertTrue(m.failures(), "at the budget limit should fail")
        m.calm_tick_calls -= 1
        self.assertFalse(m.failures())

    def test_failures_for_steps_and_cooldown_on_steps_only(self):
        m = M6AcceptanceMetrics(target_steps=2)
        self.assertIn("steps", m.failures()[0])
        m.on_step_applied()
        m.on_step_applied()
        m.on_rejection("movement_cooldown", verb="Wait")
        self.assertFalse(m.failures())
        m.on_rejection("movement_cooldown", verb="Step")
        self.assertTrue(any("movement_cooldown" in f for f in m.failures()))

    def test_api_errors_are_counted_and_fail_the_run(self):
        # An ingest refusal (400) or a 429 never reaches on_rejection, but it
        # spent a request and is a client or budget fault the run must show.
        from agentrealm_agent.client import ApiError

        class Failing(Reads):
            def tick(self, *a, **k):
                raise ApiError(400, "malformed_intent")

        m = M6AcceptanceMetrics(target_steps=0)
        client = m.wrap(Failing())
        with self.assertRaises(ApiError):
            client.tick(1, [{"verb": "Say"}])
        m.on_window(urgent=False)
        self.assertEqual(m.calm_tick_calls, 1, "a failed request still spent the window")
        self.assertEqual(m.api_errors, ["tick 400 malformed_intent"])
        self.assertTrue(any("API error" in f for f in m.failures()))
        self.assertIn("API errors: 1", m.summary_lines())

    def test_stop_is_set_at_the_step_goal(self):
        stop = threading.Event()
        m = M6AcceptanceMetrics(target_steps=2, stop=stop)
        m.on_step_applied()
        self.assertFalse(stop.is_set())
        m.on_step_applied()
        self.assertTrue(stop.is_set())


class FakeMovementServer:
    """Runs queues one intent per tick and enforces movement_cooldown.

    Each window advances the server clock one tick. A Step closer than
    ``period`` ticks to the last applied one is rejected with
    movement_cooldown, which discards the rest of the queue.
    """

    def __init__(self, windows: int, stop: threading.Event, period: int = 4):
        self.tick_now = 100
        self.windows = windows
        self.stop = stop
        self.period = period
        self.pos = (0, 0)
        self.last_step: int | None = None
        self.queue: list[dict] = []
        self.queue_id: str | None = None
        self.start = 0  # tick the queue's first intent runs
        self.done = 0  # intents of the queue already resolved
        self.submits = 0
        self.results: list[dict] = []

    def wait(self, not_before: float = 0.0) -> None:
        self.windows -= 1
        if self.windows < 0:
            self.stop.set()
        self.tick_now += 1
        self._run_to(self.tick_now)

    def _run_to(self, now: int) -> None:
        while self.done < len(self.queue) and self.start + self.done <= now:
            i, t = self.done, self.start + self.done
            intent = self.queue[i]
            self.done += 1
            res = {"tick": t, "queue_id": self.queue_id, "index": i, "outcome": "applied"}
            if intent["verb"] == "Step":
                if self.last_step is not None and t - self.last_step < self.period:
                    res["outcome"] = "rejected"
                    res["rejection"] = {"category": "rules", "code": "movement_cooldown", "retryability": "transient"}
                    self.results.append(res)
                    self.queue, self.done = [], 0
                    return
                self.last_step = t
                self.pos = step_landing(self.pos, intent["direction"])
            self.results.append(res)

    def world(self, cid):
        return {"tick_rate_hz": 10, "code": "sandbox", "status": "live"}

    def terrain(self, cid, map_id, x0, y0, width, height):
        rows = ["d" * width for _ in range(height)]
        return {"tick": self.tick_now, "map_id": map_id, "x0": x0, "y0": y0, "width": width, "height": height,
                "legend": {"d": {"block_type": "dirt"}}, "rows": rows}

    def entities(self, cid, map_id, x0, y0, width, height):
        return {"tick": self.tick_now, "entities": []}

    def self_(self, cid):
        return {"perception_range": 25, "movement_range": 1, "alive": True, "lives": 3}

    def position(self, cid):
        return {"map_id": 7, "x": self.pos[0], "y": self.pos[1]}

    def zone(self, cid, map_id, x, y):
        return {"tick": self.tick_now, "safe": False}

    def tick(self, cid, intents, *, snapshot_version=None):
        results, self.results = self.results, []
        r = {"tick": self.tick_now, "window_remaining_ms": 0, "intent_results": results}
        if intents is not None:
            self.submits += 1
            self.queue_id = f"q{self.submits}"
            self.queue, self.start, self.done = list(intents), self.tick_now + 1, 0
            r["queue_id"] = self.queue_id
        if self.done < len(self.queue):
            r["queue"] = {"queue_id": self.queue_id, "next_index": self.done}
        return r


class RunnerCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.object(config, "STATE_DIR", Path(tmp.name))
        patch.start()
        self.addCleanup(patch.stop)

    def make_runner(self, client, stop: threading.Event, metrics=None) -> runner.Runner:
        cfg = CharacterConfig(
            "T",
            "sandbox",
            Policy(goals=["goto"], goto=(80, 0), pickup=False, entity_refresh=1000),
            Path("t.toml"),
        )
        if metrics is not None:
            client = metrics.wrap(client)
        r = runner.Runner(cfg, client, 1, stop, out=lambda _: None, acceptance=metrics)
        self.addCleanup(r.trace.close)
        w = WorldModel(character_id=1, map_id=7, pos=(0, 0), perception=25, tick=100)
        for y in range(-3, 4):
            for x in range(-30, 120):
                w.view.tiles[(x, y)] = "dirt"
        w.terrain_center, w.terrain_map, w.entities_tick = (0, 0), 7, 100
        r.world, r.mem = w, Memory(need_self=False, need_position=False)
        return r

    def run_against(self, server: FakeMovementServer, r: runner.Runner) -> None:
        with mock.patch.object(runner.Pacer, "wait_next_window", lambda _self, nb=0.0: server.wait(nb)):
            r.run()


class RunnerAcceptanceHookTest(RunnerCase):
    def test_walks_to_the_goal_without_cooldown_inside_the_calm_budget(self):
        stop = threading.Event()
        metrics = M6AcceptanceMetrics(target_steps=30, stop=stop)
        server = FakeMovementServer(2000, stop)
        self.run_against(server, self.make_runner(server, stop, metrics))
        self.assertGreaterEqual(metrics.steps_applied, 30)
        self.assertEqual(metrics.movement_cooldown_rejections, 0)
        self.assertGreater(server.windows, 0, "stopped at the goal, not when the windows ran out")
        self.assertLess(metrics.calm_tick_fraction, CALM_BUDGET_FRACTION)
        self.assertGreater(metrics.calm_calls, metrics.calm_tick_calls, "reads are counted too")
        self.assertFalse(metrics.failures())

    def test_movement_cooldown_rejections_are_counted(self):
        # A server slower than the agent's movement_speed rejects its Steps.
        stop = threading.Event()
        metrics = M6AcceptanceMetrics(target_steps=10, stop=stop)
        server = FakeMovementServer(300, stop, period=6)
        self.run_against(server, self.make_runner(server, stop, metrics))
        self.assertGreater(metrics.movement_cooldown_rejections, 0)
        self.assertIn("movement_cooldown", metrics.rejection_codes)
        self.assertTrue(any("movement_cooldown" in f for f in metrics.failures()))


class ScriptedServer:
    def __init__(self, ticks):
        self.ticks = list(ticks)
        self.sent = []

    def tick(self, cid, intents, *, snapshot_version=None):
        self.sent.append(intents)
        r = dict(self.ticks.pop(0))
        if intents is not None:
            r["queue_id"] = f"q{len(self.sent)}"
        return r


class WalkPacingTest(RunnerCase):
    """The next walk owes Waits counted to the server's tick, not local windows."""

    def walked(self, after_second_poll) -> list[str]:
        applied = [{"tick": 101 + i, "queue_id": "q1", "index": i, "outcome": "applied"} for i in range(5)]
        server = ScriptedServer([
            {"tick": 100},
            {"tick": 105, "intent_results": applied},
            {"tick": 110},
        ])
        r = self.make_runner(server, threading.Event())
        r.queue_horizon_ticks = 5
        r.tick()
        r.tick()
        self.assertEqual(r.mem.last_step_tick, 105)
        after_second_poll(r)
        r.tick()
        return [i["verb"] for i in server.sent[2]]

    def test_windows_counted_locally_do_not_shorten_the_owed_waits(self):
        def skipped_windows(r):
            r.world.tick += 3  # calm windows that sent nothing

        self.assertEqual(self.walked(skipped_windows), ["Wait", "Wait", "Wait", "Step"])

    def test_ticks_the_server_reported_count_toward_the_owed_waits(self):
        def terrain_read(r):
            r.world.tick += 3
            r.heard_tick(108)

        self.assertEqual(self.walked(terrain_read), ["Wait", "Step"])


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
            code = self.smoke.main(["--no-planner", *argv])  # offline: the planner test mode
        return code, out.getvalue(), err.getvalue()

    def test_no_api_key_exits_2(self):
        code, _, err = self.main([])
        self.assertEqual(code, 2)
        self.assertIn("AGENTREALM_API_KEY", err)

    def test_a_character_off_olympuff_exits_2(self):
        toml = self.tmp / "elsewhere.toml"
        toml.write_text(self.smoke.DEFAULT_PROFILE.read_text().replace('"olympuff"', '"sandbox"'))
        code, _, err = self.main(["--api-key", "k", "--character-id", "1", "--profile", str(toml)])
        self.assertEqual(code, 2)
        self.assertIn("olympuff", err)

    def test_missing_character_selection_exits_2(self):
        code, _, err = self.main(["--api-key", "k"])
        self.assertEqual(code, 2)
        self.assertIn("AGENTREALM_CHARACTER_ID", err)

    def exit_code_for(self, metrics):
        with mock.patch.object(self.smoke, "Client"), \
                mock.patch.object(self.smoke, "resolve_character_id", return_value=9), \
                mock.patch.object(self.smoke, "run_smoke", return_value=metrics):
            return self.main(["--api-key", "k", "--character-id", "9"])

    def test_exit_0_and_pass_when_the_criteria_hold(self):
        code, out, _ = self.exit_code_for(M6AcceptanceMetrics(target_steps=0))
        self.assertEqual(code, 0)
        self.assertIn("PASS", out)

    def test_exit_1_and_fail_when_a_criterion_misses(self):
        code, _, err = self.exit_code_for(M6AcceptanceMetrics(target_steps=200, steps_applied=10))
        self.assertEqual(code, 1)
        self.assertIn("FAIL", err)


if __name__ == "__main__":
    unittest.main()
