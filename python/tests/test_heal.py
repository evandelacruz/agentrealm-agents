"""A10: Heal state and healing helpers."""

import random
import unittest
import unittest.mock

from agentrealm_agent.brain import decide
from agentrealm_agent.config import Policy
from agentrealm_agent.executor import DEFAULT_WEAPON_COOLDOWN_TICKS
from agentrealm_agent.healing import (
    REGEN_KEY,
    REGEN_MEASURE_TICKS,
    SURVIVAL_KEY,
    absorb_heal_pending,
    hurt,
    note_heal_pending,
    regen_known,
    regen_per_tick,
    save_regen_yes,
)
from agentrealm_agent.executor.supply_refusals import Refusal, note_refusal, refusal_situation
from agentrealm_agent.item_table import InventorySupply, carried_from_inventory
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.memory import Memory
from agentrealm_agent.states import dispatch
from agentrealm_agent.states.base import PlayContext
from agentrealm_agent.world import Entity, WorldModel, ZoneFact


def grid(at=(1, 1), size=5) -> WorldModel:
    """Map 7, all dirt, safe zone at (0, 0) beside the respawn anchor."""
    w = WorldModel(character_id=9, map_id=7, pos=at, perception=size, health=5, max_health=10)
    for x in range(size):
        for y in range(size):
            w.view.tiles[(x, y)] = "dirt"
    w.terrain_center, w.terrain_map = at, 7
    w.record_respawn_anchor(7, (0, 0))
    w.zones[7] = {(0, 0): ZoneFact(safe=True)}
    if at != (0, 0):
        w.zones[7][at] = ZoneFact(safe=False)
    return w


def ctx(m: Memory | None = None, kb: KnowledgeBase | None = None) -> PlayContext:
    return PlayContext(m or Memory(), Policy(kind="scripted"), random.Random(0), knowledge=kb)


def verbs(out) -> list[str]:
    return [i["verb"] for i in out.intents or []]


class HealingHelpersTest(unittest.TestCase):
    def test_hurt(self):
        self.assertTrue(hurt(grid()))

    def test_held_from_carried_inventory(self):
        inv = {"held": [{"id": 4, "supply_subtype_code": "small_potion"}, {"id": "x"}, "junk"]}
        self.assertEqual(carried_from_inventory(inv)[0], [InventorySupply(4, "small_potion")])

    def test_apply_observation_fills_held(self):
        w = WorldModel(character_id=9)
        inv = {"held": [{"id": 4, "supply_subtype_code": "apple"}], "armed": None}
        w.apply_observation({"complete": True, "snapshot": {"health": 5, "max_health": 10, "inventory": inv}})
        self.assertEqual(w.held_supplies, [InventorySupply(4, "apple")])

    def test_only_yes_is_read_from_knowledge_base(self):
        kb = KnowledgeBase.empty("sandbox")
        kb.extra[SURVIVAL_KEY] = {REGEN_KEY: "no"}
        self.assertIsNone(regen_known(kb, Memory()))
        save_regen_yes(kb)
        self.assertEqual(regen_known(kb, Memory()), "yes")
        self.assertIsNone(regen_per_tick(kb), "a yes with no rate gives no rest estimate")
        save_regen_yes(kb, 0.05)
        self.assertEqual(regen_per_tick(kb), 0.05)


