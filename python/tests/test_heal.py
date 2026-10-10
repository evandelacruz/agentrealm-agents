"""A10: Heal state and healing helpers."""

import random
import unittest

from agentrealm_agent.brain import decide
from agentrealm_agent.config import Policy
from agentrealm_agent.executor import DEFAULT_WEAPON_COOLDOWN_TICKS
from agentrealm_agent.healing import (
    HEAL_MAX_TRIES,
    REGEN_KEY,
    REGEN_MEASURE_TICKS,
    SURVIVAL_KEY,
    absorb_heal_pending,
    hurt,
    note_heal_pending,
    regen_known,
    save_regen_yes,
)
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

    def test_rejected_take_is_not_retried_forever(self):
        w = grid(at=(1, 1))
        w.entities = [Entity("supply", 8, (2, 1), "apple")]
        m = Memory()
        for _ in range(HEAL_MAX_TRIES):
            self.assertEqual(verbs(dispatch(w, ctx(m))), ["Take"])
            w.tick += 7
        self.assertNotIn("Take", verbs(dispatch(w, ctx(m))))

    def test_carried_food_before_potion(self):
        w = grid()
        w.held_supplies = [InventorySupply(4, "small_potion"), InventorySupply(5, "berry")]
        out = dispatch(w, ctx())
        self.assertEqual(out.intents[0], {"verb": "Arm", "supply_id": 5})
        self.assertEqual(verbs(out), ["Arm", "Use"])

    def test_drinks_carried_potion_and_caps_rejected_use(self):
        w = grid()
        w.held_supplies = [InventorySupply(4, "small_potion")]
        m = Memory()
        for _ in range(HEAL_MAX_TRIES):
            self.assertEqual(verbs(dispatch(w, ctx(m))), ["Arm", "Use"])
            w.tick += 7
        self.assertNotIn("Use", verbs(dispatch(w, ctx(m))))

    def test_rearms_weapon_after_drinking_potion(self):
        w = grid()
        w.armed_code = "pocket_knife"
        w.held_supplies = [InventorySupply(1, "pocket_knife"), InventorySupply(4, "small_potion")]
        m = Memory()
        out = dispatch(w, ctx(m))
        self.assertEqual(verbs(out), ["Arm", "Use"])
        self.assertEqual(m.heal_rearm, "pocket_knife")
        w.armed_code = None
        w.held_supplies = [InventorySupply(1, "pocket_knife")]
        out = dispatch(w, ctx(m))
        self.assertEqual(verbs(out), ["Arm"])
        self.assertIsNone(m.heal_rearm)

    def test_rearms_weapon_once_the_drink_heals_to_full(self):
        # A potion that fills health ends the hurt guard; the re-arm still runs.
        w = grid()
        w.armed_code = "small_potion"
        w.health = 10
        w.held_supplies = [InventorySupply(1, "pocket_knife")]
        m = Memory(heal_rearm="pocket_knife")
        out = dispatch(w, ctx(m))
        self.assertEqual(out.state, "Heal")
        self.assertEqual(out.intents, [{"verb": "Arm", "supply_id": 1}])
        self.assertIsNone(m.heal_rearm)
        w.armed_code = "pocket_knife"
        self.assertNotEqual(dispatch(w, ctx(m)).state, "Heal")

    def test_rearms_weapon_with_a_hostile_in_range(self):
        w = grid()
        w.health = 10
        w.armed_code = "small_potion"
        w.held_supplies = [InventorySupply(1, "pocket_knife")]
        w.entities = [Entity("npc", 3, (2, 2), "slime")]
        m = Memory(heal_rearm="pocket_knife")
        out = dispatch(w, ctx(m))
        self.assertEqual(out.state, "Heal")
        self.assertEqual(out.intents, [{"verb": "Arm", "supply_id": 1}])

    def test_the_weapon_goes_back_before_a_second_potion(self):
        # Free-play run 3: heal_rearm held from 191 s to the end. It never
        # outlives one drink now, so Equip is never kept waiting on it.
        w = grid()
        w.armed_code = "small_potion"
        w.held_supplies = [InventorySupply(1, "pocket_knife"), InventorySupply(4, "small_potion")]
        m = Memory(heal_rearm="pocket_knife")
        out = dispatch(w, ctx(m))
        self.assertEqual(out.intents, [{"verb": "Arm", "supply_id": 1}])
        self.assertIsNone(m.heal_rearm)
        w.armed_code = "pocket_knife"
        w.held_supplies = [InventorySupply(4, "small_potion")]
        self.assertEqual(verbs(dispatch(w, ctx(m))), ["Arm", "Use"])
        self.assertEqual(m.heal_rearm, "pocket_knife")

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
        r.mem = Memory(need_self=False, need_position=False, heal_rearm="pocket_knife")
        self.assertIsNone(r.reflex_while_held())
        self.assertEqual(r.mem.heal_rearm, "pocket_knife")

    def test_a_drink_with_nothing_armed_leaves_nothing_to_rearm(self):
        w = grid()
        w.held_supplies = [InventorySupply(4, "small_potion")]
        m = Memory()
        self.assertEqual(verbs(dispatch(w, ctx(m))), ["Arm", "Use"])
        self.assertIsNone(m.heal_rearm)

    def test_weapon_gone_clears_rearm(self):
        w = grid()
        w.health = 10
        m = Memory(heal_rearm="pocket_knife")
        self.assertNotEqual(dispatch(w, ctx(m)).state, "Heal")
        self.assertIsNone(m.heal_rearm)

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
        save_regen_yes(kb)
        m = Memory()
        out = dispatch(w, ctx(m, kb=kb))
        self.assert_heals_in_zone(out)
        self.assertIsNone(m.heal_regen_sample)  # known: no sample taken

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
            m.nav_stuck.backoff_until[nav_stuck.goal_key("explore", 7, cell)] = 10_000
        out = dispatch(w, ctx(m, kb))
        self.assertEqual(out.state, "Heal")
        self.assertRegex(out.reason, r"heal_explore → \(0, [04]\)")  # the next edge cells, not the backed-off ones

    def test_a_heal_backoff_on_a_frontier_keeps_explore_off_it(self):
        from agentrealm_agent.navigation import stuck as nav_stuck
        from agentrealm_agent.pathing import safe_explore_targets

        w, _ = self.zone_column()
        m = Memory()
        m.nav_stuck.backoff_until[nav_stuck.goal_key("heal_explore", 7, (0, 1))] = 10_000
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
        self.assertEqual(m.heal_rearm, "bronze_sword")
        w.armed_code = None
        w.held_supplies = [InventorySupply(1, "bronze_sword"), InventorySupply(4, "small_potion")]
        self.assertEqual(dispatch(w, ctx(m)).intents, [{"verb": "Arm", "supply_id": 1}])

    def test_a_potion_in_the_slot_is_never_the_weapon_to_rearm(self):
        w = grid()
        w.armed_code = "large_potion"
        w.held_supplies = [InventorySupply(4, "small_potion")]
        m = Memory()
        self.assertEqual(verbs(dispatch(w, ctx(m))), ["Arm", "Use"])
        self.assertIsNone(m.heal_rearm, "no weapon held: nothing to re-arm, never the potion")

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
        self.assertEqual(m.heal_rearm, "bronze_sword")


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


if __name__ == "__main__":
    unittest.main()
