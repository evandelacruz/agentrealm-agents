"""A38: Boss state, plan preconditions, fight clock, defeat from boss health."""

import random
import unittest

from agentrealm_agent.config import Policy
from agentrealm_agent.directives import PARAM_DEFAULTS
from agentrealm_agent.item_table import InventorySupply
from agentrealm_agent.memory import BossFight, Memory
from agentrealm_agent.plan import Plan, goal_done, validate_goal_op
from agentrealm_agent.states import PlayContext, dispatch
from agentrealm_agent.states.boss import (
    CLOCK_COMMIT_TICKS,
    fight_boss_preconditions_met,
    potion_count,
    sync_boss,
)
from agentrealm_agent.states.fight import fight_target
from agentrealm_agent.states.flee import should_flee
from agentrealm_agent.world import Entity, WorldModel, ZoneFact


def grid(rows: list[str], at=(0, 0), map_id=8) -> WorldModel:
    glyph = {".": "dirt", "#": "wall", "D": "framed_door"}
    w = WorldModel(character_id=1, map_id=map_id, pos=at, perception=5)
    for y, row in enumerate(rows):
        for x, g in enumerate(row):
            w.view.tiles[(x, y)] = glyph.get(g, g)
    w.apply_position({"map_id": map_id, "x": at[0], "y": at[1], "level": 1})
    w.health, w.max_health = 10, 10
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
        out = dispatch(w, ctx(w, Memory(), boss_plan(3, 0, min_health=10)))
        self.assertNotEqual(out.state, "Boss")

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
        self.assertIsNone(m.boss)  # no clock running: disengaged, not finished

    def test_boss_out_of_view_with_clock_stays_engaged(self):
        w = grid(["..."], at=(1, 0), map_id=9)
        w.boss_fight_end_tick = w.tick + 500
        w.entities = [boss()]
        plan = boss_plan()
        m = Memory()
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
        return w, Memory(), boss_plan(0, 0)

    def test_retreats_out_when_path_and_clock_allow(self):
        w, m, plan = self.hurt_in_fight()
        out = dispatch(w, ctx(w, m, plan, on_hostile="fight"))
        self.assertEqual(out.state, "Boss")
        self.assertIn("retreat out", out.reason)
        self.assertEqual((out.intents[0]["x"], out.intents[0]["y"]), (1, 0))

    def test_commits_when_clock_nearly_gone(self):
        w, m, plan = self.hurt_in_fight()
        w.boss_fight_end_tick = w.tick + CLOCK_COMMIT_TICKS
        out = dispatch(w, ctx(w, m, plan, on_hostile="fight"))
        self.assertEqual(out.state, "Boss")
        self.assertTrue(out.paced)
        self.assertEqual(out.intents[0]["verb"], "Use")

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
    def test_in_boss_fight_from_served_clock(self):
        w = WorldModel(character_id=1, tick=100)
        w.apply_observation({"version": "1", "complete": True, "snapshot": {"boss_fight_end_tick": 250}})
        self.assertTrue(w.in_boss_fight())
        self.assertEqual(w.boss_fight_ticks_left(), 150)
        w.tick = 260
        self.assertFalse(w.in_boss_fight())

    def test_delta_keeps_clock_unless_named(self):
        w = WorldModel(character_id=1, tick=100)
        w.apply_observation({"version": "1", "complete": True, "snapshot": {"boss_fight_end_tick": 250}})
        w.apply_observation({"version": "2", "delta": {"lives": 3}})
        self.assertEqual(w.boss_fight_end_tick, 250)
        w.apply_observation({"version": "3", "delta": {"boss_fight_end_tick": None}})
        self.assertIsNone(w.boss_fight_end_tick)


if __name__ == "__main__":
    unittest.main()