class HealStateTest(unittest.TestCase):
    def test_takes_adjacent_food(self):
        w = grid(at=(1, 1))
        w.entities = [Entity("supply", 8, (2, 1), "apple")]
        out = dispatch(w, ctx())
        self.assertEqual(out.state, "Heal")
        self.assertEqual(out.intents, [{"verb": "Take", "supply_id": 8}])

    def test_walks_one_step_toward_distant_food(self):
        w = grid(at=(1, 1))
        w.entities = [Entity("supply", 8, (4, 4), "golden_cap")]
        out = dispatch(w, ctx())
        self.assertEqual(out.intents, [{"verb": "SetPosition", "x": 2, "y": 2}])

    def test_a_held_take_waits_for_the_situation_to_change(self):
        # The runner files each refusal (``Runner._note_supply_refused``).
        w = grid(at=(1, 1))
        w.entities = [Entity("supply", 8, (2, 1), "apple")]
        m = Memory()
        self.assertEqual(verbs(dispatch(w, ctx(m))), ["Take"])
        note_refusal(m, "take", 8, Refusal("odd_code", "hold", w.tick, refusal_situation(w, (2, 1))))
        w.tick += 7
        self.assertNotIn("Take", verbs(dispatch(w, ctx(m))), "nothing changed: not sent again")
        w.held_supplies = [InventorySupply(3, "bronze_sword")]
        self.assertEqual(verbs(dispatch(w, ctx(m))), ["Take"], "a new item: try again")

    def test_carried_food_before_potion(self):
        w = grid()
        w.held_supplies = [InventorySupply(4, "small_potion"), InventorySupply(5, "berry")]
        out = dispatch(w, ctx())
        self.assertEqual(out.intents[0], {"verb": "Arm", "supply_id": 5})
        self.assertEqual(verbs(out), ["Arm", "Use"])

    def test_drinks_carried_potion_and_holds_a_refused_one(self):
        w = grid()
        w.held_supplies = [InventorySupply(4, "small_potion")]
        m = Memory()
        self.assertEqual(verbs(dispatch(w, ctx(m))), ["Arm", "Use"])
        note_refusal(m, "use", 4, Refusal("odd_code", "hold", w.tick, refusal_situation(w)))
        w.tick += 7
        self.assertNotIn("Use", verbs(dispatch(w, ctx(m))))

    def test_rearms_weapon_after_drinking_potion(self):
        w = grid()
        w.armed_code = "pocket_knife"
        w.held_supplies = [InventorySupply(1, "pocket_knife"), InventorySupply(4, "small_potion")]
        m = Memory()
        out = dispatch(w, ctx(m))
        self.assertEqual(verbs(out), ["Arm", "Use"])
        self.assertEqual(m.self_use_rearm, "pocket_knife")
        w.armed_code = None
        w.held_supplies = [InventorySupply(1, "pocket_knife")]
        out = dispatch(w, ctx(m))
        self.assertEqual(verbs(out), ["Arm"])
        self.assertIsNone(m.self_use_rearm)

    def test_rearms_weapon_once_the_drink_heals_to_full(self):
        # A potion that fills health ends the hurt guard; the re-arm still runs.
        w = grid()
        w.armed_code = "small_potion"
        w.health = 10
        w.held_supplies = [InventorySupply(1, "pocket_knife")]
        m = Memory(self_use_rearm="pocket_knife")
        out = dispatch(w, ctx(m))
        self.assertEqual(out.state, "Heal")
        self.assertEqual(out.intents, [{"verb": "Arm", "supply_id": 1}])
        self.assertIsNone(m.self_use_rearm)
        w.armed_code = "pocket_knife"
        self.assertNotEqual(dispatch(w, ctx(m)).state, "Heal")

    def test_rearms_weapon_with_a_hostile_in_range(self):
        w = grid()
        w.health = 10
        w.armed_code = "small_potion"
        w.held_supplies = [InventorySupply(1, "pocket_knife")]
        w.entities = [Entity("npc", 3, (2, 2), "slime")]
        m = Memory(self_use_rearm="pocket_knife")
        out = dispatch(w, ctx(m))
        self.assertEqual(out.state, "Heal")
        self.assertEqual(out.intents, [{"verb": "Arm", "supply_id": 1}])

    def test_the_weapon_goes_back_before_a_second_potion(self):
        # Free-play run 3: self_use_rearm held from 191 s to the end. It never
        # outlives one drink now, so Equip is never kept waiting on it.
        w = grid()
        w.armed_code = "small_potion"
        w.held_supplies = [InventorySupply(1, "pocket_knife"), InventorySupply(4, "small_potion")]
        m = Memory(self_use_rearm="pocket_knife")
        out = dispatch(w, ctx(m))
        self.assertEqual(out.intents, [{"verb": "Arm", "supply_id": 1}])
        self.assertIsNone(m.self_use_rearm)
        w.armed_code = "pocket_knife"
        w.held_supplies = [InventorySupply(4, "small_potion")]
        self.assertEqual(verbs(dispatch(w, ctx(m))), ["Arm", "Use"])
        self.assertEqual(m.self_use_rearm, "pocket_knife")

    def test_a_drink_sends_the_use_with_the_arm(self):
        """Free-play run 3: the drink's Arm went out and its Use never did."""
        w = grid()
        w.armed_code = "pocket_knife"
        w.held_supplies = [InventorySupply(1, "pocket_knife"), InventorySupply(4, "small_potion")]
        d = decide(w, Memory(), Policy(kind="scripted"), random.Random(0))
        self.assertEqual(d.state, "Heal")
        self.assertEqual(d.submit_queue, [{"verb": "Arm", "supply_id": 4},
                                          {"verb": "Use", "target": {"kind": "self"}}])

    def test_a_drink_waits_out_the_use_cooldown_after_the_arm(self):
        w = grid()
        w.tick = 100
        w.held_supplies = [InventorySupply(4, "small_potion")]
        out = dispatch(w, ctx(Memory(last_use_tick=96)))
        self.assertTrue(out.paced)
        self.assertEqual(verbs(out), ["Arm"] + ["Wait"] * (DEFAULT_WEAPON_COOLDOWN_TICKS - 5) + ["Use"])

    def test_the_reflex_probe_leaves_the_rearm_due(self):
        # While the drink's queue is held, the runner's reflex probe runs
        # Heal; the re-arm must still be due when the queue is done.
        import threading
        from pathlib import Path

        from agentrealm_agent.config import CharacterConfig
        from agentrealm_agent.runner import Runner

        cfg = CharacterConfig("T", "sandbox", Policy(kind="scripted", goals=[]), Path("t.toml"))
        r = Runner(cfg, None, 1, threading.Event(), out=lambda _: None, knowledge=KnowledgeBase.empty("sandbox"))
        self.addCleanup(r.trace.close)
        r.world = grid()
        r.world.armed_code = "small_potion"
        r.world.held_supplies = [InventorySupply(1, "pocket_knife")]
        r.mem = Memory(need_self=False, need_position=False, self_use_rearm="pocket_knife")
        self.assertIsNone(r.reflex_while_held())
        self.assertEqual(r.mem.self_use_rearm, "pocket_knife")

    def test_a_drink_with_nothing_armed_leaves_nothing_to_rearm(self):
        w = grid()
        w.held_supplies = [InventorySupply(4, "small_potion")]
        m = Memory()
        self.assertEqual(verbs(dispatch(w, ctx(m))), ["Arm", "Use"])
        self.assertIsNone(m.self_use_rearm)

    def test_weapon_gone_clears_rearm(self):
        w = grid()
        w.health = 10
        m = Memory(self_use_rearm="pocket_knife")
        self.assertNotEqual(dispatch(w, ctx(m)).state, "Heal")
        self.assertIsNone(m.self_use_rearm)

    def test_takes_food_even_when_it_would_overheal(self):
        # Free food beats a potion or a walk to town, whatever its heal.
        w = grid(at=(1, 1))
        w.health = 9
        w.entities = [Entity("supply", 8, (2, 1), "apple")]
        w.held_supplies = [InventorySupply(4, "small_potion")]
        kb = KnowledgeBase.empty("sandbox")
        kb.items["apple"] = {"heal_amount": 5}
        out = dispatch(w, ctx(kb=kb))
        self.assertEqual(out.intents, [{"verb": "Take", "supply_id": 8}])

    def test_learns_heal_from_applied_take(self):
        kb = KnowledgeBase.empty("sandbox")
        m = Memory(heal_pending=(5, "apple", "take"))
        w = grid()
        w.health = 8
        absorb_heal_pending(m, w, kb, [])
        self.assertEqual(kb.items["apple"]["heal_amount"], 3)
        self.assertTrue(kb.items["apple"]["heal_on_pickup"])

    def test_a_drink_behind_cooldown_waits_is_measured(self):
        # The potion is named by the Arm before the Use, Waits aside.
        from agentrealm_agent.runner import Runner

        r = Runner.__new__(Runner)
        r.world, r.mem = grid(), Memory()
        r.world.armed_code = "pocket_knife"
        r.world.held_supplies = [InventorySupply(4, "small_potion")]
        use = {"verb": "Use", "target": {"kind": "self"}}
        r.mem.pending_intents = [{"verb": "Arm", "supply_id": 4}, {"verb": "Wait"}, {"verb": "Wait"}, use]
        r._note_heal_intent(use, 3)
        self.assertEqual(r.mem.heal_pending, (5, "small_potion", "use"))

    def test_nothing_learned_at_full_health(self):
        # Loot also Takes food; at full health it cannot show a heal.
        m, w = Memory(), grid()
        w.health = 10
        note_heal_pending(m, w, "apple", "take")
        self.assertIsNone(m.heal_pending)

    def test_walks_to_known_safe_tile(self):
        w = grid(at=(2, 2))
        out = dispatch(w, ctx())
        self.assertEqual(out.state, "Heal")
        self.assertEqual(out.intents, [{"verb": "SetPosition", "x": 1, "y": 1}])

    def test_walk_to_safe_goes_round_a_known_pack_seen_since_it_was_planned(self):
        """A63 run 4: Heal's walk to the safe tile kept the path it planned and
        sent a 10-Step queue into a pack of known-hostile NPCs."""
        from agentrealm_agent.navigation import walk as nav_walk
        from agentrealm_agent.survival import hostile_reach

        w, m = grid(at=(11, 0), size=12), Memory()
        for i in range(-1, 13):  # walled in: no way round through fog
            for cell in ((i, -1), (i, 12), (-1, i), (12, i)):
                w.view.tiles[cell] = "wall"
        straight = [(x, 0) for x in range(10, -1, -1)]
        m.path, m.goal = list(straight), "heal_measure"
        m.walks["heal_measure"] = nav_walk.start(w, "heal_measure", (0, 0), straight)
        w.hostile_types.add(("npc", "wartlurch"))
        w.entities = [Entity("npc", 20 + i, p, "wartlurch") for i, p in enumerate([(5, 0), (5, 1), (6, 0)])]
        c = ctx(m)
        out = dispatch(w, c)
        self.assertEqual(out.state, "Heal")
        self.assertEqual(m.path[-1], (0, 0))
        self.assertTrue(hostile_reach(w, c.policy).isdisjoint(m.path), m.path)

    def test_a_reach_the_walk_cannot_go_round_is_not_replanned_every_decision(self):
        """Review on #137: with the reach unavoidable, the replanned path always
        crosses it, so comparing against the reach now replanned forever."""
        from unittest import mock
        from agentrealm_agent.states import heal

        w, m = grid(at=(8, 0), size=9), Memory()
        for x in range(-1, 10):
            for y in range(-1, 10):
                if y != 0 or x in (-1, 9):
                    w.view.tiles[(x, y)] = "wall"  # one corridor along y = 0
        w.hostile_types.add(("npc", "wartlurch"))
        w.entities = [Entity("npc", 20, (3, 2), "wartlurch")]  # reaches x 1..5 of the corridor
        c = ctx(m)
        with mock.patch.object(heal, "cost_path", wraps=heal.cost_path) as planned:
            self.assertEqual(dispatch(w, c).state, "Heal")
            self.assertEqual(planned.call_count, 1)
            w.entities = [Entity("npc", 20, (4, 2), "wartlurch")]  # it moves, still in reach
            self.assertEqual(dispatch(w, c).state, "Heal")
            self.assertEqual(planned.call_count, 1, "no new hostile, no new plan")
            w.entities.append(Entity("npc", 21, (5, 2), "wartlurch"))  # out of reach of us and the safe tile
            dispatch(w, c)
            self.assertEqual(planned.call_count, 2, "a hostile not in reach when planned is")

    def test_no_safe_tile_yields_to_the_safe_default(self):
        w = grid(at=(2, 2))
        w.zones[7] = {}
        m = Memory()
        out = dispatch(w, ctx(m))
        self.assertEqual(out.state, "Explore")
        self.assertIsNotNone(out.intents)
        self.assertIn("Heal: no reachable safe tile", out.yielded)
        # No backoff: Heal looks again next window, and still yields.
        w.tick += 7
        out = dispatch(w, ctx(m))
        self.assertEqual(out.state, "Explore")
        self.assertIn("Heal: no reachable safe tile", out.yielded)

    def test_hostile_in_range_keeps_heal_out(self):
        # Retreat or Flee (A9) answers the hostile; Heal waits until none is in range.
        w = grid()
        w.entities = [Entity("npc", 3, (2, 2), "slime")]
        w.hostile_types.add(("npc", "slime"))  # a type seen attacking (survival.is_hostile)
        self.assertIn(dispatch(w, ctx()).state, ("Retreat", "Flee"))

    def test_full_health_yields_to_explore(self):
        w = grid()
        w.health = 10
        self.assertEqual(dispatch(w, ctx()).state, "Explore")

    def assert_heals_in_zone(self, out):
        """In a one-cell safe zone every safe-default step leaves it, so Heal rests there."""
        self.assertEqual(out.state, "Heal")
        self.assertEqual(verbs(out), ["Wait"], out.reason)
        self.assertEqual(out.reason, "heal: rest in the safe zone")

    def test_moves_inside_a_larger_safe_zone(self):
        # Room in the zone: Heal keeps moving via the safe default, on zone cells only.
        w = grid(at=(0, 0))
        for x in range(5):
            for y in range(5):
                w.zones[7][(x, y)] = ZoneFact(safe=True)
        out = dispatch(w, ctx(Memory(), KnowledgeBase.empty("sandbox")))
        self.assertEqual(out.state, "Heal")
        self.assertEqual(verbs(out), ["SetPosition"])
        self.assertTrue(out.reason.startswith("heal in safe ground: "), out.reason)

    def test_measures_regen_then_saves_yes(self):
        w = grid(at=(0, 0))
        m, kb = Memory(), KnowledgeBase.empty("sandbox")
        out = dispatch(w, ctx(m, kb))
        self.assert_heals_in_zone(out)
        self.assertIsNotNone(m.heal_regen_sample)
        w.tick, w.health = 7, 6
        out = dispatch(w, ctx(m, kb))
        self.assert_heals_in_zone(out)
        self.assertEqual(regen_known(kb, Memory()), "yes")
        # A124: the window's rate is kept, for the planner's rest estimate.
        self.assertEqual(regen_per_tick(kb), 1 / 7)

    def test_a_saved_yes_with_no_rate_is_measured_again(self):
        # A124 review: a world saved with regen "yes" before the rate was kept
        # measures it on its next rest; a quiet window there stays "yes".
        w = grid(at=(0, 0))
        m, kb = Memory(), KnowledgeBase.empty("sandbox")
        save_regen_yes(kb)
        for t in range(0, REGEN_MEASURE_TICKS + 1, 10):
            w.tick = t
            self.assert_heals_in_zone(dispatch(w, ctx(m, kb)))
        self.assertFalse(m.heal_regen_absent, "a saved yes is never undone by one quiet window")
        self.assertEqual(regen_known(kb, Memory()), "yes")
        self.assertIsNone(regen_per_tick(kb))
        w.tick += 10
        dispatch(w, ctx(m, kb))
        w.tick, w.health = w.tick + 10, 6
        dispatch(w, ctx(m, kb))
        self.assertEqual(regen_per_tick(kb), 1 / 10)

    def test_regen_absent_is_kept_for_this_run_only(self):
        w = grid(at=(0, 0))
        m, kb = Memory(), KnowledgeBase.empty("sandbox")
        for t in range(0, REGEN_MEASURE_TICKS + 1, 10):
            w.tick = t
            out = dispatch(w, ctx(m, kb))
        self.assertTrue(m.heal_regen_absent)
        self.assertIsNone(regen_known(kb, Memory()))
        self.assertNotIn(SURVIVAL_KEY, kb.extra)
        # Regen known "no": Heal sends nothing and the safe default moves.
        w.tick += 10
        out = dispatch(w, ctx(m, kb))
        self.assertEqual(out.state, "Explore")
        self.assertIn("Heal: no safe-zone regen this run", out.yielded)

    def test_sample_restarts_after_leaving_zone(self):
        w = grid(at=(0, 0))
        m = Memory()
        dispatch(w, ctx(m))
        self.assertIsNotNone(m.heal_regen_sample)
        w.pos, w.tick = (1, 1), 10
        dispatch(w, ctx(m))
        self.assertIsNone(m.heal_regen_sample)
        w.pos, w.tick = (0, 0), REGEN_MEASURE_TICKS + 20
        out = dispatch(w, ctx(m))
        self.assert_heals_in_zone(out)
        self.assertEqual(m.heal_regen_sample, (REGEN_MEASURE_TICKS + 20, 5, REGEN_MEASURE_TICKS + 20))
        self.assertFalse(m.heal_regen_absent)

    def test_sample_restarts_when_health_falls(self):
        w = grid(at=(0, 0))
        m = Memory()
        dispatch(w, ctx(m))
        w.tick, w.health = 10, 4
        dispatch(w, ctx(m))
        self.assertEqual(m.heal_regen_sample, (10, 4, 10))

    def test_rests_when_regen_known(self):
        w = grid(at=(0, 0))
        kb = KnowledgeBase.empty("sandbox")
        save_regen_yes(kb, 0.05)
        m = Memory()
        out = dispatch(w, ctx(m, kb=kb))
        self.assert_heals_in_zone(out)
        self.assertIsNone(m.heal_regen_sample)  # known, with its rate: no sample taken

    def test_no_regen_stops_the_measure_walk_flip_flop(self):
        # Live: at 3/10, no food, no regen, Heal swapped between `heal_measure`
        # and "heal in safe ground" while the safe default walked out of the
        # zone and back. Now it rests in the zone until the verdict, then
        # never samples or walks to safe ground again, and asks the planner
        # once for food and potions.
        w = grid(at=(0, 0))
        w.health = 3
        m, kb = Memory(), KnowledgeBase.empty("sandbox")
        reasons = []
        for t in range(0, REGEN_MEASURE_TICKS + 1, 10):
            w.tick = t
            out = dispatch(w, ctx(m, kb))
            reasons.append(out.reason)
            self.assertEqual(w.pos, (0, 0))
        self.assertTrue(m.heal_regen_absent)
        self.assertFalse(any("heal_measure" in r for r in reasons), reasons)
        asks = [s for s in m.strategist_signals if s["trigger"] == "heal_supplies"]
        self.assertEqual(len(asks), 1)
        self.assertEqual((asks[0]["health"], asks[0]["regen"]), (3, "no"))
        # Off the zone afterwards: no walk back, no new sample, no second ask.
        for t, at in ((REGEN_MEASURE_TICKS + 10, (2, 2)), (REGEN_MEASURE_TICKS + 20, (0, 0))):
            w.tick, w.pos = t, at
            out = dispatch(w, ctx(m, kb))
            self.assertNotEqual(out.state, "Heal")
            self.assertNotIn("heal_measure", out.reason)
        self.assertIsNone(m.heal_regen_sample)
        self.assertEqual(len([s for s in m.strategist_signals if s["trigger"] == "heal_supplies"]), 1)

    def test_heal_supplies_asked_again_after_a_full_heal(self):
        w = grid(at=(2, 2))
        m = Memory(heal_regen_absent=True)
        dispatch(w, ctx(m))
        w.health = 10
        dispatch(w, ctx(m))
        w.health = 4
        dispatch(w, ctx(m))
        self.assertEqual(len([s for s in m.strategist_signals if s["trigger"] == "heal_supplies"]), 2)

    def test_heal_supplies_rearmed_by_a_full_heal_another_state_saw(self):
        # Review: a full heal seen while Fight or Flee held the round re-arms the ask.
        from agentrealm_agent.healing import note_heal_window

        m = Memory(heal_supplies_asked=True)
        w = grid()
        w.health = 10
        note_heal_window(m, w)
        self.assertFalse(m.heal_supplies_asked)
        m.heal_supplies_asked = True
        w.health = 4
        note_heal_window(m, w)
        self.assertTrue(m.heal_supplies_asked)

    def test_dispatch_rearms_the_ask_even_when_a_reflex_above_heal_runs(self):
        m = Memory(heal_supplies_asked=True)
        w = grid()
        w.health = 10
        w.entities = [Entity("npc", 3, (2, 2), "slime")]  # Retreat or Flee holds the round
        out = dispatch(w, ctx(m))
        self.assertNotEqual(out.state, "Heal")
        self.assertFalse(m.heal_supplies_asked)

    def test_a_long_rest_leaves_stuck_detection_untouched(self):
        # Review repro: one-cell zone, regen "yes", health held at 3 for 1300
        # ticks. The safe default used to commit a walk outside the zone each
        # window and escalate it to REVEAL without one move.
        from agentrealm_agent.navigation import stuck as nav_stuck

        w = grid(at=(0, 0))
        w.health = 3
        kb = KnowledgeBase.empty("sandbox")
        save_regen_yes(kb)
        m = Memory()
        for t in range(0, 1301, 10):
            w.tick = t
            out = dispatch(w, ctx(m, kb))
            self.assertEqual((out.state, verbs(out)), ("Heal", ["Wait"]), out.reason)
        self.assertEqual(m.nav_stuck.attempts, {})
        self.assertEqual(m.nav_stuck.stuck_signals, [])
        w.health, w.tick = 10, 1310
        out = dispatch(w, ctx(m, kb))
        self.assertEqual(out.state, "Explore")
        att = nav_stuck.active(m, w)
        self.assertTrue(att is None or att.level < nav_stuck.REVEAL, out.reason)
        self.assertNotIn("reveal", out.reason)

    def test_resting_walks_only_inside_the_zone(self):
        # A zone with an unexplored edge: Heal walks there through zone cells only.
        w = grid(at=(0, 2))
        for y in range(5):
            w.zones[7][(0, y)] = ZoneFact(safe=True)
        kb = KnowledgeBase.empty("sandbox")
        save_regen_yes(kb)
        out = dispatch(w, ctx(Memory(), kb))
        self.assertEqual(out.state, "Heal")
        self.assertEqual(verbs(out), ["SetPosition"])
        step = (out.intents[0]["x"], out.intents[0]["y"])
        self.assertEqual(step[0], 0, "never off the zone column")

    def zone_column(self):
        w = grid(at=(0, 2))
        for y in range(5):
            w.zones[7][(0, y)] = ZoneFact(safe=True)
        kb = KnowledgeBase.empty("sandbox")
        save_regen_yes(kb)
        return w, kb

    def test_an_explore_backoff_on_a_zone_frontier_keeps_heal_off_it(self):
        from agentrealm_agent.navigation import stuck as nav_stuck

        w, kb = self.zone_column()
        m = Memory()
        for cell in ((0, 1), (0, 3)):
            nav_stuck.remember_give_up(m.nav_stuck, nav_stuck.goal_key("explore", 7, cell), "explore", 10_000)
        out = dispatch(w, ctx(m, kb))
        self.assertEqual(out.state, "Heal")
        self.assertRegex(out.reason, r"heal_explore → \(0, [04]\)")  # the next edge cells, not the backed-off ones

    def test_a_heal_backoff_on_a_frontier_keeps_explore_off_it(self):
        from agentrealm_agent.navigation import stuck as nav_stuck
        from agentrealm_agent.pathing import safe_explore_targets

        w, _ = self.zone_column()
        m = Memory()
        nav_stuck.remember_give_up(m.nav_stuck, nav_stuck.goal_key("heal_explore", 7, (0, 1)), "heal_explore", 10_000)
        self.assertNotIn((0, 1), safe_explore_targets(w, m, Policy(kind="scripted")))
        self.assertIn((0, 3), safe_explore_targets(w, m, Policy(kind="scripted")))

    def test_regen_absent_does_not_pull_to_safe_ground(self):
        # Once this run has measured no regen, Heal sends nothing anywhere:
        # the plan's executor (or the safe default) moves instead.
        w = grid(at=(2, 2))
        m = Memory(heal_regen_absent=True)
        out = dispatch(w, ctx(m))
        self.assertNotEqual(out.state, "Heal")
        self.assertIn("Heal: no safe-zone regen this run", out.yielded)
        w.pos = (0, 0)
        out = dispatch(w, ctx(m))
        self.assertNotEqual(out.state, "Heal")


