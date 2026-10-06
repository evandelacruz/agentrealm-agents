"""A29: M9 acceptance metrics and smoke script (no server)."""

from __future__ import annotations

import importlib.util
import io
import json
import math
import tempfile
import threading
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from agentrealm_agent import acceptance_smoke, config
from agentrealm_agent.config import Policy
from agentrealm_agent.directives import PARAM_DEFAULTS, load_directives
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.m9_acceptance import (
    M9AcceptanceMetrics,
    clear_entrance_looks,
    entrance_row_recorded,
    missing_entrance_marks,
    required_entrance_marks,
)
from agentrealm_agent.memory import Memory
from agentrealm_agent.travel.knowledge import sync_entrances, sync_town
from agentrealm_agent.travel.strength import StrengthBracket
from agentrealm_agent.world import WorldModel
from tests.test_m7_acceptance import FakeSleeper, STEP

REPO = Path(__file__).resolve().parents[2]
SMOKE_PATH = REPO / "scripts" / "smoke_m9_olympuff.py"


def load_smoke():
    spec = importlib.util.spec_from_file_location("smoke_m9_olympuff", SMOKE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def kb_with_entrances() -> KnowledgeBase:
    kb = KnowledgeBase.from_dict("sandbox", {})
    sync_entrances(kb, {"maps": [{"map_id": 7, "entrances": [{"x": 10, "y": 20}, {"x": 30, "y": 40}]}]})
    sync_town(kb, {"map_id": 7, "x": 0, "y": 0})
    return kb


def decide(m, w, kb, mem=None, *, state="Explore", reason="explore", intents=STEP):
    m.before_tick(
        w,
        mem or Memory(),
        state=state,
        reason=reason,
        intents=intents,
        policy=Policy(hostile=["npc"]),
        params=dict(PARAM_DEFAULTS),
        knowledge=kb,
    )


class EntranceCatalogTest(unittest.TestCase):
    def test_required_skips_strength_closed_cells(self):
        kb = kb_with_entrances()
        bracket = StrengthBracket(closed={(7, (30, 40))})
        self.assertEqual(required_entrance_marks(kb, bracket), {(7, (10, 20))})

    def test_recorded_requires_looked_block_type(self):
        self.assertFalse(entrance_row_recorded({}))
        self.assertFalse(entrance_row_recorded({"looked": True}))
        self.assertTrue(entrance_row_recorded({"looked": True, "block_type": "framed_door"}))
        self.assertFalse(
            entrance_row_recorded({"looked": True, "block_type": "framed_door", "locked": True}),
        )
        self.assertTrue(
            entrance_row_recorded(
                {"looked": True, "block_type": "framed_door", "locked": True, "needs": "key"},
            ),
        )


class M9GateTest(unittest.TestCase):
    def open_world(self, pos=(0, 0), map_id=7) -> WorldModel:
        w = WorldModel(character_id=1, map_id=map_id, pos=pos, perception=5, tick=1)
        for y in range(-2, 3):
            for x in range(-2, 3):
                w.view.tiles[(x, y)] = "dirt"
        return w

    def test_fails_until_every_required_entrance_is_recorded(self):
        kb = kb_with_entrances()
        m = M9AcceptanceMetrics()
        decide(m, self.open_world(), kb)
        self.assertEqual(m.catalog_size, 2)
        self.assertFalse(m.entrances_ok())
        self.assertTrue(any("not looked" in f for f in m.failures()))

    def test_passes_when_all_recorded_and_on_town(self):
        kb = kb_with_entrances()
        m = M9AcceptanceMetrics()
        decide(m, self.open_world((3, 3)), kb)
        with kb.lock:
            for key in list(kb.entrances):
                kb.entrances[key].update({"looked": True, "block_type": "grass"})
        decide(m, self.open_world((0, 0), map_id=7), kb)
        self.assertTrue(m.entrances_ok())
        self.assertTrue(m.at_town_end)
        self.assertEqual(m.failures(), [])

    def record(self, kb, *keys):
        with kb.lock:
            for key in keys or list(kb.entrances):
                kb.entrances[key].update({"looked": True, "block_type": "grass"})

    def test_failure_counts_the_marks_the_gate_still_misses(self):
        kb = kb_with_entrances()
        m = M9AcceptanceMetrics()
        decide(m, self.open_world(), kb)
        self.record(kb, "7:10,20")
        with kb.lock:
            kb.entrances["7:30,40"]["looked"] = True  # looked, but no block_type filed
        decide(m, self.open_world(), kb)
        self.assertEqual(m.entrances_recorded, 1)
        self.assertIn("1 entrance mark(s) not looked and recorded this run", m.failures())

    def test_looks_from_an_earlier_run_do_not_count(self):
        kb = kb_with_entrances()
        self.record(kb)  # persisted knowledge base: every mark already looked
        calls = []
        stop = threading.Event()
        m = M9AcceptanceMetrics(stop=stop, on_entrances_done=lambda: calls.append(1))
        m.snapshot(kb)
        decide(m, self.open_world((0, 0)), kb)
        self.assertEqual((m.entrances_recorded, len(m.pre_recorded)), (0, 2))
        self.assertFalse(m.entrances_ok())
        self.assertEqual(calls, [])
        self.assertFalse(stop.is_set())
        self.assertIn("2 entrance mark(s) not looked and recorded this run", m.failures())
        decide(m, self.open_world((0, 0)), kb)
        self.assertEqual(m.entrances_recorded, 0, "a pre-recorded mark stays uncounted")

    def test_a_look_made_by_the_first_decision_counts(self):
        # before_tick runs after the decision, so Investigate can record a
        # mark before the gate's first look at the knowledge base.
        kb = kb_with_entrances()
        m = M9AcceptanceMetrics()
        m.snapshot(kb)
        self.record(kb, "7:10,20")
        decide(m, self.open_world((3, 3)), kb)
        self.assertEqual((m.entrances_recorded, m.pre_recorded), (1, set()))
        self.record(kb, "7:30,40")
        decide(m, self.open_world((0, 0)), kb)
        self.assertTrue(m.entrances_ok())

    def test_a_mark_reopened_by_the_bracket_counts_once_looked(self):
        kb = kb_with_entrances()
        mem = Memory()
        mem.strength = StrengthBracket(closed={(7, (30, 40))})
        m = M9AcceptanceMetrics()
        m.snapshot(kb)
        self.record(kb, "7:10,20")
        decide(m, self.open_world((3, 3)), kb, mem)
        self.record(kb, "7:30,40")  # looked while closed: no route, but a look is a look
        mem.strength = StrengthBracket()  # loadout change reopens it
        decide(m, self.open_world((0, 0)), kb, mem)
        self.assertEqual((m.catalog_size, m.entrances_recorded), (2, 2))
        self.assertTrue(m.entrances_ok())

    def test_cleared_looks_count_once_looked_again(self):
        kb = kb_with_entrances()
        self.record(kb)
        with kb.lock:
            kb.entrances["7:10,20"].update({"locked": True, "needs": "key"})
        self.assertEqual(clear_entrance_looks(kb), 2)
        with kb.lock:
            row = kb.entrances["7:10,20"]
            self.assertNotIn("looked", row)
            self.assertNotIn("block_type", row)
            self.assertNotIn("needs", row)
        m = M9AcceptanceMetrics()
        m.snapshot(kb)
        self.assertEqual(m.pre_recorded, set())
        decide(m, self.open_world((3, 3)), kb)
        self.record(kb, "7:30,40")
        with kb.lock:
            kb.entrances["7:10,20"].update({"looked": True, "block_type": "framed_door", "needs": "key"})
        decide(m, self.open_world((0, 0)), kb)
        self.assertTrue(m.entrances_ok())
        self.assertEqual(m.failures(), [])

    def test_leaving_town_after_a_visit_fails_town_at_end(self):
        kb = kb_with_entrances()
        self.record(kb)
        m = M9AcceptanceMetrics()
        decide(m, self.open_world((0, 0)), kb)
        self.assertTrue(m.at_town_end)
        decide(m, self.open_world((1, 0)), kb)
        self.assertFalse(m.at_town_end)
        self.assertIn("character not on town at end", m.failures())

    def test_town_on_another_map_is_not_town(self):
        kb = kb_with_entrances()
        self.record(kb)
        m = M9AcceptanceMetrics()
        decide(m, self.open_world((0, 0), map_id=8), kb)
        self.assertFalse(m.at_town_end)

    def test_entrances_done_fires_once_and_stop_waits_for_town(self):
        kb = kb_with_entrances()
        calls = []
        stop = threading.Event()
        m = M9AcceptanceMetrics(stop=stop, on_entrances_done=lambda: calls.append(1))
        decide(m, self.open_world((5, 5)), kb)
        self.assertEqual(calls, [], "catalog not complete yet")
        self.record(kb)
        decide(m, self.open_world((5, 5)), kb)
        decide(m, self.open_world((4, 4)), kb)
        self.assertEqual(calls, [1], "the town hand-off is sent once")
        self.assertFalse(stop.is_set(), "entrances done but not on town")
        decide(m, self.open_world((0, 0)), kb)
        self.assertTrue(stop.is_set())

    def test_town_alone_does_not_stop_before_entrances(self):
        kb = kb_with_entrances()
        stop = threading.Event()
        m = M9AcceptanceMetrics(stop=stop)
        decide(m, self.open_world((0, 0)), kb)
        self.assertFalse(stop.is_set())

    def test_all_marks_over_strength_is_not_a_missing_catalog(self):
        kb = kb_with_entrances()
        mem = Memory()
        mem.strength = StrengthBracket(closed={(7, (10, 20)), (7, (30, 40))})
        m = M9AcceptanceMetrics()
        decide(m, self.open_world((0, 0)), kb, mem)
        self.assertEqual((m.marks_known, m.catalog_size, m.strength_closed_skipped), (2, 0, 2))
        self.assertTrue(m.entrances_ok())
        self.assertEqual(m.failures(), [])

    def test_no_minimap_marks_fails_as_no_catalog(self):
        kb = KnowledgeBase.from_dict("sandbox", {})
        sync_town(kb, {"map_id": 7, "x": 0, "y": 0})
        m = M9AcceptanceMetrics()
        decide(m, self.open_world((0, 0)), kb)
        self.assertFalse(m.entrances_ok())
        self.assertIn("no entrance catalog from minimap", m.failures())

    def test_survival_failures_match_m7(self):
        m = M9AcceptanceMetrics(stop_on_death=True)
        m.on_death()
        self.assertIn("1 death(s)", m.failures(full_run=False)[0])


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

    def run_main(self, seconds: float, played):
        def run_smoke(client, cfg, cid, metrics, *, timeout_s, planner=None):
            played(metrics)
            return metrics, seconds

        with mock.patch.object(self.smoke, "Client") as Client, \
                mock.patch.object(self.smoke, "resolve_character_id", return_value=9), \
                mock.patch.object(self.smoke.time, "sleep"), \
                mock.patch.object(self.smoke, "run_smoke", side_effect=run_smoke):
            client = Client.return_value
            client.world.return_value = {"town": {"map_id": 7, "x": 0, "y": 0}}
            client.position.return_value = {"map_id": 7, "x": 10, "y": 20}
            client.self_.return_value = {"alive": True}
            return self.main(["--api-key", "k", "--character-id", "9", "--seconds", str(seconds)])

    def test_short_run_passes_without_entrance_catalog(self):
        code, out, _ = self.run_main(60, lambda m: None)
        self.assertEqual(code, 0)
        self.assertIn("PASS", out)

    def test_full_run_fails_without_entrances_or_town(self):
        code, _, err = self.run_main(7200, lambda m: None)
        self.assertEqual(code, 1)
        self.assertIn("entrance", err.lower())

    def test_full_run_passes_when_gate_is_met(self):
        def played(m):
            m.marks_known, m.catalog_size, m.entrances_recorded, m.at_town_end = 3, 3, 3, True

        code, out, _ = self.run_main(7200, played)
        self.assertEqual(code, 0, out)

    def test_run_smoke_sends_town_on_catalog_done_and_restores_directives(self):
        profile = self.tmp / "olympuff_m9.toml"
        profile.write_text(self.smoke.DEFAULT_PROFILE.read_text())
        cfg = config.load(profile)
        cfg.directives_path.write_text('goals = ["gather_gems"]\n')
        seen = []

        class FakeRunner:
            def __init__(self, cfg, client, cid, stop, out, *, knowledge, acceptance, strategist=None):
                self.stop, self.metrics = stop, acceptance

            def run(self):
                self.metrics.on_entrances_done()
                seen.append(load_directives(cfg.directives_path).goals)

        with mock.patch.object(acceptance_smoke, "Runner", FakeRunner), \
                mock.patch.object(self.smoke, "clear_entrance_looks", return_value=0) as clear, \
                redirect_stdout(io.StringIO()):
            metrics = M9AcceptanceMetrics()
            order = []
            clear.side_effect = lambda kb: order.append("clear") or 0
            metrics.snapshot = lambda kb: order.append("snapshot")
            self.smoke.run_smoke(mock.Mock(), cfg, 9, metrics, timeout_s=0)
        self.assertEqual(order, ["clear", "snapshot"], "snapshot after clearing, before the runner")
        self.assertEqual(seen, [["gather_gems", "travel:town"]])
        self.assertEqual(cfg.directives_path.read_text(), 'goals = ["gather_gems"]\n')

    def test_main_wakes_before_run(self):
        with mock.patch.object(self.smoke, "Client") as Client, \
                mock.patch.object(self.smoke, "resolve_character_id", return_value=9), \
                mock.patch.object(self.smoke.time, "sleep"), \
                mock.patch.object(self.smoke, "run_smoke", side_effect=lambda c, cfg, cid, m, **kw: (m, 10)):
            fake = FakeSleeper()
            Client.return_value = fake
            code, _, _ = self.main(["--api-key", "k", "--character-id", "9", "--seconds", "60"])
        self.assertEqual(code, 0)
        self.assertEqual(fake.ticks, [[{"verb": "Wait"}]])


class TownHandoffTest(unittest.TestCase):
    def setUp(self):
        self.smoke = load_smoke()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "olympuff_m9.directives.toml"

    def test_send_to_town_keeps_params_and_replaces_travel_ops(self):
        self.path.write_text(
            'goals = ["travel:entrance:7:10:20", "gather_gems"]\nnever_attack = ["sheep"]\n'
            'instructions = "be \\"careful\\""\n[params]\nrisk = 0.25\n'
        )
        self.smoke.send_to_town(self.path)
        d = load_directives(self.path)
        self.assertEqual(d.goals, ["gather_gems", "travel:town"])
        self.assertEqual(d.never_attack, ["sheep"])
        self.assertEqual(d.instructions, 'be "careful"')
        self.assertEqual(d.params["risk"], 0.25)

    def test_send_to_town_keeps_non_bmp_text(self):
        self.path.write_text(
            'goals = ["gather \U0001F48E"]\ninstructions = "stay safe \U0001F6E1"\n', encoding="utf-8"
        )
        self.smoke.send_to_town(self.path)
        d = load_directives(self.path)
        self.assertEqual(d.goals, ["gather \U0001F48E", "travel:town"])
        self.assertEqual(d.instructions, "stay safe \U0001F6E1")

    def test_send_to_town_escapes_control_characters(self):
        text = "a\x7fb\x00c\x1fd\te\nf\\g\"h"
        self.path.write_text(f'goals = ["x"]\ninstructions = {json.dumps(text)}\n', encoding="utf-8")
        self.smoke.send_to_town(self.path)
        self.assertEqual(load_directives(self.path).instructions, text)
        for ch in [chr(c) for c in range(0x20)] + ["\x7f"]:
            self.assertEqual(tomllib.loads("x = " + self.smoke.toml_value(ch))["x"], ch)

    def test_send_to_town_with_non_finite_params_keeps_defaults(self):
        for raw in ("inf", "-inf", "nan"):
            self.path.write_text(f"[params]\nfight_margin = {raw}\ncuriosity = {raw}\nrisk = 0.25\n")
            self.smoke.send_to_town(self.path)
            d = load_directives(self.path)
            self.assertEqual(d.goals, ["travel:town"])
            self.assertEqual(d.params["fight_margin"], PARAM_DEFAULTS["fight_margin"])
            self.assertEqual(d.params["curiosity"], PARAM_DEFAULTS["curiosity"])
            self.assertEqual(d.params["risk"], 0.25)

    def test_toml_value_round_trips_every_param_type(self):
        for v in (True, False, 0, 3, -2, 1.5, 0.1, 1e300, float("inf"), float("-inf")):
            self.assertEqual(tomllib.loads("x = " + self.smoke.toml_value(v))["x"], v)
        self.assertTrue(math.isnan(tomllib.loads("x = " + self.smoke.toml_value(float("nan")))["x"]))

    def test_send_to_town_with_no_file_writes_defaults(self):
        self.smoke.send_to_town(self.path)
        d = load_directives(self.path)
        self.assertEqual(d.goals, ["travel:town"])
        self.assertEqual(d.params, PARAM_DEFAULTS)

    def test_send_to_town_replaces_an_unparseable_file(self):
        self.path.write_text("goals = [")
        self.smoke.send_to_town(self.path)
        self.assertEqual(load_directives(self.path).goals, ["travel:town"])

    def test_restore_puts_back_or_removes_the_file(self):
        self.smoke.send_to_town(self.path)
        self.smoke.restore_file(self.path, None)
        self.assertFalse(self.path.exists())
        self.path.write_bytes(b"x")
        self.smoke.restore_file(self.path, b"goals = []\n")
        self.assertEqual(self.path.read_bytes(), b"goals = []\n")


class MissingMarksTest(unittest.TestCase):
    def test_missing_lists_unlooked_required_marks(self):
        kb = kb_with_entrances()
        self.assertEqual(len(missing_entrance_marks(kb, StrengthBracket())), 2)
        with kb.lock:
            kb.entrances["7:10,20"].update({"looked": True, "block_type": "grass"})
        self.assertEqual(len(missing_entrance_marks(kb, StrengthBracket())), 1)


if __name__ == "__main__":
    unittest.main()
