"""A38: Boss state, plan preconditions, fight clock, defeat from boss health.
A163: the clock is counted from the boss-door step, and Boss leaves by the exit door."""

import random
import unittest
from unittest import mock

from agentrealm_adk import boss_clock
from agentrealm_adk.config import Policy
from agentrealm_adk.directives import PARAM_DEFAULTS
from agentrealm_adk.item_table import InventorySupply
from agentrealm_adk.knowledge_base import KnowledgeBase
from agentrealm_adk.memory import BossClock, BossFight, Memory
from agentrealm_adk.survival import health_left
from agentrealm_agent.plan import Plan, goal_done, validate_goal_op
from agentrealm_agent.states import PlayContext, dispatch
from agentrealm_adk.navigation import cost_path
from agentrealm_agent.states.boss import (
    LEAVE_MARGIN_SECONDS,
    boss_fight_on,
    clock_nearly_out,
    exit_steps,
    boss_attack_queue,
    fight_boss_preconditions_met,
    potion_count,
    sync_boss,
)
from agentrealm_agent.states.fight import fight_target
from agentrealm_agent.states.flee import should_flee
from agentrealm_adk.world import Entity, Passage, WorldModel, ZoneFact


def grid(rows: list[str], at=(0, 0), map_id=8) -> WorldModel:
    glyph = {".": "dirt", "#": "wall", "D": "framed_door"}
    w = WorldModel(character_id=1, map_id=map_id, pos=at, perception=5)
    for y, row in enumerate(rows):
        for x, g in enumerate(row):
            w.view.tiles[(x, y)] = glyph.get(g, g)
    w.apply_minimap({"maps": [{"map_id": map_id, "level": 1}]})
    w.apply_position({"map_id": map_id, "x": at[0], "y": at[1]})
    w.health, w.max_health = 10, 10
    # A sword: the knife's damage at the 1 s attack cooldown the win-estimate
    # cases here are priced on (the knife itself swings every 0.8 s).
    w.armed_code = "sword"
    return w


def ctx(w: WorldModel, m: Memory, plan: Plan | None = None, **policy) -> PlayContext:
    return PlayContext(
        m,
        Policy(kind="scripted", goals=["explore"], **policy),
        random.Random(0),
        params=dict(PARAM_DEFAULTS),
        plan=plan,
    )


def boss_plan(x=0, y=0, **fields) -> Plan:
    return Plan([{"op": "fight_boss", "x": x, "y": y, **fields}], dict(PARAM_DEFAULTS))


def boss(health=20, pos=(2, 0), eid=50) -> Entity:
    return Entity("npc", eid, pos, "cellar_boss", health=health, max_health=30)


def in_room(w: WorldModel, m: Memory, landing=(1, 0), door_tick=None) -> Memory:
    """The clock started on a step through the boss door that landed on ``landing``."""
    m.boss_clock = BossClock(w.tick if door_tick is None else door_tick, w.map_id, landing)
    return m


class FightBossOpTest(unittest.TestCase):
    def test_validates_precondition_fields(self):
        op = validate_goal_op(
            {
                "op": "fight_boss",
                "x": 4,
                "y": 0,
                "min_health": 8,
                "min_potions": 2,
                "armed": "bronze_sword",
                "worn": ["bronze_mail"],
            }
        )
        self.assertEqual(op["min_health"], 8)

    def test_drops_bad_worn(self):
        self.assertIsNone(validate_goal_op({"op": "fight_boss", "x": 0, "y": 0, "worn": "mail"}))

    def test_goal_done_never_pops_fight_boss(self):
        w = grid(["..."])
        w.entities = [boss(health=0)]
        plan = boss_plan()
        self.assertFalse(goal_done(plan.current(), w, plan))