class RearmTheLastWeaponTest(unittest.TestCase):
    """Free-play run 4: a potion left armed by run 3 was what Heal remembered
    to re-arm, so Heal re-armed a potion and no weapon came back."""

    def test_drinking_the_armed_potion_rearms_the_held_weapon(self):
        w = grid()
        w.armed_code = "small_potion"
        w.held_supplies = [InventorySupply(1, "bronze_sword"), InventorySupply(4, "small_potion")]
        m = Memory()
        out = dispatch(w, ctx(m))
        self.assertEqual(out.intents, [{"verb": "Use", "target": {"kind": "self"}}])
        self.assertEqual(m.self_use_rearm, "bronze_sword")
        w.armed_code = None
        w.held_supplies = [InventorySupply(1, "bronze_sword"), InventorySupply(4, "small_potion")]
        self.assertEqual(dispatch(w, ctx(m)).intents, [{"verb": "Arm", "supply_id": 1}])

    def test_a_potion_in_the_slot_is_never_the_weapon_to_rearm(self):
        w = grid()
        w.armed_code = "large_potion"
        w.held_supplies = [InventorySupply(4, "small_potion")]
        m = Memory()
        self.assertEqual(verbs(dispatch(w, ctx(m))), ["Arm", "Use"])
        self.assertIsNone(m.self_use_rearm, "no weapon held: nothing to re-arm, never the potion")

    def test_the_last_weapon_armed_comes_back_over_another_held_one(self):
        w = grid()
        w.health = 10
        w.armed_code = "bronze_sword"
        m = Memory()
        dispatch(w, ctx(m))  # every decision notes the weapon in hand
        self.assertEqual(m.last_weapon, "bronze_sword")
        w.health = 5
        w.armed_code = "torch"  # a tool swapped in since
        w.held_supplies = [
            InventorySupply(1, "pocket_knife"),
            InventorySupply(2, "bronze_sword"),
            InventorySupply(4, "small_potion"),
        ]
        self.assertEqual(verbs(dispatch(w, ctx(m))), ["Arm", "Use"])
        self.assertEqual(m.self_use_rearm, "bronze_sword")


