"""A25: M8 acceptance metrics and smoke script (no server)."""

from __future__ import annotations

import importlib.util
import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from agentrealm_agent.config import Policy
from agentrealm_agent.directives import PARAM_DEFAULTS
from agentrealm_agent.item_table import InventorySupply
from agentrealm_agent.m8_acceptance import M8AcceptanceMetrics, TARGET_SECONDS
from agentrealm_agent.memory import Memory
from agentrealm_agent.world import Entity, WorldModel
from agentrealm_agent.zone_discovery import apply_zone

REPO = Path(__file__).resolve().parents[2]
SMOKE_PATH = REPO / "scripts" / "smoke_m8_olympuff.py"
OVERWORLD = 7


def load_smoke():
    spec = importlib.util.spec_from_file_location("smoke_m8_olympuff", SMOKE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def metrics(**kw) -> M8AcceptanceMetrics:
    return M8AcceptanceMetrics(**kw)


def world(**kw) -> WorldModel:
    w = WorldModel(character_id=1, map_id=OVERWORLD, pos=(0, 0), perception=5, tick=1, **kw)
    for y in range(-2, 3):
        for x in range(-2, 3):
            w.view.tiles[(x, y)] = "dirt"
    w.terrain_center, w.terrain_map = w.pos, OVERWORLD
    return w


def decide(m, w, *, state="Explore", intents=None, params=None):
    m.before_tick(
        w,
        Memory(),
        state=state,
        reason="test",
        intents=intents if intents is not None else [{"verb": "Wait"}],
        policy=Policy(hostile=["npc"], on_hostile="fight"),
        params=params or dict(PARAM_DEFAULTS),
        knowledge=None,
    )


class MilestoneGateTest(unittest.TestCase):
    def test_full_run_fails_until_milestones_met(self):
        m = metrics()
        decide(m, world(gems=0))
        self.assertTrue(any("gems never increased" in f for f in m.failures()))

    def test_gems_earned_when_the_counter_rises(self):
        m = metrics()
        decide(m, world(gems=0))
        decide(m, world(gems=3))
        self.assertTrue(m.gems_earned)
        self.assertNotIn("gems never increased", m.failures())

    def test_armor_weapon_and_potion_reserve(self):
        m = metrics(potion_reserve=2)
        w = world(gems=10, health=10, max_health=10)
        w.worn_codes = {"body": "bronze_mail"}
        w.worn_slots["bronze_mail"] = "body"
        w.armed_code = "bronze_sword"
        w.held_supplies = [
            InventorySupply(1, "small_potion"),
            InventorySupply(2, "small_potion"),
        ]
        decide(m, w)
        self.assertTrue(m.armor_worn and m.shop_weapon and m.potion_reserve_met)

    def test_heal_food_and_potion(self):
        m = metrics()
        w = world()
        w.entities = [Entity("supply", 9, (0, 0), code="apple")]
        decide(
            m,
            w,
            state="Heal",
            intents=[{"verb": "Take", "supply_id": 9}],
        )
        self.assertTrue(m.heal_food_take)
        w.armed_code = "small_potion"
        decide(
            m,
            w,
            state="Heal",
            intents=[{"verb": "Use", "target": {"kind": "character", "character_id": 1}}],
        )
        self.assertTrue(m.heal_potion)

    def test_weak_kill_on_npc_died_after_lone_weak_fight(self):
        m = metrics()
        w = world(health=10, max_health=10)
        w.entities = [Entity("npc", 5, (1, 0), code="snotling")]
        w.threat.record(("npc", "snotling"), 1)
        decide(
            m,
            w,
            state="Fight",
            intents=[{"verb": "Use", "target": {"kind": "npc", "npc_id": 5}}],
        )
        m.on_events([{"kind": "NPCDied", "npc_id": 5}])
        self.assertEqual(m.weak_hostile_kills, 1)

    def test_fight_while_hurt_fails(self):
        m = metrics()
        w = world(health=5, max_health=10)
        w.entities = [Entity("npc", 5, (1, 0), code="snotling")]
        w.threat.record(("npc", "snotling"), 1)
        decide(
            m,
            w,
            state="Fight",
            intents=[{"verb": "Use", "target": {"kind": "npc", "npc_id": 5}}],
        )
        self.assertEqual(m.fight_below_floor, 1)
        self.assertTrue(any("health floor" in f for f in m.failures(full_run=False)))

    def test_short_run_skips_milestones(self):
        m = metrics()
        decide(m, world(gems=0))
        self.assertEqual(m.failures(full_run=False), [])


class SmokeScriptTest(unittest.TestCase):
    def setUp(self):
        self.smoke = load_smoke()

    def main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = self.smoke.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_no_api_key_exits_2(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            code, _, err = self.main([])
        self.assertEqual(code, 2)
        self.assertIn("AGENTREALM_API_KEY", err)

    def test_short_run_passes_without_milestones(self):
        def played(m):
            pass

        with mock.patch.object(self.smoke, "Client"), \
                mock.patch.object(self.smoke, "resolve_character_id", return_value=9), \
                mock.patch.object(self.smoke.time, "sleep"), \
                mock.patch.object(self.smoke, "wake"), \
                mock.patch.object(self.smoke, "navigation_start", return_value=(OVERWORLD, (0, 0))), \
                mock.patch.object(self.smoke, "run_smoke", side_effect=lambda *a, **k: (a[3], 10.0)):
            code, out, _ = self.main(["--api-key", "k", "--character-id", "9", "--seconds", "10"])
        self.assertEqual(code, 0, out)

    def test_full_run_fails_without_milestones(self):
        with mock.patch.object(self.smoke, "Client"), \
                mock.patch.object(self.smoke, "resolve_character_id", return_value=9), \
                mock.patch.object(self.smoke.time, "sleep"), \
                mock.patch.object(self.smoke, "wake"), \
                mock.patch.object(self.smoke, "navigation_start", return_value=(OVERWORLD, (0, 0))), \
                mock.patch.object(self.smoke, "run_smoke", side_effect=lambda *a, **k: (a[3], float(TARGET_SECONDS))):
            code, _, err = self.main(["--api-key", "k", "--character-id", "9", "--seconds", str(TARGET_SECONDS)])
        self.assertEqual(code, 1)
        self.assertIn("gems never increased", err)


if __name__ == "__main__":
    unittest.main()
