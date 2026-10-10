"""A60: the regen probe's stop conditions and its hand-off to the M7 gate (no server)."""

from __future__ import annotations

import importlib.util
import io
import json
import random
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from agentrealm_agent import config
from agentrealm_agent.directives import PARAM_DEFAULTS, load_directives
from agentrealm_agent.healing import REGEN_KEY, SURVIVAL_KEY
from agentrealm_agent.knowledge_base import load as load_knowledge
from agentrealm_agent.memory import Memory
from agentrealm_agent.states import PlayContext, dispatch
from agentrealm_agent.world import Entity, WorldModel, ZoneFact
from tests.test_m7_acceptance import decide, metrics as m7_metrics

REPO = Path(__file__).resolve().parents[2]
PROBE_PATH = REPO / "scripts" / "probe_regen.py"
PROFILE = REPO / "python" / "characters" / "regen_probe.toml"
WINDOWS = 100  # 1000 ticks: well past the regen sample's 200


def load_probe():
    spec = importlib.util.spec_from_file_location("probe_regen", PROBE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def hurt_on_safe_tile() -> WorldModel:
    """Map 7, all dirt, standing hurt on the safe tile (0, 0) beside the respawn anchor."""
    w = WorldModel(character_id=9, map_id=7, pos=(0, 0), perception=5, health=5, max_health=10)
    for x in range(5):
        for y in range(5):
            w.view.tiles[(x, y)] = "dirt"
    w.terrain_center, w.terrain_map = (0, 0), 7
    w.record_respawn_anchor(7, (0, 0))
    w.zones[7] = {(0, 0): ZoneFact(safe=True)}
    return w


class FakeRunner:
    """Stands in for the live runner: the real states decide each window on a
    fixed world, and the hooks see it the way the runner calls them.
    ``events(w, hooks)`` runs before each decision (health back, a death)."""

    world: WorldModel
    events = staticmethod(lambda w, hooks: None)
    windows = 0

    park_report = None  # the park phase (A66) is not faked

    def __init__(
        self, cfg, client, cid, stop, out=print, knowledge=None, acceptance=None, strategist=None, park_seconds=0.0, abort=None
    ):
        self.cfg, self.stop, self.knowledge, self.hooks = cfg, stop, knowledge, acceptance

    def run(self) -> None:
        w, m = FakeRunner.world, Memory()
        params = load_directives(self.cfg.directives_path).params
        for _ in range(WINDOWS):
            self.hooks.on_window(urgent=False)
            if self.stop.is_set():
                return
            FakeRunner.events(w, self.hooks)
            if self.stop.is_set():
                return
            FakeRunner.windows += 1
            ctx = PlayContext(m, self.cfg.policy, random.Random(0), params=params, knowledge=self.knowledge)
            out = dispatch(w, ctx)
            self.hooks.before_tick(
                w,
                m,
                state=out.state,
                reason=out.reason,
                intents=out.intents,
                policy=self.cfg.policy,
                params=ctx.params,
                knowledge=self.knowledge,
            )
            w.tick += 10


class ProbeRunTest(unittest.TestCase):
    """``main`` end to end against a fake client and runner, with the world
    knowledge base in a temporary directory."""

    def setUp(self):
        self.probe = load_probe()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.worlds = Path(tmp.name)
        offline = mock.patch("agentrealm_agent.supplies.load", return_value="bundled")  # no fetch in tests
        offline.start()
        self.addCleanup(offline.stop)
        for patch in (
            mock.patch("agentrealm_agent.knowledge_base.WORLDS_DIR", self.worlds),
            mock.patch("agentrealm_agent.acceptance_smoke.Runner", FakeRunner),
            mock.patch.object(self.probe, "Client"),
            mock.patch.object(self.probe, "resolve_character_id", return_value=9),
            mock.patch.object(self.probe, "wake"),
            mock.patch.object(self.probe.time, "sleep"),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        FakeRunner.world = hurt_on_safe_tile()
        FakeRunner.events = staticmethod(lambda w, hooks: None)
        FakeRunner.windows = 0

    def run_probe(self, *extra: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = self.probe.main(["--api-key", "k", "--character-id", "9", "--no-planner", *extra])
        return code, out.getvalue(), err.getvalue()

    def saved_regen(self):
        path = self.worlds / "olympuff.json"
        if not path.exists():
            return None
        return (json.loads(path.read_text()).get(SURVIVAL_KEY) or {}).get(REGEN_KEY)

    def test_yes_is_saved_and_ends_the_probe(self):
        def health_back(w, hooks):
            if w.tick >= 20:
                w.health = 6

        FakeRunner.events = staticmethod(health_back)
        code, out, err = self.run_probe()
        self.assertEqual(code, 0, err)
        self.assertIn("safe-zone regen: yes", out)
        self.assertIn("PASS", out)
        self.assertEqual(self.saved_regen(), "yes")
        self.assertLess(FakeRunner.windows, 5, "stops on the verdict")

    def test_yes_from_the_probe_passes_the_m7_regen_clause_on_a_later_run(self):
        later, fresh = m7_metrics(), m7_metrics()
        decide(fresh, hurt_on_safe_tile(), knowledge=load_knowledge("olympuff"))
        self.assertIn("safe-zone regen never measured", fresh.failures(), "control: no answer yet")

        FakeRunner.events = staticmethod(lambda w, hooks: setattr(w, "health", 6) if w.tick >= 20 else None)
        self.assertEqual(self.run_probe()[0], 0)
        w = hurt_on_safe_tile()
        w.health = w.max_health  # the later hour is never hurt, as in A58 runs 1-8
        decide(later, w, Memory(), knowledge=load_knowledge("olympuff"))
        self.assertEqual(later.regen, "yes")
        self.assertNotIn("safe-zone regen never measured", later.failures(full_hour=True))

    def test_no_exits_0_and_saves_nothing(self):
        code, out, err = self.run_probe()
        self.assertEqual(code, 0, err)
        self.assertIn("safe-zone regen: no", out)
        self.assertIn("PASS: safe-zone regen is 'no'", out)
        self.assertNotEqual(self.saved_regen(), "yes")
        self.assertLess(FakeRunner.windows, WINDOWS, "stops on the verdict")

    def test_cap_without_a_verdict_exits_1(self):
        FakeRunner.world.health = 10  # never hurt
        code, out, err = self.run_probe("--seconds", "0")
        self.assertEqual(code, 1)
        self.assertEqual(FakeRunner.windows, 0, "the cap stops the run")
        self.assertIn("safe-zone regen: not answered", out)
        self.assertIn("regen not answered before the probe stopped (never hurt)", err)

    def test_first_death_ends_the_probe_as_a_failure(self):
        def dies(w, hooks):
            if w.tick >= 30:
                hooks.on_death()

        FakeRunner.events = staticmethod(dies)
        code, _, err = self.run_probe("--stop-on-death")
        self.assertEqual(code, 1)
        self.assertEqual(FakeRunner.windows, 3, "nothing more after the death")
        self.assertIn("1 death(s)", err)
        self.assertIsNone(self.saved_regen())

    def test_by_default_a_death_is_reported_and_the_probe_plays_on(self):
        def dies(w, hooks):
            if w.tick == 30:
                hooks.on_death()

        FakeRunner.events = staticmethod(dies)
        _, out, err = self.run_probe()
        self.assertGreater(FakeRunner.windows, 3, "play goes on after the respawn")
        self.assertIn("deaths: 1", out)
        self.assertNotIn("death(s)", err)

    def test_a_yes_already_saved_answers_at_once(self):
        self.assertEqual(self.run_probe()[0], 0)  # "no": nothing saved
        FakeRunner.events = staticmethod(lambda w, hooks: setattr(w, "health", 6) if w.tick >= 20 else None)
        FakeRunner.world = hurt_on_safe_tile()
        self.run_probe()
        FakeRunner.world, FakeRunner.windows = hurt_on_safe_tile(), 0
        FakeRunner.events = staticmethod(lambda w, hooks: None)
        code, out, _ = self.run_probe()
        self.assertEqual((code, FakeRunner.windows), (0, 1))
        self.assertIn("safe-zone regen: yes", out)

    def test_missing_api_key_exits_2(self):
        with mock.patch.dict("os.environ", {"AGENTREALM_API_KEY": ""}):
            err = io.StringIO()
            with redirect_stderr(err):
                self.assertEqual(self.probe.main([]), 2)
        self.assertIn("AGENTREALM_API_KEY", err.getvalue())


class ProbeProfileTest(unittest.TestCase):
    """The probe profile and its directives, with the real states deciding."""

    def probe_decides(self, *, health: int = 10, lives: int = 10) -> str:
        cfg = config.load(PROFILE)
        params = load_directives(cfg.directives_path).params
        w = hurt_on_safe_tile()
        w.pos, w.terrain_center, w.health, w.lives = (2, 2), (2, 2), health, lives
        w.entities = [Entity("npc", 3, (3, 2), "slime")]
        w.hostile_types.add(("npc", "slime"))  # a type seen attacking (survival.is_hostile)
        return dispatch(w, PlayContext(Memory(), cfg.policy, random.Random(0), params=params)).state

    def test_only_risk_and_fight_margin_move_off_the_defaults(self):
        params = load_directives(config.load(PROFILE).directives_path).params
        changed = {k for k, v in params.items() if v != PARAM_DEFAULTS[k]}
        self.assertEqual(changed, {"fight_margin", "risk"})

    def test_a_fresh_character_fights_an_unmeasured_hostile_to_get_hurt(self):
        self.assertEqual(self.probe_decides(health=10), "Fight")

    def test_it_retreats_at_the_should_retreat_threshold(self):
        self.assertEqual(self.probe_decides(health=7), "Fight")
        self.assertEqual(self.probe_decides(health=6), "Retreat")

    def test_low_on_lives_it_retreats_instead_of_meeting_an_unmeasured_hostile(self):
        # Fewer lives lower the effective risk, so the win estimate refuses
        # an unmeasured hostile at full health.
        self.assertEqual(self.probe_decides(lives=5), "Retreat")
        self.assertEqual(self.probe_decides(lives=4), "Retreat")


if __name__ == "__main__":
    unittest.main()