class DrinkTargetsSelfTest(unittest.TestCase):
    """Free-play run 4: the Use named our own id as a ``character`` target
    and drank nothing. The API's target for yourself is ``{"kind": "self"}``."""

    def test_the_drink_uses_the_self_target(self):
        w = grid()
        w.held_supplies = [InventorySupply(4, "small_potion")]
        out = dispatch(w, ctx())
        self.assertEqual(out.intents[-1], {"verb": "Use", "target": {"kind": "self"}})

    def test_a_self_use_is_measured(self):
        from agentrealm_agent.runner import Runner

        r = Runner.__new__(Runner)
        r.world, r.mem = grid(), Memory()
        r.world.armed_code = "small_potion"
        use = {"verb": "Use", "target": {"kind": "self"}}
        r.mem.pending_intents = [use]
        r._note_heal_intent(use, 0)
        self.assertEqual(r.mem.heal_pending, (5, "small_potion", "use"))



# The health line and the one safe cell (A89), on open ground with a cluster
# of safe cells west of the character.
CLUSTER = [(4, 10), (4, 11), (3, 10), (3, 11)]
TRAVEL = {"op": "travel", "to": "point", "x": 30, "y": 10}


def open_ground(health: int):
    from tests.test_survival_runs import world

    return world(health=health)