class PreconditionsTest(unittest.TestCase):
    def test_health_and_gear(self):
        w = grid(["."])
        w.health = 7
        w.armed_code = "knife"
        op = {"op": "fight_boss", "x": 0, "y": 0, "min_health": 8, "armed": "bronze_sword"}
        ok, reason = fight_boss_preconditions_met(w, op)
        self.assertFalse(ok)
        self.assertIn("health", reason)
        w.health = 10
        w.armed_code = "bronze_sword"
        self.assertTrue(fight_boss_preconditions_met(w, op)[0])

    def test_potion_count(self):
        w = grid(["."])
        w.held_supplies = [InventorySupply(1, "small_potion"), InventorySupply(2, "apple")]
        self.assertEqual(potion_count(w), 1)


class BossStateTest(unittest.TestCase):
    def test_approaches_door_when_preconditions_met(self):
        w = grid(["..D"])
        m = Memory()
        out = dispatch(w, ctx(w, m, boss_plan(3, 0)))
        self.assertEqual(out.state, "Boss")
        self.assertEqual((out.intents[0]["x"], out.intents[0]["y"]), (1, 0))
        self.assertIsNone(m.boss)

    def test_waits_when_preconditions_fail(self):
        w = grid(["..D"])
        w.health = 3
        m = Memory()
        out = dispatch(w, ctx(w, m, boss_plan(3, 0, min_health=10)))
        # Not ready: Boss asks the planner and holds; the safe default never walks for it (A120).
        self.assertEqual((out.state, out.intents, out.wait), ("Boss", None, True), out.reason)
        asks = [s["why"] for s in m.strategist_signals if s["trigger"] == "no_way_on"]
        self.assertEqual(asks, ["fight_boss preconditions not met: health 3 < 10"])

    def test_fights_boss_without_retreat_tail(self):
        w = grid(["..."], at=(1, 0), map_id=9)
        w.entities = [boss()]
        m = Memory()
        out = dispatch(w, ctx(w, m, boss_plan()))
        self.assertEqual(out.state, "Boss")
        self.assertTrue(out.paced)
        self.assertTrue(all(i.get("verb") == "Use" for i in out.intents[:3]))
        self.assertEqual(m.boss.boss_id, 50)


class DefeatTest(unittest.TestCase):
    def test_dead_boss_still_listed_pops_op_and_clears_memory(self):
        w = grid(["..."], at=(1, 0), map_id=9)
        w.entities = [boss()]
        plan = Plan(
            [{"op": "fight_boss", "x": 0, "y": 0}, {"op": "wait", "seconds": 5}],
            dict(PARAM_DEFAULTS),
        )
        m = Memory()
        self.assertEqual(dispatch(w, ctx(w, m, plan)).state, "Boss")
        w.tick += 10
        w.entities = [boss(health=0)]
        for _ in range(3):
            out = dispatch(w, ctx(w, m, plan))
            self.assertNotEqual(out.state, "Boss")
        self.assertEqual(plan.current()["op"], "wait")
        self.assertIsNone(m.boss)

    def test_boss_out_of_view_is_not_defeat(self):
        w = grid(["..."], at=(1, 0), map_id=9)
        w.entities = [boss()]
        plan = boss_plan()
        m = Memory()
        sync_boss(w, m, plan)
        w.entities = []
        sync_boss(w, m, plan)
        self.assertEqual(plan.current()["op"], "fight_boss")
        self.assertIsNotNone(m.boss, "the assumed clock runs: still the fight, not finished")
        w.apply_position({"map_id": 8, "x": 0, "y": 0})
        sync_boss(w, m, plan)
        self.assertEqual(plan.current()["op"], "fight_boss")
        self.assertIsNone(m.boss)  # off the room's map, no clock: disengaged, not finished

    def test_boss_out_of_view_with_clock_stays_engaged(self):
        w = grid(["..."], at=(1, 0), map_id=9)
        w.entities = [boss()]
        plan = boss_plan()
        m = in_room(w, Memory())
        sync_boss(w, m, plan)
        w.entities = []
        sync_boss(w, m, plan)
        self.assertIsNotNone(m.boss)
        self.assertEqual(plan.current()["op"], "fight_boss")

    def test_level_clear_pops_op(self):
        w = grid(["..."], at=(1, 0), map_id=9)
        w.entities = [boss()]
        plan = boss_plan()
        m = Memory()
        sync_boss(w, m, plan)
        w.tick += 5
        w.entities = []  # teleported outside the level on the clear tick
        w.note_level_clear({"level_number": 1, "max_health_gain": 5})
        sync_boss(w, m, plan)
        self.assertIsNone(plan.current())
        self.assertIsNone(m.boss)


