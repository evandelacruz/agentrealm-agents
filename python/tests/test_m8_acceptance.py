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
DRINK = {"verb": "Use", "target": {"kind": "self"}}
APPLIED = {"outcome": "applied"}
SUPPLY_TAKEN = {"kind": "SupplyTaken", "supply_id": 9, "taker_id": 1, "tick": 2}
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


def killed(npc_id, *, actor_id=1, tick=8, npc_type="snotling") -> list[dict]:
    """The killing blow and the death, as a tick's events carry them (API Events)."""
    return [
        {"kind": "NPCDamaged", "npc_id": npc_id, "amount": 4, "map_id": OVERWORLD, "x": 1, "y": 0,
         "actor_kind": "character", "actor_id": actor_id, "tick": tick},
        {"kind": "NPCDied", "npc_id": npc_id, "npc_type": npc_type, "map_id": OVERWORLD, "x": 1, "y": 0, "tick": tick},
    ]


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
        w = world(health=9, max_health=10)
        w.entities = [Entity("supply", 9, (0, 0), code="apple")]
        decide(
            m,
            w,
            state="Heal",
            intents=[{"verb": "Take", "supply_id": 9}],
        )
        self.assertFalse(m.food_taken_hurt, "sent is not taken")
        m.on_events([SUPPLY_TAKEN])
        self.assertTrue(m.food_taken_hurt)
        w.armed_code = "small_potion"
        w.held_supplies = [InventorySupply(4, "small_potion")]
        decide(m, w, state="Heal", intents=[DRINK])
        self.assertFalse(m.heal_potion, "sent is not drunk")
        m.on_intent_result(DRINK, APPLIED)
        w.held_supplies, w.armed_code = [], None
        decide(m, w, state="Heal", intents=HELD)
        self.assertTrue(m.heal_potion)

    def test_food_eaten_by_walking_onto_it_counts(self):
        # Heal's food walk ends on the apple's cell: eaten on pickup, no Take sent.
        m = metrics()
        w = world(health=9, max_health=10)
        w.entities = [Entity("supply", 9, (1, 0), code="apple")]
        decide(m, w, state="Heal", intents=[{"verb": "Step", "direction": "E"}])
        w.entities = []
        m.on_events([SUPPLY_TAKEN])
        self.assertTrue(m.food_taken_hurt)

    def test_food_taken_on_a_detour_counts(self):
        # Hurt, Detour picks up an apple on its way: the food heals all the same.
        m = metrics()
        w = world(health=9, max_health=10)
        w.entities = [Entity("supply", 9, (1, 0), code="apple")]
        decide(m, w, state="Detour", intents=[{"verb": "Take", "supply_id": 9}])
        w.entities = []  # SupplyTaken removes it before the gate reads the events
        m.on_events([SUPPLY_TAKEN])
        self.assertTrue(m.food_taken_hurt)

    def test_food_taken_at_full_health_or_by_another_or_not_food_is_not_counted(self):
        m = metrics()
        w = world(health=10, max_health=10)
        w.entities = [Entity("supply", 9, (1, 0), code="apple")]
        decide(m, w, state="Detour", intents=[{"verb": "Take", "supply_id": 9}])
        m.on_events([SUPPLY_TAKEN])
        self.assertFalse(m.food_taken_hurt, "full health: it healed nothing")
        w = world(health=9, max_health=10)
        w.entities = [Entity("supply", 9, (1, 0), code="apple"), Entity("supply", 10, (1, 1), code="gem")]
        decide(m, w)
        m.on_events([{**SUPPLY_TAKEN, "taker_id": 2}, {**SUPPLY_TAKEN, "supply_id": 10}])
        self.assertFalse(m.food_taken_hurt)

    def test_heal_arm_and_use_potion_while_weapon_still_armed(self):
        # Heal's first drink: [Arm potion, Use self] while armed_code is still the weapon.
        m = metrics()
        w = world()
        w.armed_code = "bronze_sword"
        w.held_supplies = [InventorySupply(4, "small_potion"), InventorySupply(6, "bronze_sword")]
        sword_use = [{"verb": "Arm", "supply_id": 6}, DRINK]
        decide(m, w, state="Heal", intents=sword_use)
        m.on_intent_result(DRINK, APPLIED)
        w.held_supplies = [InventorySupply(6, "bronze_sword")]  # lost some other way
        decide(m, w, state="Heal", intents=HELD)
        self.assertFalse(m.heal_potion, "a self-Use with the sword in hand drinks nothing")
        w.held_supplies = [InventorySupply(4, "small_potion"), InventorySupply(6, "bronze_sword")]
        decide(m, w, state="Heal", intents=[{"verb": "Arm", "supply_id": 4}, DRINK])
        m.on_intent_result(DRINK, APPLIED)
        w.held_supplies = [InventorySupply(6, "bronze_sword")]
        decide(m, w, state="Heal", intents=HELD)
        self.assertTrue(m.heal_potion)


