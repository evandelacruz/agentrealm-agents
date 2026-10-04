import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentrealm_agent import __main__ as cli, config
from agentrealm_agent.run_metrics import (
    LevelTimer,
    compare_run_metrics,
    compute_metrics,
    load_metrics_source,
    metrics_from_trace,
    tick_trace_extras,
)

OVERWORLD = 1


class RunMetricsTest(unittest.TestCase):
    def test_compute_metrics_from_trace_lines(self):
        lines = [
            {"call": "tick", "events": [{"kind": "NPCDied"}], "gems": 3},
            {
                "call": "tick",
                "events": [{"kind": "Died"}, {"kind": "NPCDied"}],
                "gems": 10,
                "level_clear_ceremony": {"level_number": 2, "max_health_gain": 5},
                "level_duration_s": 120.5,
            },
            {"call": "tick", "gems": 12},
        ]
        m = compute_metrics(lines)
        self.assertEqual(m.deaths, 1)
        self.assertEqual(m.kills, 2)
        self.assertEqual(m.gems, 12)
        self.assertEqual(m.levels_cleared, 1)
        self.assertEqual(m.time_per_level[2], 120.5)

    def test_metrics_from_trace_file(self):
        payload = [
            {"call": "world"},
            {"call": "tick", "events": [], "gems": 0},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wren.trace.jsonl"
            path.write_text("\n".join(json.dumps(x) for x in payload) + "\n", encoding="utf-8")
            m = metrics_from_trace(path)
        self.assertEqual(m.gems, 0)
        self.assertEqual(m.deaths, 0)

    def _clear(self, timer, tick, now):
        return tick_trace_extras(
            tick_response={"level_clear_ceremony": {"level_number": 1, "max_health_gain": 2}},
            gems=4,
            level_timer=timer,
            tick=tick,
            now=now,
        )

    def test_level_time_spans_every_map_of_the_level(self):
        timer = LevelTimer(OVERWORLD)
        timer.note_map(OVERWORLD, 90, 990.0)
        timer.note_map(7, 100, 1000.0)  # through the entrance door
        timer.note_map(8, 300, 1060.0)  # a second map of the same level
        timer.note_map(9, 450, 1100.0)  # the boss room
        extra = self._clear(timer, 500, 1125.0)
        self.assertEqual(extra["gems"], 4)
        self.assertEqual(extra["level_duration_s"], 125.0)
        self.assertEqual(extra["level_duration_ticks"], 400)

    def test_level_time_restarts_only_on_leaving_the_overworld_again(self):
        timer = LevelTimer(OVERWORLD)
        timer.note_map(OVERWORLD, 0, 0.0)
        timer.note_map(7, 10, 10.0)
        self._clear(timer, 20, 20.0)
        timer.note_map(9, 20, 20.0)  # stale boss-room map read in the clear tick
        self.assertEqual(timer.duration(30.0, 30), (None, None))
        timer.note_map(OVERWORLD, 21, 21.0)  # moved outside
        timer.note_map(12, 40, 40.0)  # the next level
        timer.note_map(13, 50, 50.0)
        self.assertEqual(timer.duration(60.0, 60), (20.0, 20))

    def test_a_run_that_starts_inside_a_level_has_no_time_for_it(self):
        timer = LevelTimer(OVERWORLD)
        timer.note_map(8, 100, 1000.0)
        self.assertNotIn("level_duration_s", self._clear(timer, 200, 1100.0))

    def test_a_truncated_line_is_skipped_and_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wren.trace.jsonl"
            path.write_text(
                json.dumps({"call": "tick", "events": [{"kind": "NPCDied"}]}) + "\n" + '{"call": "tick", "ev',
                encoding="utf-8",
            )
            m = metrics_from_trace(path)
        self.assertEqual(m.kills, 1)
        self.assertEqual(m.bad_lines, 1)

    def test_only_the_last_run_counts(self):
        lines = [
            {"call": "world", "world": {"code": "sandbox"}},
            {"call": "tick", "events": [{"kind": "Died"}, {"kind": "NPCDied"}], "gems": 9},
            {"call": "world", "world": {"code": "sandbox"}},
            {"call": "world", "error": {"status": 503, "code": "x"}},
            {"call": "tick", "events": [{"kind": "NPCDied"}]},
        ]
        m = compute_metrics(lines)
        self.assertEqual((m.deaths, m.kills, m.gems), (0, 1, None))


class MetricsCliTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        patch = mock.patch.object(config, "STATE_DIR", self.dir)
        patch.start()
        self.addCleanup(patch.stop)
        self.toml = self.dir / "wren.toml"
        self.toml.write_text('name = "wren"\navatar = "default"\nmodel_agent = "test"\n', encoding="utf-8")

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict("os.environ", {"AGENTREALM_API_KEY": ""}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_metrics_needs_no_api_key(self):
        (self.dir / "wren.trace.jsonl").write_text(json.dumps({"call": "tick", "gems": 3}) + "\n", encoding="utf-8")
        rc, out, _ = self.main("metrics", str(self.toml))
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("wren: "))
        self.assertEqual(json.loads(out[len("wren: "):])["gems"], 3)

    def test_missing_trace_fails(self):
        rc, out, err = self.main("metrics", str(self.toml))
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("no trace", err)

    def test_other_commands_still_need_a_key(self):
        rc, _, err = self.main("status", str(self.toml))
        self.assertEqual(rc, 2)
        self.assertIn("AGENTREALM_API_KEY", err)

    def test_compare_metrics_cli(self):
        base = {"call": "tick", "events": [{"kind": "Died"}], "gems": 1}
        cand = {"call": "tick", "events": [{"kind": "Died"}, {"kind": "NPCDied"}], "gems": 4}
        trace_base = self.dir / "base.trace.jsonl"
        trace_cand = self.dir / "cand.trace.jsonl"
        trace_base.write_text(json.dumps(base) + "\n", encoding="utf-8")
        trace_cand.write_text(json.dumps(cand) + "\n", encoding="utf-8")
        rc, out, _ = self.main("compare-metrics", str(trace_base), str(trace_cand))
        self.assertEqual(rc, 0)
        diff = json.loads(out)
        self.assertEqual(diff["deaths"], 0)
        self.assertEqual(diff["kills"], 1)
        self.assertEqual(diff["gems"], 3)


class CompareRunMetricsTest(unittest.TestCase):
    def test_compare_run_metrics_deltas(self):
        base = compute_metrics([{"call": "tick", "events": [{"kind": "Died"}], "gems": 2}])
        cand = compute_metrics(
            [
                {
                    "call": "tick",
                    "events": [{"kind": "NPCDied"}],
                    "gems": 5,
                    "level_clear_ceremony": {"level_number": 1},
                    "level_duration_s": 90.0,
                }
            ]
        )
        diff = compare_run_metrics(base, cand)
        self.assertEqual(diff["deaths"], -1)
        self.assertEqual(diff["kills"], 1)
        self.assertEqual(diff["gems"], 3)
        self.assertEqual(diff["time_per_level"]["1"], 90.0)

    def test_load_metrics_from_cli_capture(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wren.metrics.txt"
            path.write_text('wren: {"deaths": 2, "kills": 1, "gems": 0, "levels_cleared": 0, "time_per_level": {}, "bad_lines": 0}\n', encoding="utf-8")
            m = load_metrics_source(path)
        self.assertEqual(m.deaths, 2)


if __name__ == "__main__":
    unittest.main()