def line_ctx(plan=None, **params) -> PlayContext:
    from agentrealm_agent.directives import PARAM_DEFAULTS

    policy = Policy(kind="scripted", goals=[], hostile=["npc"])
    return PlayContext(Memory(), policy, random.Random(0), params={**PARAM_DEFAULTS, **params}, plan=plan)


def travel_plan():
    from agentrealm_agent.directives import PARAM_DEFAULTS
    from agentrealm_agent.plan import Plan

    return Plan([dict(TRAVEL)], dict(PARAM_DEFAULTS))


def wall_in(w: WorldModel, cells) -> None:
    """A wall ring round ``cells``: no way in from any side."""
    xs, ys = [p[0] for p in cells], [p[1] for p in cells]
    for x in range(min(xs) - 1, max(xs) + 2):
        for y in range(min(ys) - 1, max(ys) + 2):
            if (x, y) not in cells:
                w.view.tiles[(x, y)] = "wall"


def measure_targets(outs) -> list[str]:
    return [o.reason for o in outs if o.state == "Heal" and o.reason.startswith("heal_measure")]


class LowHealthLineTest(unittest.TestCase):
    """Whether resting is worth leaving the plan's op for is the planner's
    ``low_health`` line: above it the op goes first, at or below it Heal rests."""

    def test_above_the_line_the_plans_op_goes_first(self):
        from tests.test_freeplay_run3 import safe

        w, c = open_ground(9), line_ctx(travel_plan())
        safe(w, *CLUSTER)
        out = dispatch(w, c)
        self.assertEqual(out.state, "Travel")
        self.assertIn("Heal: health above low_health: the plan's travel goes first", out.yielded)

    def test_above_the_line_unreachable_safe_cells_never_hold_off_the_op(self):
        from tests.test_freeplay_run3 import safe, stand_still
        from agentrealm_agent.navigation import stuck as nav_stuck

        w, c = open_ground(9), line_ctx(travel_plan())
        safe(w, *CLUSTER)
        wall_in(w, CLUSTER)
        outs = stand_still(w, c, 6 * nav_stuck.PROGRESS_TICK_LIMIT)
        self.assertEqual(measure_targets(outs), [])
        self.assertNotIn("Heal", {o.state for o in outs})

    def test_at_the_line_heal_rests_first(self):
        from tests.test_freeplay_run3 import safe

        w, c = open_ground(4), line_ctx(travel_plan())
        safe(w, *CLUSTER)
        out = dispatch(w, c)
        self.assertEqual((out.state, out.reason), ("Heal", f"heal_measure → {CLUSTER[0]}"))

    def test_the_planner_moves_the_line(self):
        from tests.test_freeplay_run3 import safe

        w, c = open_ground(9), line_ctx(travel_plan(), low_health=0.9)
        safe(w, *CLUSTER)
        self.assertEqual(dispatch(w, c).state, "Heal")
        w, c = open_ground(4), line_ctx(travel_plan(), low_health=0.3)
        safe(w, *CLUSTER)
        self.assertEqual(dispatch(w, c).state, "Travel")

    def test_the_planner_may_only_raise_the_line(self):
        # A lower line rests and drinks later: a loosening, like any survival param.
        from agentrealm_agent.directives import PARAM_DEFAULTS
        from agentrealm_agent.plan import apply_set_param, collect_rejections, validate_goal_op

        raised = {"op": "set_param", "name": "low_health", "value": 0.8}
        self.assertEqual(apply_set_param(dict(PARAM_DEFAULTS), dict(PARAM_DEFAULTS), raised)["low_health"], 0.8)
        lowered = {"op": "set_param", "name": "low_health", "value": 0.2}
        params, rejected = collect_rejections(lambda: apply_set_param(dict(PARAM_DEFAULTS), dict(PARAM_DEFAULTS), lowered))
        self.assertEqual(params["low_health"], PARAM_DEFAULTS["low_health"])
        self.assertTrue(rejected)
        self.assertIsNone(validate_goal_op({"op": "set_param", "name": "low_health", "value": 1.5}))

    def test_on_safe_ground_above_the_line_no_regen_sample(self):
        from tests.test_freeplay_run3 import safe

        w, c = open_ground(9), line_ctx(travel_plan())
        safe(w, (10, 10), *CLUSTER)
        self.assertEqual(dispatch(w, c).state, "Travel")
        self.assertIsNone(c.memory.heal_regen_sample, "no sample while the plan's op runs")

    def test_with_no_plan_op_it_rests_whatever_the_health(self):
        from tests.test_freeplay_run3 import safe

        w, c = open_ground(9), line_ctx()
        safe(w, *CLUSTER)
        out = dispatch(w, c)
        self.assertEqual((out.state, out.reason), ("Heal", f"heal_measure → {CLUSTER[0]}"))