class BossMemoryTest(unittest.TestCase):
    def engaged(self) -> tuple[WorldModel, Memory, Plan]:
        w = grid(["..."], at=(1, 0), map_id=9)
        w.entities = [boss()]
        plan = boss_plan()
        m = Memory()
        sync_boss(w, m, plan)
        self.assertIsNotNone(m.boss)
        return w, m, plan

    def test_cleared_on_drop(self):
        w, m, plan = self.engaged()
        plan.drop_current("test")
        sync_boss(w, m, plan)
        self.assertIsNone(m.boss)

    def test_cleared_on_reload(self):
        w, m, _ = self.engaged()
        w.entities = []
        sync_boss(w, m, boss_plan())  # an equal op on a new stack is a new fight
        self.assertIsNone(m.boss)

    def test_reload_with_boss_in_view_starts_a_new_fight(self):
        w, m, plan = self.engaged()
        reloaded = boss_plan()
        sync_boss(w, m, reloaded)
        self.assertIs(m.boss.op, reloaded.current())

    def test_cleared_on_death(self):
        w, m, plan = self.engaged()
        w.alive = False
        sync_boss(w, m, plan)
        self.assertIsNone(m.boss)

    def test_not_engaged_on_door_before_boss_seen(self):
        w = grid(["..D"], at=(2, 0))
        m = Memory()
        out = dispatch(w, ctx(w, m, boss_plan(2, 0)))
        self.assertEqual(out.state, "Boss")
        self.assertTrue(out.wait)
        self.assertIsNone(m.boss)


