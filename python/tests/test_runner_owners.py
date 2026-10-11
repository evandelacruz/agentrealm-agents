"""The runner is the loop (A91): each rule it used to carry lives with its owner.

- Hostile types and posts carried across runs: ``knowledge_base.hostiles``.
- Where a held queue may have taken us, and whether an answer asks for what
  it already does: ``executor.held_queue``.
- The held-queue probe leaves memory as it was unless its answer is sent:
  ``dispatch.probe_held_queue``.
- How a call reads in the console and the trace file: ``trace``.
"""

from __future__ import annotations

import ast
import json
import random
import tempfile
import unittest
from pathlib import Path

from agentrealm_agent.executor.held_queue import held_queue_matches, possible_positions
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.knowledge_base.hostiles import load_hostiles, save_hostiles
from agentrealm_agent.memory import Memory
from agentrealm_agent.plan import Plan
from agentrealm_agent.states.dispatch import PROBE_NEVER_KEEPS, probe_held_queue
from agentrealm_agent.trace import Trace, fmt_submit, oscillation_detail, tick_detail
from agentrealm_agent.world import WorldModel

RUNNER_SOURCE = Path(__file__).resolve().parents[1] / "agentrealm_agent" / "runner.py"


def held_walk(w: WorldModel, m: Memory, steps: list[str], sent_tick: int) -> None:
    m.pending_intents = [{"verb": "Step", "direction": d} for d in steps]
    m.pending_next_index = 0
    m.queue_sent_tick = sent_tick
    w.pos = (5, 5)


class PossiblePositionsTest(unittest.TestCase):
    def test_only_steps_elapsed_ticks_could_run_count(self):
        w, m = WorldModel(1), Memory()
        held_walk(w, m, ["right", "right", "right", "right"], sent_tick=10)
        w.tick = 11  # two ticks: the queue's first two Steps may have run
        self.assertEqual(possible_positions(w, m), [(5, 5), (6, 5), (7, 5)])

    def test_no_held_queue_is_the_tracked_position(self):
        w, m = WorldModel(1), Memory()
        w.pos = (2, 3)
        self.assertEqual(possible_positions(w, m), [(2, 3)])


class HeldQueueMatchesTest(unittest.TestCase):
    def test_a_held_take_asked_again_matches_waits_aside(self):
        w, m = WorldModel(1), Memory()
        m.pending = {"verb": "Take", "supply_id": 7}
        again = [{"verb": "Wait"}, {"verb": "Take", "supply_id": 7}]
        self.assertTrue(held_queue_matches(w, m, again[1], again))
        self.assertFalse(held_queue_matches(w, m, {"verb": "Take", "supply_id": 8}, None))

    def test_a_walk_to_the_cell_the_next_step_enters_matches(self):
        w, m = WorldModel(1), Memory()
        held_walk(w, m, ["up", "up"], sent_tick=0)
        self.assertTrue(held_queue_matches(w, m, {"verb": "SetPosition", "x": 5, "y": 4}, None))
        self.assertFalse(held_queue_matches(w, m, {"verb": "SetPosition", "x": 6, "y": 5}, None))


class ProbeHeldQueueTest(unittest.TestCase):
    def setUp(self):
        self.m = Memory()
        self.rng = random.Random(1)
        self.plan = Plan([], {})

    def probe(self, sends: bool):
        def decide():
            self.m.state = "Fight"
            self.m.gather_status = "changed by the probe"
            self.m.nav_stuck = "probe only"
            self.rng.random()
            return "answer"

        return probe_held_queue(self.m, self.rng, self.plan, decide, lambda _d: sends)

    def test_an_answer_not_sent_leaves_memory_and_rng_as_they_were(self):
        before_rng = self.rng.getstate()
        self.assertIsNone(self.probe(sends=False))
        self.assertEqual(self.m.state, "")
        self.assertEqual(self.m.gather_status, "")
        self.assertNotEqual(self.m.nav_stuck, "probe only")
        self.assertEqual(self.rng.getstate(), before_rng)

    def test_a_sent_answer_keeps_memory_but_never_the_probe_never_keeps_fields(self):
        self.assertEqual(self.probe(sends=True), "answer")
        self.assertEqual(self.m.state, "Fight")
        self.assertIn("nav_stuck", PROBE_NEVER_KEEPS)
        self.assertNotEqual(self.m.nav_stuck, "probe only")


class TraceTest(unittest.TestCase):
    def test_a_call_is_one_console_line_and_one_json_record(self):
        lines: list[str] = []
        w = WorldModel(1)
        w.tick, w.map_id, w.pos = 42, 3, (4, 5)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "fake.1.trace.jsonl"
            trace = Trace(path, "fake", lines.append)
            trace.log(w, "tick", "Wait (idle)", {"reason": "idle"})
            trace.close()
            record = json.loads(path.read_text())
        self.assertEqual(lines, ["[fake] t=42 @3:4,5 tick     Wait (idle)"])
        self.assertEqual((record["tick"], record["map"], record["call"], record["reason"]), (42, 3, "tick", "idle"))

    def test_tick_and_oscillation_lines(self):
        queue = [{"verb": "Wait"}, {"verb": "Step", "direction": "right"}, {"verb": "Step", "direction": "right"}]
        self.assertEqual(fmt_submit(queue), "queue 2×Step 1×Wait")
        self.assertEqual(fmt_submit(None, {"verb": "Take", "supply_id": 9}), "Take(9)")
        self.assertEqual(
            tick_detail(None, None, "queue held", [{"kind": "Damaged", "amount": 2, "source_kind": "npc"}], 1),
            "— (queue held) | Damaged 2 by npc | dropped 1",
        )
        event = {"cells": [[1, 1], [1, 2]], "goal": "explore", "target": [9, 9]}
        self.assertEqual(oscillation_detail(event), "pacing [[1, 1], [1, 2]]: gave up explore → (9, 9)")


class HostilesRoundTripTest(unittest.TestCase):
    def test_a_hostile_type_learned_is_known_to_the_next_run(self):
        kb = KnowledgeBase.empty("sandbox")
        w = WorldModel(1)
        w.hostile_types.add(("npc", "fake_biter"))
        save_hostiles(kb, w)
        fresh = WorldModel(1)
        load_hostiles(kb, fresh)
        self.assertIn(("npc", "fake_biter"), fresh.hostile_types)


class RunnerIsTheLoopTest(unittest.TestCase):
    def test_runner_carries_none_of_the_moved_rules(self):
        src = RUNNER_SOURCE.read_text()
        for gone in ("def possible_positions", "def held_queue_matches", "def steps_land_clear", "PROBE_NEVER_KEEPS =", "def _fmt_", "json.dumps"):
            self.assertNotIn(gone, src)

    def test_the_executor_imports_nothing_from_states(self):
        for path in (RUNNER_SOURCE.parent / "executor").glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.ImportFrom):
                    modules = [node.module or ""] + [f"{node.module or ''}.{a.name}" for a in node.names]
                elif isinstance(node, ast.Import):
                    modules = [a.name for a in node.names]
                else:
                    continue
                for module in modules:
                    self.assertNotIn("states", module.split("."), f"{path.name}:{node.lineno} imports {module}")


if __name__ == "__main__":
    unittest.main()