class PotionDrinkTest(unittest.TestCase):
    """A76: only a Use that uses a potion up counts as Heal drinking one."""

    def drink_sent(self, potions=2) -> tuple[M8AcceptanceMetrics, WorldModel]:
        m = metrics()
        w = world(health=8, max_health=10)
        w.armed_code = "small_potion"
        w.held_supplies = [InventorySupply(10 + i, "small_potion") for i in range(potions)]
        decide(m, w, state="Heal", intents=[DRINK])
        return m, w

    def test_applied_use_that_leaves_the_potions_is_not_a_drink(self):
        # A67 run 4: a Use at 8/10 health, potions 2 -> 2.
        m, w = self.drink_sent()
        m.on_intent_result(DRINK, APPLIED)
        decide(m, w, state="Heal", intents=HELD)
        self.assertFalse(m.heal_potion)
        self.assertIn("Heal never drank a carried potion", m.failures())
        # Judged once: a later potion loss with no drink out is not that drink.
        w.held_supplies = w.held_supplies[:1]
        decide(m, w, state="Explore", intents=HELD)
        self.assertFalse(m.heal_potion)

    def test_applied_use_and_a_potion_gone_is_a_drink(self):
        m, w = self.drink_sent()
        m.on_intent_result(DRINK, APPLIED)
        w.held_supplies = w.held_supplies[:1]
        decide(m, w, state="Heal", intents=HELD)
        self.assertTrue(m.heal_potion)
        self.assertNotIn("Heal never drank a carried potion", m.failures())

    def test_a_stowed_potion_counts_toward_the_drop(self):
        m, w = self.drink_sent(potions=1)
        w.chest_supplies = [InventorySupply(20, "small_potion")]
        decide(m, w, state="Heal", intents=[DRINK])  # resent with one stowed: 2 before
        m.on_intent_result(DRINK, APPLIED)
        w.held_supplies = []
        decide(m, w, state="Heal", intents=HELD)
        self.assertTrue(m.heal_potion)

    def test_potion_gone_without_an_applied_result_is_not_a_drink(self):
        m, w = self.drink_sent()
        w.held_supplies = w.held_supplies[:1]
        decide(m, w, state="Heal", intents=HELD)
        self.assertFalse(m.heal_potion)

    def test_no_effect_or_rejected_use_is_not_a_drink(self):
        for outcome in ("applied_no_effect", "rejected"):
            with self.subTest(outcome=outcome):
                m, w = self.drink_sent()
                m.on_intent_result(DRINK, {"outcome": outcome})
                w.held_supplies = w.held_supplies[:1]
                decide(m, w, state="Heal", intents=HELD)
                self.assertFalse(m.heal_potion)

    def test_a_queue_that_replaces_the_drink_drops_it(self):
        m, w = self.drink_sent()
        decide(m, w, state="Explore", intents=WAIT)
        m.on_events([])  # that response carried nothing of the drink
        m.on_intent_result(DRINK, APPLIED)
        w.held_supplies = w.held_supplies[:1]
        decide(m, w, state="Explore", intents=HELD)
        self.assertFalse(m.heal_potion)

    def test_the_replacing_responses_own_drink_result_still_counts(self):
        # Sent queues replace the drink, but that response may still carry
        # its result (an empty stop keeps the same queue id), as the runner's
        # _forget_replaced_drink allows.
        m, w = self.drink_sent()
        decide(m, w, state="Explore", intents=[])
        m.on_intent_result(DRINK, APPLIED)
        m.on_events([])
        w.held_supplies = w.held_supplies[:1]
        decide(m, w, state="Explore", intents=HELD)
        self.assertTrue(m.heal_potion)

    def test_the_replacing_responses_supply_used_still_counts(self):
        m, w = self.drink_sent()
        decide(m, w, state="Explore", intents=[])
        m.on_events([{"kind": "SupplyUsed", "actor_id": 1, "supply_code": "small_potion"}])
        self.assertTrue(m.heal_potion)

    def test_a_death_drops_the_drink(self):
        m, w = self.drink_sent()
        m.on_intent_result(DRINK, APPLIED)
        m.on_death()
        w.held_supplies = []  # the potions fell with the chest
        decide(m, w, state="Heal", intents=HELD)
        self.assertFalse(m.heal_potion)

    def test_our_supply_used_event_is_a_drink(self):
        m, _ = self.drink_sent()
        m.on_events([{"kind": "SupplyUsed", "actor_id": 1, "supply_code": "small_potion"}])
        self.assertTrue(m.heal_potion)

    def test_supply_used_by_another_character_or_for_another_supply_is_not(self):
        m, _ = self.drink_sent()
        m.on_events(
            [
                {"kind": "SupplyUsed", "actor_id": 2, "supply_code": "small_potion"},
                {"kind": "SupplyUsed", "actor_id": 1, "supply_code": "teleport_scroll"},
            ]
        )
        self.assertFalse(m.heal_potion)

    def test_supply_used_with_no_drink_out_is_not_counted(self):
        m = metrics()
        w = world()
        decide(m, w, state="Explore", intents=WAIT)
        m.on_events([{"kind": "SupplyUsed", "actor_id": 1, "supply_code": "small_potion"}])
        self.assertFalse(m.heal_potion)