class RetreatCommitTest(unittest.TestCase):
    def hurt_in_fight(self) -> tuple[WorldModel, Memory, Plan]:
        # Door at (0, 0), boss adjacent at (3, 0), a safe tile below; one hit could kill.
        w = grid(["D....", "#####"], at=(2, 0), map_id=9)
        w.view.tiles[(2, 1)] = "dirt"
        w.health = 1
        w.zones[9] = {(2, 1): ZoneFact(safe=True)}
        w.entities = [boss(pos=(3, 0))]
        return w, in_room(w, Memory()), boss_plan(5, 5)

    def test_retreats_out_when_path_and_clock_allow(self):
        w, m, plan = self.hurt_in_fight()
        out = dispatch(w, ctx(w, m, plan, on_hostile="fight"))
        self.assertEqual(out.state, "Boss")
        self.assertIn("retreat out", out.reason)
        self.assertEqual((out.intents[0]["x"], out.intents[0]["y"]), (1, 0))

    def test_retreats_out_from_a_boss_it_would_lose_to_standing_off(self):
        """Review on #131: a boss two cells off and not moving is still one to leave."""
        w = grid(["D.....", "######"], at=(2, 0), map_id=9)
        w.entities = [boss(pos=(4, 0))]
        out = dispatch(w, ctx(w, in_room(w, Memory()), boss_plan(5, 5), on_hostile="fight"))
        self.assertEqual(out.state, "Boss")
        self.assertIn("retreat out", out.reason)
        self.assertEqual((out.intents[0]["x"], out.intents[0]["y"]), (1, 0))

    def test_leaves_by_the_exit_door_near_the_limit(self):
        """Manual §11: before the limit, walk out through the room's exit door (A163)."""
        w, m, plan = self.hurt_in_fight()
        w.health = 10  # not hurt, and a boss nearly beaten: only the clock sends it out
        w.entities = [boss(health=2, pos=(3, 0))]
        w.tick = 5000
        c = ctx(w, m, plan, on_hostile="fight")
        limit = boss_clock.DEFAULT_LIMIT_SECONDS * c.tick_hz
        # Two steps to the exit door and the margin left: time to go.
        m.boss_clock = BossClock(w.tick - limit + 2 * 4 + LEAVE_MARGIN_SECONDS * c.tick_hz, 9, (1, 0))
        out = dispatch(w, c)
        self.assertEqual(out.state, "Boss")
        self.assertIn("leave before the limit", out.reason)
        self.assertEqual((out.intents[0]["x"], out.intents[0]["y"]), (1, 0))
        # A minute earlier it fights on.
        m.boss_clock = BossClock(w.tick - limit + 600, 9, (1, 0))
        out = dispatch(w, c)
        self.assertEqual(out.intents[0]["verb"], "Use")

    def test_leaves_near_the_limit_with_the_boss_out_of_view_or_forbidden(self):
        """Review on #262: no early return keeps it in the room past the limit."""
        for case in ("out of view", "forbidden"):
            w, m, plan = self.hurt_in_fight()
            w.health = 10
            c = ctx(w, m, plan, on_hostile="fight")
            m.boss = BossFight(plan.current(), 50, w.tick)
            m.boss_clock.door_tick = w.tick - boss_clock.DEFAULT_LIMIT_SECONDS * c.tick_hz + 5
            if case == "out of view":
                w.entities = []
            with mock.patch("agentrealm_agent.states.boss.attack_forbidden", return_value=case == "forbidden"):
                out = dispatch(w, c)
            self.assertEqual(out.state, "Boss", case)
            self.assertIn("leave before the limit", out.reason, case)
            self.assertEqual((out.intents[0]["x"], out.intents[0]["y"]), (1, 0), case)

    def test_walks_to_the_rooms_exit_door_not_the_ops_boss_door(self):
        """Finding 38: the op's boss door is on the map outside; out is by the room's door."""
        w, m, plan = self.hurt_in_fight()
        w.view.tiles[(4, 0)] = "framed_door"
        w.entities = [boss(pos=(3, 0))]
        in_room(w, m, landing=(1, 0))
        self.assertEqual(boss_clock.exit_door(m, w, None), (0, 0))
        in_room(w, m, landing=(4, 1))
        self.assertEqual(boss_clock.exit_door(m, w, None), (4, 0))

    def test_commits_with_no_way_out(self):
        w, m, plan = self.hurt_in_fight()
        w.view.tiles[(1, 0)] = "wall"
        out = dispatch(w, ctx(w, m, plan, on_hostile="fight"))
        self.assertEqual(out.state, "Boss")
        self.assertEqual(out.intents[0]["verb"], "Use")

    def test_retreat_and_flee_stand_down(self):
        w, m, plan = self.hurt_in_fight()
        c = ctx(w, m, plan)
        self.assertNotEqual(dispatch(w, c).state, "Retreat")
        self.assertFalse(should_flee(w, c))


class FightDeferralTest(unittest.TestCase):
    def test_fight_ignores_boss(self):
        w = grid(["..."], at=(1, 0))
        policy = Policy(kind="scripted", on_hostile="fight")
        w.entities = [boss(pos=(2, 0))]
        self.assertIsNone(fight_target(w, policy, []))
        w.entities.append(Entity("npc", 7, (0, 0), "snotling"))
        w.hostile_types.add(("npc", "snotling"))  # a type seen attacking (survival.is_hostile)
        self.assertEqual(fight_target(w, policy, []).id, 7)


