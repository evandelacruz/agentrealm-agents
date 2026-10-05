"""A33: M10 acceptance metrics, odd-bush fixture, and smoke script (no server)."""

from __future__ import annotations

import importlib.util
import io
import random
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from agentrealm_agent import acceptance_smoke, config
from agentrealm_agent.break_memory import record_attempt
from agentrealm_agent.client import ApiError
from agentrealm_agent.config import Policy
from agentrealm_agent.directives import PARAM_DEFAULTS
from agentrealm_agent.investigation import mark_cell_read, mark_npc_spoken
from agentrealm_agent.item_table import InventorySupply
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.m10_acceptance import TARGET_SECONDS, M10AcceptanceMetrics, odd_block_opened
from agentrealm_agent.memory import Memory
from agentrealm_agent.brain import decide
from agentrealm_agent.states.intents import use_block
from agentrealm_agent.world import Entity, WorldModel
from tests.fixtures.navigation import grids, sim
from tests.test_m6_acceptance import FakeMovementServer, RunnerCase
from tests.test_m7_acceptance import OVERWORLD, load_smoke as load_m7_smoke

REPO = Path(__file__).resolve().parents[2]
SMOKE_PATH = REPO / "scripts" / "smoke_m10_olympuff.py"
ODD_BUSH_POS = (4, 2)