class KillTest(unittest.TestCase):
    """A kill is our killing blow on the tick the NPC died (run-shaped events)."""

    def test_weak_kill_on_npc_died_after_lone_weak_fight(self):
        m = metrics()
        w = weak_fight_world()
        decide(m, w, state="Fight", intents=SWING)
        m.on_events(killed(5))
        self.assertEqual((m.kills, m.weak_hostile_kills), (1, 1))

    def test_weak_kill_on_a_later_held_queue_tick(self):
        m = metrics()
        w = weak_fight_world()
        decide(m, w, state="Fight", intents=SWING)
        m.on_events([])
        decide(m, w, state="Fight", intents=HELD)
        m.on_events([])
        decide(m, w, state="Fight", intents=HELD)
        m.on_events(killed(5))
        self.assertEqual(m.weak_hostile_kills, 1)

    def test_npc_died_for_another_npc_is_not_our_kill(self):
        m = metrics()
        w = weak_fight_world()
        decide(m, w, state="Fight", intents=SWING)
        m.on_events(killed(6))
        self.assertEqual((m.kills, m.weak_hostile_kills), (1, 0), "our blow, but never attacked as lone weak")
        m.on_events([{"kind": "NPCDied", "npc_id": 5, "npc_type": "snotling", "tick": 9}])
        self.assertEqual(m.kills, 1, "a death with no blow of ours is not our kill")

    def test_another_characters_killing_blow_is_not_our_kill(self):
        m = metrics()
        w = weak_fight_world()
        decide(m, w, state="Fight", intents=SWING)
        m.on_events(killed(5, actor_id=2))
        self.assertEqual((m.kills, m.weak_hostile_kills), (0, 0))

    def test_our_hit_on_an_earlier_tick_is_not_the_killing_blow(self):
        m = metrics()
        decide(m, weak_fight_world(), state="Fight", intents=SWING)
        hit, died = killed(5)
        m.on_events([{**hit, "tick": died["tick"] - 1}, died])
        self.assertEqual(m.kills, 0)

    def test_events_with_no_tick_are_not_a_killing_blow(self):
        m = metrics()
        decide(m, weak_fight_world(), state="Fight", intents=SWING)
        m.on_events([{k: v for k, v in ev.items() if k != "tick"} for ev in killed(5)])
        self.assertEqual(m.kills, 0)

    def test_an_npc_out_of_sight_is_judged_again_at_the_next_attack(self):
        m = metrics()
        w = weak_fight_world()
        w.entities.append(Entity("npc", 6, (2, 0), code="snotling"))
        decide(m, w, state="Fight", intents=SWING)  # in a group: not lone
        decide(m, world(health=10, max_health=10))  # both out of sight
        decide(m, weak_fight_world(), state="Fight", intents=SWING)  # alone now
        m.on_events(killed(5))
        self.assertEqual(m.weak_hostile_kills, 1)

    def test_kill_after_fight_hands_over_to_another_state_counts(self):
        # The swing that kills may be sent by Flee's "not outrunning" fight or
        # land after the state moved on: the killing blow decides.
        m = metrics()
        w = weak_fight_world()
        decide(m, w, state="Flee", intents=SWING)
        decide(m, w, state="Retreat")
        m.on_events(killed(5))
        self.assertEqual(m.weak_hostile_kills, 1)

    def test_kill_of_a_target_in_a_group_is_not_a_lone_kill(self):
        m = metrics()
        w = weak_fight_world()
        w.entities.append(Entity("npc", 6, (2, 0), code="snotling"))
        decide(m, w, state="Fight", intents=SWING)
        m.on_events(killed(5))
        self.assertEqual((m.kills, m.weak_hostile_kills), (1, 0))

    def test_a_hostile_that_hits_harder_than_base_is_not_weak(self):
        m = metrics()
        w = weak_fight_world()
        w.threat.record(("npc", "snotling"), 3)
        decide(m, w, state="Fight", intents=SWING)
        m.on_events(killed(5))
        self.assertEqual((m.kills, m.weak_hostile_kills), (1, 0))

    def test_kill_of_a_target_outside_the_lone_group_is_not_lone(self):
        # The lone weak hostile is NPC 6; NPC 5, the one attacked, is far off.
        m = metrics()
        w = weak_fight_world()
        w.entities = [Entity("npc", 6, (1, 0), code="snotling"), Entity("npc", 5, (20, 0), code="snotling")]
        decide(m, w, state="Fight", intents=SWING)
        m.on_events(killed(5))
        self.assertEqual(m.weak_hostile_kills, 0)

    def test_a_type_never_measured_is_weak_at_the_win_estimates_price(self):
        # Closing on a hostile whose swings at us all missed: no hit measured,
        # priced at the base attack power, and its kill is a lone weak kill.
        m = metrics()
        w = world(health=10, max_health=10)
        w.entities = [Entity("npc", 5, (1, 0), code="snotling")]
        w.threat.misses[("npc", "snotling")] = 3
        decide(m, w, state="Fight", intents=SWING)
        m.on_events(killed(5))
        self.assertEqual((m.kills, m.weak_hostile_kills), (1, 1))

    def test_summary_counts_every_kill(self):
        m = metrics()
        decide(m, weak_fight_world(), state="Fight", intents=SWING)
        m.on_events(killed(5))
        self.assertIn("kills: 1, lone weak: 1", m.summary_lines())

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
        r.on_events(killed(5))
        self.assertEqual(m.weak_hostile_kills, 1)

    def test_a_drinks_result_reaches_the_gate_through_the_runner(self):
        stop = threading.Event()
        m = metrics()
        r = self.make_runner(TownServer(10, stop), stop, m)
        w = r.world
        w.health, w.max_health, w.armed_code = 8, 10, "small_potion"
        w.held_supplies = [InventorySupply(4, "small_potion")]
        decide(m, w, state="Heal", intents=[DRINK])
        r.mem.pending, r.mem.pending_queue = DRINK, "q1"
        r.apply_intent_results([{"queue_id": "q1", "index": 0, "tick": 101, "outcome": "applied"}])
        w.held_supplies = []
        decide(m, w, state="Heal", intents=HELD)
        self.assertTrue(m.heal_potion)


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