class OneSafeCellTest(unittest.TestCase):
    """Heal commits to one safe cell. A walk to it that is given up, or a
    check that rules it out, gives it up in the one give-up memory (A90), and
    safe ground with it while that backs off: never the next cell at once."""

    def test_walled_in_safe_cells_are_never_walked_to(self):
        from tests.test_freeplay_run3 import safe
        from tests.test_survival_runs import MAP

        w, c = open_ground(4), line_ctx()
        safe(w, *CLUSTER)
        wall_in(w, CLUSTER)
        out = dispatch(w, c)
        self.assertNotEqual(out.state, "Heal")
        self.assertIn("Heal: no reachable safe tile", out.yielded)
        for cell in CLUSTER:
            self.assertIn((MAP, cell), c.memory.safe_unreachable)
        asks = [s for s in c.memory.strategist_signals if s["trigger"] == "heal_supplies"]
        self.assertEqual([a.get("safe_ground") for a in asks], ["unreachable"])

    def test_a_cell_ruled_out_is_given_up_like_a_walk(self):
        # One reachability for every safe walk: a cell Retreat or Park ruled
        # out is out for Heal too, and gives safe ground up.
        from tests.test_freeplay_run3 import safe
        from tests.test_survival_runs import MAP
        from agentrealm_agent.navigation import stuck as nav_stuck

        w, c = open_ground(7), line_ctx()
        safe(w, *CLUSTER)
        self.assertEqual(dispatch(w, c).reason, f"heal_measure → {CLUSTER[0]}")
        c.memory.safe_unreachable[(MAP, CLUSTER[0])] = w.tick  # as retreat.no_progress marks it
        w.tick += 10
        self.assertNotEqual(dispatch(w, c).state, "Heal")
        self.assertTrue(nav_stuck.backed_off(c.memory, "heal_measure", MAP, CLUSTER[0], w.tick))
        signals = c.memory.nav_stuck.stuck_signals
        self.assertEqual([(s["goal"], s["target"]) for s in signals], [("heal_measure", list(CLUSTER[0]))])
        w.tick += 10
        self.assertIn("Heal: safe ground given up while its walk backs off", dispatch(w, c).yielded)

    def test_cells_it_cannot_reach_do_not_chain(self):
        # Reachable by budget, never nearer: one cell is walked to, given up,
        # and no other is started on while it backs off.
        from tests.test_freeplay_run3 import safe, stand_still
        from tests.test_survival_runs import MAP
        from agentrealm_agent.navigation import stuck as nav_stuck

        w, c = open_ground(7), line_ctx()
        safe(w, *CLUSTER)
        outs = stand_still(w, c, nav_stuck.PROGRESS_TICK_LIMIT + nav_stuck.BACKOFF_BASE_TICKS // 2)
        self.assertEqual(set(measure_targets(outs)), {f"heal_measure → {CLUSTER[0]}"})
        self.assertTrue(nav_stuck.backed_off(c.memory, "heal_measure", MAP, CLUSTER[0], w.tick))
        self.assertNotEqual(outs[-1].state, "Heal")
        asks = [s for s in c.memory.strategist_signals if s["trigger"] == "heal_supplies"]
        self.assertEqual(len(asks), 1)
        self.assertEqual(asks[0]["safe_ground"], "unreachable")
        self.assertNotIn("regen", asks[0], "regen was never measured")

    def test_safe_ground_comes_back_when_the_give_up_lapses(self):
        # A give-up that never lapsed would shut every safe cell out, even
        # reachable ones; it lapses with its backoff, whatever the health.
        from tests.test_freeplay_run3 import safe, stand_still
        from agentrealm_agent.navigation import stuck as nav_stuck

        w, c = open_ground(7), line_ctx()
        safe(w, *CLUSTER)
        stand_still(w, c, nav_stuck.PROGRESS_TICK_LIMIT + 20)
        until = max(r.until for r in c.memory.nav_stuck.given_up.values() if r.goal == "heal_measure")
        w.tick = until - 1
        self.assertNotEqual(dispatch(w, c).state, "Heal", "still given up")
        w.tick = until
        out = dispatch(w, c)
        self.assertEqual(out.state, "Heal")
        self.assertTrue(out.reason.startswith("heal_measure → "), out.reason)


def ringed(w: WorldModel, size: int) -> WorldModel:
    """A wall ring round the ``size`` grid: no way round through unknown ground."""
    for i in range(-1, size + 1):
        for p in ((i, -1), (i, size), (-1, i), (size, i)):
            w.view.tiles[p] = "wall"
    return w


class FoodByPriceTest(unittest.TestCase):
    """Food within ``FOOD_REACH`` is picked by one price: its walk on Heal's
    ground, a block on the straight way a held item opens costed at its break."""

    def test_the_cheapest_walk_wins_not_the_nearest_cell(self):
        w = grid(at=(3, 4), size=8)
        w.zones[7] = {}
        for x in range(0, 7):
            w.view.tiles[(x, 3)] = "wall"  # the near food is behind a wall, the way round is long
        w.entities = [Entity("supply", 1, (3, 2), "apple"), Entity("supply", 2, (6, 6), "apple")]
        out = dispatch(w, ctx())
        self.assertEqual(out.state, "Heal")
        self.assertEqual(out.reason, "heal_food → (6, 6)")

    def test_food_behind_a_bush_a_held_blade_cuts(self):
        w = ringed(grid(at=(1, 2), size=6), 6)
        w.zones[7] = {}
        for y in range(6):
            w.view.tiles[(2, y)] = "bush"  # no way round: the bush is the way
        w.held_supplies = [InventorySupply(5, "bronze_sword")]
        w.armed_code = "bronze_sword"
        w.entities = [Entity("supply", 1, (3, 2), "apple")]
        out = dispatch(w, ctx())
        self.assertEqual(out.state, "Heal")
        self.assertTrue(out.reason.startswith("heal_food: break cut @ (2, 2)"), out.reason)
        self.assertIn({"verb": "Use", "target": {"kind": "block", "x": 2, "y": 2}}, out.intents)

    def test_a_walk_round_cheaper_than_the_cut_is_walked(self):
        w = grid(at=(1, 2), size=6)
        w.zones[7] = {}
        w.view.tiles[(2, 2)] = "bush"  # one bush, open ground round it
        w.held_supplies = [InventorySupply(5, "bronze_sword")]
        w.armed_code = "bronze_sword"
        w.entities = [Entity("supply", 1, (3, 2), "apple")]
        out = dispatch(w, ctx())
        self.assertEqual((out.state, out.reason), ("Heal", "heal_food → (3, 2)"))

    def test_food_no_walk_reaches_is_no_target(self):
        w = ringed(grid(at=(1, 2), size=6), 6)
        w.zones[7] = {}
        for y in range(6):
            w.view.tiles[(2, y)] = "wall"
        w.entities = [Entity("supply", 1, (3, 2), "apple")]
        out = dispatch(w, ctx())
        self.assertNotEqual(out.state, "Heal")


class HealWalkOpensItsPricedWayTest(unittest.TestCase):
    """The block Heal opens is the first one its priced way crosses, on
    every Heal walk, food or not."""

    def test_the_block_opened_is_the_one_the_way_crosses(self):
        # Two bushes on the straight route; the cheapest way goes round the
        # first (open ground above it) and cuts the second.
        w = ringed(grid(at=(0, 2), size=6), 6)
        w.zones[7] = {}
        w.view.tiles[(1, 2)] = "bush"
        for y in range(6):
            w.view.tiles[(3, y)] = "bush"  # a full column: the way must cut one
        w.held_supplies = [InventorySupply(5, "bronze_sword")]
        w.armed_code = "bronze_sword"
        w.entities = [Entity("supply", 1, (5, 2), "apple")]
        from agentrealm_agent.states import heal

        c = ctx()
        with unittest.mock.patch.object(heal, "FOOD_REACH", 5):
            out = dispatch(w, c)
        self.assertEqual((out.state, out.reason), ("Heal", "heal_food → (5, 2)"))
        self.assertEqual(out.intents, [{"verb": "SetPosition", "x": 1, "y": 1}], "round the first bush")
        self.assertIn((3, 2), c.memory.path, "the walk crosses the block its price cut")
        self.assertEqual(c.memory.nav_stuck.stuck_signals, [], "food priced reachable is never given up")
        # Beside the block, Heal opens it.
        w.pos = c.memory.path[c.memory.path.index((3, 2)) - 1]
        with unittest.mock.patch.object(heal, "FOOD_REACH", 5):
            out = dispatch(w, c)
        self.assertEqual((out.state, out.reason), ("Heal", "heal_food: break cut @ (3, 2)"))

    def test_the_walk_to_a_block_out_of_reach_is_heals_own(self):
        # The straight way to the bush column runs through a pack's reach; a
        # longer way goes round it. Out of the blade's reach, the walk to the
        # block is Heal's: on its ground (the reach costly), bounded by stuck
        # detection, and its goal kept, so a hostile seen later replans it.
        from agentrealm_agent.states import heal
        from agentrealm_agent.survival import hostile_reach

        w = ringed(grid(at=(0, 0), size=11), 11)
        w.zones[7] = {}
        for y in range(11):
            w.view.tiles[(8, y)] = "bush"
        w.held_supplies = [InventorySupply(5, "bronze_sword")]
        w.armed_code = "bronze_sword"
        w.hostile_types.add(("npc", "wartlurch"))
        w.entities = [Entity("supply", 1, (10, 0), "apple"), Entity("npc", 20, (3, 1), "wartlurch")]
        c = ctx()
        with unittest.mock.patch.object(heal, "FOOD_REACH", 10):
            out = dispatch(w, c)
        m = c.memory
        self.assertEqual((out.state, out.reason), ("Heal", "heal_food → (10, 0)"))
        self.assertEqual(m.goal, "heal_food")
        self.assertIn((8, 0), m.path, "the way cuts the bush its price chose")
        self.assertTrue(hostile_reach(w, c.policy).isdisjoint(m.path), m.path)
        self.assertIsNotNone(heal.nav_stuck.active(m, w), "the walk is a bounded attempt")

    def test_a_walk_to_the_safe_zones_edge_cuts_through(self):
        # Resting on safe ground: the zone's unexplored edge lies past a
        # bush column a held blade cuts, so Heal opens it like any walk.
        w = ringed(grid(at=(0, 2), size=6), 6)
        w.zones[7] = {(x, y): ZoneFact(safe=True) for x in range(6) for y in range(6)}
        for y in range(6):
            w.view.tiles[(1, y)] = "bush"
        for y in range(6):
            del w.view.tiles[(5, y)]  # unseen ground in the zone: its edge is at x 4
            w.view.tiles.pop((6, y), None)
        w.held_supplies = [InventorySupply(5, "bronze_sword")]
        w.armed_code = "bronze_sword"
        kb = KnowledgeBase.empty("sandbox")
        save_regen_yes(kb)
        out = dispatch(w, ctx(kb=kb))
        self.assertEqual(out.state, "Heal")
        self.assertIn("heal_explore: break", out.reason)


if __name__ == "__main__":
    unittest.main()
