import json
import tempfile
import unittest
from pathlib import Path

from agentrealm_agent.run_metrics import LevelTimer, compute_metrics, metrics_from_trace, tick_trace_extras


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

    def test_level_timer_and_tick_extras(self):
        timer = LevelTimer()
        timer.note_map(7, 100, 1000.0)
        timer.note_map(7, 101, 1001.0)
        extra = tick_trace_extras(
            tick_response={"level_clear_ceremony": {"level_number": 1, "max_health_gain": 2}},
            gems=4,
            level_timer=timer,
            tick=500,
            now=1125.0,
        )
        self.assertEqual(extra["gems"], 4)
        self.assertEqual(extra["level_duration_s"], 125.0)
        self.assertEqual(extra["level_duration_ticks"], 400)


if __name__ == "__main__":
    unittest.main()