def load_smoke():
    spec = importlib.util.spec_from_file_location("smoke_m10_olympuff", SMOKE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def metrics(**kw) -> M10AcceptanceMetrics:
    return M10AcceptanceMetrics(**kw)


def open_world(pos=(0, 0), *, map_id=1) -> WorldModel:
    w = WorldModel(character_id=1, map_id=map_id, pos=pos, perception=5, tick=1)
    for y in range(-3, 4):
        for x in range(-3, 6):
            w.view.tiles[(x, y)] = "dirt"
    w.terrain_center, w.terrain_map = pos, map_id
    return w


def decide_tick(m, w, kb=None, mem=None, *, intents=[{"verb": "Wait"}]):
    m.before_tick(
        w,
        mem if mem is not None else Memory(),
        state="Explore",
        reason="explore",
        intents=intents,
        policy=Policy(hostile=["npc"]),
        params=dict(PARAM_DEFAULTS),
        knowledge=kb,
    )


class ReadableGateTest(unittest.TestCase):
    def test_unread_sign_in_sight_fails_on_a_full_run(self):
        m, kb = metrics(), KnowledgeBase("fixture")
        w = open_world()
        w.view.readable[(2, 0)] = True
        decide_tick(m, w, kb)
        self.assertEqual(m.missed_reads(kb), [(1, (2, 0))])
        self.assertTrue(any("never read" in f for f in m.failures(full_run=True, knowledge=kb)))

    def test_read_sign_passes(self):
        m, kb = metrics(), KnowledgeBase("fixture")
        w = open_world()
        w.view.readable[(2, 0)] = True
        decide_tick(m, w, kb)
        mark_cell_read(kb, 1, (2, 0))
        self.assertEqual(m.failures(full_run=True, knowledge=kb), [])

    def test_short_run_skips_unread_signs(self):
        m, kb = metrics(), KnowledgeBase("fixture")
        w = open_world()
        w.view.readable[(2, 0)] = True
        decide_tick(m, w, kb)
        self.assertNotIn("never read", " ".join(m.failures(full_run=False, knowledge=kb)))


class NpcGateTest(unittest.TestCase):
    def test_unspoken_npc_in_range_fails_on_a_full_run(self):
        m, kb = metrics(), KnowledgeBase("fixture")
        w = open_world()
        w.entities = [Entity("npc", 7, (3, 0), code="helper")]
        decide_tick(m, w, kb)
        self.assertEqual(m.missed_npcs(kb), [7])
        self.assertTrue(any("never spoken" in f for f in m.failures(full_run=True, knowledge=kb)))

    def test_spoken_npc_passes(self):
        m, kb = metrics(), KnowledgeBase("fixture")
        w = open_world()
        w.entities = [Entity("npc", 7, (3, 0), code="helper")]
        decide_tick(m, w, kb)
        mark_npc_spoken(kb, 7)
        self.assertEqual(m.failures(full_run=True, knowledge=kb), [])


class BreakDisciplineTest(unittest.TestCase):
    def break_tick(self, result, intents):
        m, kb, mem = metrics(), KnowledgeBase("fixture"), Memory()
        record_attempt(kb, map_id=1, pos=(2, 0), capability="cut", result=result, tick=1)
        mem.break_pending = (1, (2, 0), "cut")
        decide_tick(m, open_world(), kb, mem=mem, intents=intents)
        return m, kb

    def test_break_on_a_failed_pair_fails(self):
        m, kb = self.break_tick("applied_no_effect", [use_block((2, 0))])
        self.assertEqual(m.duplicate_break_attempts, 1)
        self.assertTrue(any("already failed" in f for f in m.failures(full_run=False, knowledge=kb)))

    def test_rebreaking_an_opened_pair_passes(self):
        # A cut bush regrows (GAME_NOTES Breaking blocks); cutting it again is not a repeat.
        m, kb = self.break_tick("opened", [use_block((2, 0))])
        self.assertEqual(m.duplicate_break_attempts, 0)
        self.assertEqual(m.failures(full_run=False, knowledge=kb), [])

    def test_a_held_queue_is_not_a_new_attempt(self):
        m, _ = self.break_tick("failed", None)
        self.assertEqual(m.duplicate_break_attempts, 0)

    def test_a_window_without_the_break_use_is_not_an_attempt(self):
        m, _ = self.break_tick("failed", [{"verb": "Wait"}])
        self.assertEqual(m.duplicate_break_attempts, 0)

    def test_odd_block_opened_by_any_capability(self):
        kb = KnowledgeBase("fixture")
        record_attempt(kb, map_id=1, pos=ODD_BUSH_POS, capability="blast", result="opened", tick=1)
        self.assertTrue(odd_block_opened(kb, 1, ODD_BUSH_POS))


class OddBushFixtureTest(unittest.TestCase):
    def test_opens_the_lone_bush(self):
        sc = grids.ODD_BUSH
        kb = KnowledgeBase("fixture")
        w = sim.world_for(sc)
        w.held_supplies = [InventorySupply(1, "bronze_sword")]
        w.armed_code = "bronze_sword"
        m = Memory()
        policy = sim.scripted(goals=["explore"])
        rng = random.Random(0)
        overlay: dict = {}
        for _ in range(200):
            if odd_block_opened(kb, 1, ODD_BUSH_POS):
                break
            d = decide(w, m, policy, rng, knowledge=kb)
            if d.intent is not None and d.intent.get("verb") == "Use":
                sim.apply_use(w, m, sc, d.intent, overlay, knowledge=kb)
            elif d.intent is not None and d.intent.get("verb") == "SetPosition":
                sim.apply(w, m, sc, (d.intent["x"], d.intent["y"]), overlay)
            w.tick += sim.TICKS_PER_DECISION
        self.assertTrue(odd_block_opened(kb, 1, ODD_BUSH_POS), "OddBreak should open the bush")

    def test_a_failed_capability_is_not_tried_again_on_the_bush(self):
        # A clue near the bush names smash, so the mallet goes first and fails on
        # it (the sim records applied_no_effect); the sword then cuts it. The
        # gate sees the agent's own queues, every window.
        sc = grids.ODD_BUSH
        kb = KnowledgeBase("fixture")
        kb.clues.append({"kind": "sign", "text": "smash it", "map_id": 1, "x": 4, "y": 1, "tick": 1})
        w = sim.world_for(sc)
        supplies = [InventorySupply(1, "bronze_sword"), InventorySupply(2, "bronze_mallet")]
        w.held_supplies = list(supplies)
        w.armed_code = "bronze_sword"
        m, gate = Memory(), metrics()
        policy = sim.scripted(goals=["explore"])
        rng = random.Random(0)
        overlay: dict = {}
        tried: list[str] = []
        for _ in range(200):
            if odd_block_opened(kb, 1, ODD_BUSH_POS):
                break
            d = decide(w, m, policy, rng, knowledge=kb)
            intents = d.submit_queue or ([d.intent] if d.intent is not None else None)
            decide_tick(gate, w, kb, mem=m, intents=intents)
            for intent in intents or []:
                verb = intent.get("verb")
                if verb == "Arm":
                    w.armed_code = next(s.code for s in supplies if s.id == intent["supply_id"])
                elif verb == "Use":
                    if m.break_pending is not None:
                        tried.append(m.break_pending[2])
                    sim.apply_use(w, m, sc, intent, overlay, knowledge=kb)
                elif verb == "SetPosition":
                    sim.apply(w, m, sc, (intent["x"], intent["y"]), overlay)
            w.tick += sim.TICKS_PER_DECISION
        self.assertEqual(gate.duplicate_break_attempts, 0)
        self.assertEqual(tried, ["smash", "cut"])
        self.assertTrue(odd_block_opened(kb, 1, ODD_BUSH_POS), "OddBreak should open the bush")


class DeathTest(unittest.TestCase):
    def test_a_death_fails_the_run_and_stops_it(self):
        stop = threading.Event()
        m = metrics(stop=stop)
        m.on_death()
        self.assertTrue(stop.is_set())
        self.assertTrue(any("death" in f for f in m.failures(full_run=False)))


class RunAcceptanceSmokeTest(unittest.TestCase):
    def test_wires_stop_and_returns_the_runners_knowledge_when_the_save_fails(self):
        seen = {}

        class FakeRunner:
            def __init__(self, cfg, client, cid, stop, emit, *, knowledge, acceptance):
                seen["kb"], seen["stop"], seen["metrics"] = knowledge, stop, acceptance

            def run(self):
                seen["wired"] = seen["metrics"].stop is seen["stop"]
                seen["stop"].set()

        kb = KnowledgeBase("olympuff")
        cfg = config.load(REPO / "python" / "characters" / "olympuff_m10.toml")
        lines: list[str] = []
        with mock.patch.object(acceptance_smoke, "Runner", FakeRunner), \
                mock.patch.object(acceptance_smoke, "load_knowledge", side_effect=[kb, KnowledgeBase("olympuff")]), \
                mock.patch.object(acceptance_smoke, "save_knowledge", side_effect=OSError("disk full")):
            elapsed, knowledge = acceptance_smoke.run_acceptance_smoke(
                object(), cfg, 9, metrics(), timeout_s=0, out=lines.append
            )
        self.assertTrue(seen["wired"], "metrics.stop must be the runner's stop event")
        self.assertIs(knowledge, kb)
        self.assertGreaterEqual(elapsed, 0.0)
        self.assertTrue(any("not saved" in line for line in lines))


class ApiErrorTest(unittest.TestCase):
    def test_api_errors_fail_the_run(self):
        class Reads:
            def tick(self, *a, **k):
                raise ApiError(429, "rate_limited")

        m = metrics()
        client = m.wrap(Reads())
        with self.assertRaises(ApiError):
            client.tick(1, None)
        self.assertTrue(any("API error" in f for f in m.failures(full_run=False)))


class RunnerHookTest(RunnerCase):
    def test_stops_when_the_clock_runs_out(self):
        stop = threading.Event()
        ticks = iter(range(10**6))
        m = metrics(target_seconds=600, stop=stop, clock=lambda: float(next(ticks)))

        class Server(FakeMovementServer):
            def world(self, cid):
                return {**super().world(cid), "town": {"map_id": OVERWORLD, "x": 0, "y": 0}}

        server = Server(5000, stop)
        self.run_against(server, self.make_runner(server, stop, m))
        self.assertTrue(stop.is_set())


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
        """``played(metrics, kb)`` stands in for the run; ``kb`` is the runner's in-memory one."""

        def run_smoke(client, cfg, cid, metrics, *, timeout_s, out=None):
            kb = KnowledgeBase("olympuff")
            played(metrics, kb)
            return seconds, kb

        with mock.patch.object(self.smoke, "Client") as Client, \
                mock.patch.object(self.smoke, "resolve_character_id", return_value=9), \
                mock.patch.object(self.smoke.time, "sleep"), \
                mock.patch.object(self.smoke, "run_acceptance_smoke", side_effect=run_smoke):
            Client.return_value.self_.return_value = {"alive": True}
            return self.main(["--api-key", "k", "--character-id", "9", "--seconds", str(seconds)])

    def test_no_api_key_exits_2(self):
        code, _, err = self.main([])
        self.assertEqual(code, 2)
        self.assertIn("AGENTREALM_API_KEY", err)

    def test_short_run_passes_without_read_coverage(self):
        code, out, _ = self.run_main(10, lambda m, kb: None)
        self.assertEqual(code, 0)
        self.assertIn("PASS", out)

    def test_full_run_fails_without_reads_and_speech(self):
        def played(m, kb):
            w = open_world()
            w.view.readable[(1, 0)] = True
            w.entities = [Entity("npc", 3, (2, 0))]
            decide_tick(m, w, kb)

        code, _, err = self.run_main(int(TARGET_SECONDS), played)
        self.assertEqual(code, 1)
        self.assertIn("never read", err)
        self.assertIn("never spoken", err)

    def test_full_run_is_judged_on_the_runners_knowledge(self):
        # Nothing is saved to disk here: a reload would miss the read and the Say.
        def played(m, kb):
            w = open_world()
            w.view.readable[(1, 0)] = True
            w.entities = [Entity("npc", 3, (2, 0))]
            decide_tick(m, w, kb)
            mark_cell_read(kb, 1, (1, 0))
            mark_npc_spoken(kb, 3)

        code, out, err = self.run_main(int(TARGET_SECONDS), played)
        self.assertEqual(code, 0, err)
        self.assertIn("PASS", out)

    def test_wake_is_shared_with_m7(self):
        m7 = load_m7_smoke()
        self.assertIs(self.smoke.wake, m7.wake)


if __name__ == "__main__":
    unittest.main()
