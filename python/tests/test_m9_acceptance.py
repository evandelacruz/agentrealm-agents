"""A29: M9 acceptance metrics and smoke script (no server)."""

from __future__ import annotations

import importlib.util
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from agentrealm_agent import config
from agentrealm_agent.config import Policy
from agentrealm_agent.directives import PARAM_DEFAULTS
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.m9_acceptance import (
    M9AcceptanceMetrics,
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
        with kb.lock:
            for key in list(kb.entrances):
                kb.entrances[key].update({"looked": True, "block_type": "grass"})
        m = M9AcceptanceMetrics()
        decide(m, self.open_world((0, 0), map_id=7), kb)
        self.assertTrue(m.entrances_ok())
        self.assertTrue(m.at_town_end)
        self.assertEqual(m.failures(), [])

    def test_survival_failures_match_m7(self):
        m = M9AcceptanceMetrics()
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
            code = self.smoke.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_no_api_key_exits_2(self):
        code, _, err = self.main([])
        self.assertEqual(code, 2)
        self.assertIn("AGENTREALM_API_KEY", err)

    def run_main(self, seconds: float, played):
        def run_smoke(client, cfg, cid, metrics, *, timeout_s):
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
            m.catalog_size, m.entrances_recorded, m.at_town_end = 3, 3, True

        code, out, _ = self.run_main(7200, played)
        self.assertEqual(code, 0, out)

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


class MissingMarksTest(unittest.TestCase):
    def test_missing_lists_unlooked_required_marks(self):
        kb = kb_with_entrances()
        self.assertEqual(len(missing_entrance_marks(kb, StrengthBracket())), 2)
        with kb.lock:
            kb.entrances["7:10,20"].update({"looked": True, "block_type": "grass"})
        self.assertEqual(len(missing_entrance_marks(kb, StrengthBracket())), 1)


if __name__ == "__main__":
    unittest.main()
