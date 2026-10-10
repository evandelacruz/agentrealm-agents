"""A25: M8 acceptance metrics and smoke script (no server)."""

from __future__ import annotations

import importlib.util
import io
import threading
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
from tests.test_m6_acceptance import RunnerCase
from tests.test_m7_acceptance import TownServer

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


HELD = object()  # the runner polling a held queue: before_tick sees intents=None
WAIT = [{"verb": "Wait"}]
SWING = [{"verb": "Use", "target": {"kind": "npc", "npc_id": 5}}] * 3
WINNABLE = {**PARAM_DEFAULTS, "fight_margin": 0.5}  # a 10-health start beats one snotling


def decide(m, w, *, state="Explore", intents=WAIT, params=None):
    m.before_tick(
        w,
        Memory(),
        state=state,
        reason="test",
        intents=None if intents is HELD else intents,
        policy=Policy(hostile=["npc"], on_hostile="fight"),
        params=params or dict(PARAM_DEFAULTS),
        knowledge=None,
    )


def weak_fight_world(health=10) -> WorldModel:
    """One measured snotling hitting 1, in reach."""
    w = world(health=health, max_health=10)
    w.entities = [Entity("npc", 5, (1, 0), code="snotling")]
    w.threat.record(("npc", "snotling"), 1)
    return w


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
            intents=[{"verb": "Use", "target": {"kind": "self"}}],
        )
        self.assertTrue(m.heal_potion)

    def test_heal_arm_and_use_potion_while_weapon_still_armed(self):
        # Heal's first drink: [Arm potion, Use self] while armed_code is still the weapon.
        m = metrics()
        w = world()
        w.armed_code = "bronze_sword"
        w.held_supplies = [InventorySupply(4, "small_potion"), InventorySupply(6, "bronze_sword")]
        use = {"verb": "Use", "target": {"kind": "self"}}
        decide(m, w, state="Heal", intents=[{"verb": "Arm", "supply_id": 6}, use])
        self.assertFalse(m.heal_potion)
        decide(m, w, state="Heal", intents=[{"verb": "Arm", "supply_id": 4}, use])
        self.assertTrue(m.heal_potion)

    def test_weak_kill_on_npc_died_after_lone_weak_fight(self):
        m = metrics()
        w = weak_fight_world()
        decide(m, w, state="Fight", intents=SWING)
        m.on_events([{"kind": "NPCDied", "npc_id": 5}])
        self.assertEqual(m.weak_hostile_kills, 1)

    def test_weak_kill_on_a_later_held_queue_tick(self):
        m = metrics()
        w = weak_fight_world()
        decide(m, w, state="Fight", intents=SWING)
        m.on_events([])
        decide(m, w, state="Fight", intents=HELD)
        m.on_events([])
        decide(m, w, state="Fight", intents=HELD)
        m.on_events([{"kind": "NPCDied", "npc_id": 5}])
        self.assertEqual(m.weak_hostile_kills, 1)

    def test_npc_died_for_another_npc_is_not_our_kill(self):
        m = metrics()
        w = weak_fight_world()
        decide(m, w, state="Fight", intents=SWING)
        m.on_events([{"kind": "NPCDied", "npc_id": 6}])
        self.assertEqual(m.weak_hostile_kills, 0)

    def test_npc_died_after_leaving_fight_is_not_counted(self):
        m = metrics()
        w = weak_fight_world()
        decide(m, w, state="Fight", intents=SWING)
        decide(m, w, state="Retreat")
        m.on_events([{"kind": "NPCDied", "npc_id": 5}])
        self.assertEqual(m.weak_hostile_kills, 0)

    def test_kill_of_a_target_in_a_group_is_not_a_lone_kill(self):
        m = metrics()
        w = weak_fight_world()
        w.entities.append(Entity("npc", 6, (2, 0), code="snotling"))
        decide(m, w, state="Fight", intents=SWING)
        m.on_events([{"kind": "NPCDied", "npc_id": 5}])
        self.assertEqual(m.weak_hostile_kills, 0)

    def test_kill_of_a_target_outside_the_lone_group_is_not_counted(self):
        # The lone weak hostile is NPC 6; NPC 5, the one attacked, is far off.
        m = metrics()
        w = weak_fight_world()
        w.entities = [Entity("npc", 6, (1, 0), code="snotling"), Entity("npc", 5, (20, 0), code="snotling")]
        decide(m, w, state="Fight", intents=SWING)
        m.on_events([{"kind": "NPCDied", "npc_id": 5}])
        self.assertEqual(m.weak_hostile_kills, 0)

    def test_fight_started_when_it_would_lose_fails(self):
        m = metrics()
        w = weak_fight_world(health=1)
        w.threat.record(("npc", "snotling"), 2)
        decide(m, w, state="Fight", intents=SWING, params=WINNABLE)
        self.assertEqual(m.fight_below_floor, 1)
        self.assertTrue(any("health floor" in f for f in m.failures(full_run=False)))

    def test_hurt_but_winnable_fight_start_passes(self):
        m = metrics()
        decide(m, weak_fight_world(health=8), state="Fight", intents=SWING, params=WINNABLE)
        self.assertEqual(m.fight_below_floor, 0)

    def test_swings_later_in_a_fight_are_not_judged(self):
        m = metrics()
        decide(m, weak_fight_world(), state="Fight", intents=SWING, params=WINNABLE)
        w = weak_fight_world(health=1)
        w.threat.record(("npc", "snotling"), 2)
        decide(m, w, state="Fight", intents=SWING, params=WINNABLE)
        self.assertEqual(m.fight_below_floor, 0)

    def test_short_run_skips_milestones(self):
        m = metrics()
        decide(m, world(gems=0))
        self.assertEqual(m.failures(full_run=False), [])


class RunnerHookTest(RunnerCase):
    def test_npc_died_reaches_the_gate_through_the_runner(self):
        stop = threading.Event()
        m = metrics()
        r = self.make_runner(TownServer(10, stop), stop, m)
        decide(m, weak_fight_world(), state="Fight", intents=SWING)
        r.on_events([{"kind": "NPCDied", "npc_id": 5}])
        self.assertEqual(m.weak_hostile_kills, 1)


class SmokeScriptTest(unittest.TestCase):
    def setUp(self):
        self.smoke = load_smoke()

    def main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = self.smoke.main(["--no-planner", *argv])  # offline: the planner test mode
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
                mock.patch.object(self.smoke, "run_acceptance_smoke", return_value=(10.0, None)):
            code, out, _ = self.main(["--api-key", "k", "--character-id", "9", "--seconds", "10"])
        self.assertEqual(code, 0, out)

    def test_full_run_fails_without_milestones(self):
        with mock.patch.object(self.smoke, "Client"), \
                mock.patch.object(self.smoke, "resolve_character_id", return_value=9), \
                mock.patch.object(self.smoke.time, "sleep"), \
                mock.patch.object(self.smoke, "wake"), \
                mock.patch.object(self.smoke, "navigation_start", return_value=(OVERWORLD, (0, 0))), \
                mock.patch.object(self.smoke, "run_acceptance_smoke", return_value=(float(TARGET_SECONDS), None)):
            code, _, err = self.main(["--api-key", "k", "--character-id", "9", "--seconds", str(TARGET_SECONDS)])
        self.assertEqual(code, 1)
        self.assertIn("gems never increased", err)


if __name__ == "__main__":
    unittest.main()