class BossEntityParsingTest(unittest.TestCase):
    def test_snapshot_npc_health(self):
        w = WorldModel(character_id=1)
        w.apply_observation({"complete": True, "snapshot": {"entities": {"npcs": [
            {"id": 50, "x": 2, "y": 0, "npc_type_code": "cellar_boss", "health": 20, "max_health": 30},
            {"id": 7, "x": 1, "y": 0, "npc_type_code": "snotling"},
        ]}}})
        by_id = {e.id: e for e in w.entities}
        self.assertEqual((by_id[50].health, by_id[50].max_health), (20, 30))
        self.assertTrue(by_id[50].is_boss)
        self.assertFalse(by_id[7].is_boss)

    def test_delta_changes_boss_health(self):
        w = WorldModel(character_id=1)
        w.apply_observation({"complete": True, "snapshot": {"entities": {"npcs": [
            {"id": 50, "x": 2, "y": 0, "health": 20, "max_health": 30},
        ]}}})
        w.apply_observation({"version": "2", "delta": {"entities": {"npcs": {"changed": [
            {"id": 50, "x": 2, "y": 0, "health": 0, "max_health": 30},
        ]}}}})
        self.assertEqual(w.entities[0].health, 0)


class BossClockTest(unittest.TestCase):
    """A163: no read serves the limit or the time left; it is counted."""

    def stepped_through(self, tick=400) -> tuple[WorldModel, Memory, Plan]:
        w = grid(["D.."], at=(1, 0), map_id=9)
        w.tick = tick
        w.map_levels[9] = 2
        w.passage = Passage(8, (5, 5), 9, tick, (1, 0))
        plan = boss_plan(5, 5)
        m = Memory()
        sync_boss(w, m, plan)
        return w, m, plan

    def test_the_clock_starts_on_the_boss_door_step(self):
        w, m, plan = self.stepped_through()
        self.assertEqual((m.boss_clock.door_tick, m.boss_clock.room_map, m.boss_clock.landing), (400, 9, (1, 0)))
        w.tick = 1000
        sync_boss(w, m, plan)
        self.assertEqual(m.boss_clock.door_tick, 400, "counted from the step, not restarted")
        self.assertEqual(boss_clock.ticks_left(m, w, None, 10), 400 + 3000 - 1000, "5 minutes when the level sets none")
        self.assertTrue(boss_clock.running(m, w))

    def test_another_door_starts_no_clock(self):
        w = grid(["D.."], at=(1, 0), map_id=9)
        w.passage = Passage(8, (4, 4), 9, 400, (1, 0))
        m = Memory()
        sync_boss(w, m, boss_plan(5, 5))
        self.assertIsNone(m.boss_clock)

    def test_the_levels_limit_is_kept_in_the_knowledge_base(self):
        w, m, _ = self.stepped_through()
        kb = KnowledgeBase.empty("olympuff")
        kb.levels["2"] = {boss_clock.LIMIT: 90}
        self.assertEqual(boss_clock.ticks_left(m, w, kb, 10), 900)

    def test_a_death_the_clock_explains_is_the_limit_and_is_kept(self):
        w, m, _ = self.stepped_through()
        kb = KnowledgeBase.empty("olympuff")
        died = [{"kind": "Damaged", "amount": 2, "tick": 1600}, {"kind": "Died", "map_id": 8, "tick": 1600}]
        self.assertEqual(boss_clock.note_death(m, w, kb, died, 9, 9, 10), 120, "the chest lands outside: map_before counts")
        self.assertEqual(kb.levels["2"][boss_clock.LIMIT], 120)
        later = [{"kind": "Died", "map_id": 8, "tick": 2200}]
        self.assertEqual(boss_clock.note_death(m, w, kb, later, 9, 9, 10), 180, "a fight outlived the kept limit")

    def test_any_other_death_leaves_the_limit_unchanged(self):
        w, m, _ = self.stepped_through()
        kb = KnowledgeBase.empty("olympuff")
        kb.levels["2"] = {boss_clock.LIMIT: 120}
        cases = {
            "the boss's hit killed": ([{"kind": "Damaged", "amount": 9, "tick": 1700}, {"kind": "Died", "tick": 1700}], 9),
            "a hit with no amount explains nothing": ([{"kind": "Damaged", "tick": 1700}, {"kind": "Died", "tick": 1700}], 9),
            "off the room's map": ([{"kind": "Died", "tick": 1700}], 8),
            "sooner than the kept limit": ([{"kind": "Died", "tick": 1000}], 9),
            "past the longest limit": ([{"kind": "Died", "tick": 400 + 6010}], 9),
        }
        for why, (events, map_before) in cases.items():
            self.assertIsNone(boss_clock.note_death(m, w, kb, events, 9, map_before, 10), why)
            self.assertEqual(kb.levels["2"][boss_clock.LIMIT], 120, why)

    def test_the_clock_stops_when_the_boss_op_ends_in_the_room(self):
        """Review on #262: Retreat, Flee and Heal stand down only while the fight is on."""
        w, m, plan = self.stepped_through()
        plan.drop_current("test")
        sync_boss(w, m, plan)
        self.assertIsNone(m.boss_clock)
        self.assertFalse(boss_fight_on(w, ctx(w, m, plan)))

    def test_a_defeat_stops_the_clock(self):
        w, m, plan = self.stepped_through()
        w.entities = [boss(health=0)]
        sync_boss(w, m, plan)
        self.assertIsNone(m.boss_clock)

    def test_a_dark_room_keeps_its_doors(self):
        """Review on #262: the exit door once seen is the way out out of view too."""
        w, m, plan = self.stepped_through()
        self.assertEqual(boss_clock.exit_door(m, w, None), (0, 0))
        w.view.tiles[(0, 0)] = "?"
        sync_boss(w, m, plan)
        self.assertEqual(boss_clock.exit_door(m, w, None), (0, 0))

    def test_no_door_known_walks_back_to_the_landing(self):
        w = grid(["#######", "#.....#", "#.....#", "#######"], at=(5, 1), map_id=9)
        m = in_room(w, Memory(), landing=(1, 1))
        self.assertIsNone(boss_clock.exit_door(m, w, None))
        self.assertEqual(boss_clock.way_out(m, w, None), (1, 1))
        w.entities = [boss(health=2, pos=(4, 1))]
        c = ctx(w, m, boss_plan(5, 5))
        m.boss_clock.door_tick = w.tick - boss_clock.DEFAULT_LIMIT_SECONDS * c.tick_hz + 5
        out = dispatch(w, c)
        self.assertIn("leave before the limit", out.reason)
        self.assertEqual((out.intents[0]["x"], out.intents[0]["y"]), (4, 2), "round the boss, toward the landing")

    def test_a_fight_with_no_door_step_seen_assumes_the_shortest_limit(self):
        """Review on #262: after a restart, or in by another door, Retreat and
        Flee stand down only while a counted clock says there is time."""
        w = grid(["D....", "#####"], at=(2, 0), map_id=9)
        w.health = 1
        w.entities = [boss(pos=(3, 0))]
        m, plan = Memory(), boss_plan(5, 5)
        c = ctx(w, m, plan, on_hostile="fight")
        sync_boss(w, m, plan)
        self.assertTrue(m.boss_clock.assumed)
        self.assertEqual(boss_clock.ticks_left(m, w, None, 10), boss_clock.MIN_LIMIT_SECONDS * 10)
        kb = KnowledgeBase.empty("olympuff")
        w.map_levels[9] = 2
        kb.levels["2"] = {boss_clock.LIMIT: 60}
        self.assertEqual(boss_clock.ticks_left(m, w, kb, 10), 600, "a lower kept limit holds")
        self.assertTrue(boss_fight_on(w, c))
        out = dispatch(w, c)
        self.assertIn("retreat out", out.reason, "out by the room's door it has seen")
        w.tick += boss_clock.MIN_LIMIT_SECONDS * 10
        self.assertFalse(boss_fight_on(w, c), "no time left: Retreat and Flee no longer stand down")

    def test_the_walk_out_is_priced_again_when_its_inputs_change(self):
        """Review on #262: the boss on the exit step, then off it, then a longer way."""
        w = grid(["#######", "#D....#", "#.....#", "#.....#", "#######"], at=(3, 1), map_id=9)
        m = in_room(w, Memory(), landing=(2, 1))
        c = ctx(w, m, boss_plan(5, 5))
        column = [boss(pos=(2, 1)), Entity("npc", 51, (2, 2), "rat"), Entity("npc", 52, (2, 3), "rat")]
        w.entities = column
        self.assertIsNone(exit_steps(w, c, (1, 1)), "every way toward it taken")
        self.assertIsNone(m.boss_clock.route, "no open step is never kept")
        w.entities = [boss(pos=(4, 1))]
        self.assertEqual(exit_steps(w, c, (1, 1)), 2, "the same cell, the way opened")
        w.view.tiles[(2, 1)] = w.view.tiles[(2, 2)] = "wall"
        self.assertEqual(exit_steps(w, c, (1, 1)), 4, "the same cell, the way round is longer")

    def test_the_walk_out_is_priced_by_its_path_once_per_position(self):
        """Review on #262: round a wall the walk is longer than the straight line."""
        w = grid(["#######", "#D#...#", "#.#...#", "#.....#", "#######"], at=(3, 1), map_id=9)
        m = in_room(w, Memory(), landing=(1, 2))
        c = ctx(w, m, boss_plan(5, 5))
        with mock.patch("agentrealm_agent.states.boss.cost_path", wraps=cost_path) as priced:
            steps = exit_steps(w, c, (1, 1))
            self.assertEqual(exit_steps(w, c, (1, 1)), steps)
            self.assertEqual(priced.call_count, 1, "cached for the same position")
        self.assertEqual(steps, 4, "two cells away as the crow flies, four round the wall")
        step = 4  # ticks a step at the default speed and rate
        m.boss_clock.door_tick = w.tick - boss_clock.DEFAULT_LIMIT_SECONDS * c.tick_hz + 3 * step + LEAVE_MARGIN_SECONDS * c.tick_hz
        self.assertTrue(clock_nearly_out(w, c, steps), "the straight line would say there is time")
        self.assertFalse(clock_nearly_out(w, c, 2))

    def test_the_clock_stops_off_the_rooms_map(self):
        w, m, plan = self.stepped_through()
        w.apply_position({"map_id": 8, "x": 5, "y": 6})
        sync_boss(w, m, plan)
        self.assertIsNone(m.boss_clock)

    def test_a_return_restarts_the_clock_and_the_boss_keeps_its_damage(self):
        """Manual §11: a boss keeps the damage it has taken; a new step is a new clock."""
        w, m, plan = self.stepped_through()
        w.entities = [boss(health=12)]
        sync_boss(w, m, plan)
        w.apply_position({"map_id": 8, "x": 5, "y": 6})
        w.entities = []
        sync_boss(w, m, plan)
        self.assertIsNone(m.boss)
        w.tick = 2000
        w.apply_position({"map_id": 9, "x": 1, "y": 0})
        w.passage = Passage(8, (5, 5), 9, 2000, (1, 0))
        w.entities = [boss(health=12)]
        sync_boss(w, m, plan)
        self.assertEqual(m.boss_clock.door_tick, 2000)
        self.assertEqual(m.boss.since_tick, 2000)
        self.assertEqual(health_left(w.entities[0]), 12, "priced at its served health, damage kept")


class BossSwingPacingTest(unittest.TestCase):
    """A123: boss swings wait the armed weapon's cooldown at the world's tick rate."""

    def test_a_mallet_swings_every_1_3_seconds_at_any_tick_rate(self):
        w = grid(["......"], at=(1, 0))
        w.armed_code = "bronze_mallet"
        m = Memory()
        m.calm_poll_interval = 80
        for hz, ticks in ((10, 13), (20, 26)):
            with mock.patch("agentrealm_agent.states.boss.queue_horizon_intents", return_value=80):
                queue = boss_attack_queue(w, m, boss(pos=(2, 0)), hz)
            swings = [i for i, x in enumerate(queue) if x["verb"] == "Use"]
            self.assertEqual(swings[1] - swings[0], ticks, hz)


if __name__ == "__main__":
    unittest.main()
