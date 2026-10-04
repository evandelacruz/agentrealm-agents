"""A38: Boss state, plan preconditions, fight clock, boss health progress."""

import random
import unittest

from agentrealm_agent.config import Policy
from agentrealm_agent.directives import PARAM_DEFAULTS
from agentrealm_agent.item_table import InventorySupply
from agentrealm_agent.memory import Memory
from agentrealm_agent.plan import Plan, goal_done, validate_goal_op
from agentrealm_agent.states import PlayContext, dispatch
from agentrealm_agent.states.boss import (
    fight_boss_preconditions_met,
    potion_count,
)
from agentrealm_agent.world import Entity, WorldModel


def grid(rows: list[str], at=(0, 0), map_id=8) -> WorldModel:
    glyph = {".": "dirt", "#": "wall", "D": "framed_door"}
    w = WorldModel(character_id=1, map_id=map_id, pos=at, perception=5)
    for y, row in enumerate(rows):
        for x, g in enumerate(row):
            w.view.tiles[(x, y)] = glyph.get(g, g)
    return w


def ctx(w: WorldModel, m: Memory, plan: Plan | None = None) -> PlayContext:
    return PlayContext(
        m,
        Policy(kind="scripted", goals=["explore"]),
        random.Random(0),
        params=dict(PARAM_DEFAULTS),
        plan=plan,
    )


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


class PreconditionsTest(unittest.TestCase):
    def test_health_and_gear(self):
        w = grid(["."])
        w.health, w.max_health = 7, 10
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
        w = grid(["..D"], at=(0, 0), map_id=8)
        w.apply_position({"map_id": 8, "x": 0, "y": 0, "level": 1})
        w.health, w.max_health = 10, 10
        plan = Plan([{"op": "fight_boss", "x": 3, "y": 0}], dict(PARAM_DEFAULTS))
        m = Memory()
        out = dispatch(w, ctx(w, m, plan))
        self.assertEqual(out.state, "Boss")
        self.assertEqual((out.intents[0]["x"], out.intents[0]["y"]), (1, 0))

    def test_waits_when_preconditions_fail(self):
        w = grid(["..D"], at=(0, 0), map_id=8)
        w.apply_position({"map_id": 8, "x": 0, "y": 0, "level": 1})
        w.health = 3
        plan = Plan([{"op": "fight_boss", "x": 3, "y": 0, "min_health": 10}], dict(PARAM_DEFAULTS))
        out = dispatch(w, ctx(w, Memory(), plan))
        self.assertNotEqual(out.state, "Boss")

    def test_fights_boss_without_retreat_tail(self):
        w = grid(["..."], at=(1, 0), map_id=9)
        w.apply_position({"map_id": 9, "x": 1, "y": 0, "level": 2})
        w.health, w.max_health = 10, 10
        w.boss_fight_end_tick = w.tick + 500
        w.entities = [Entity("npc", 50, (2, 0), "cellar_boss", health=20, max_health=30)]
        plan = Plan([{"op": "fight_boss", "x": 0, "y": 0}], dict(PARAM_DEFAULTS))
        m = Memory(boss_engaged=True, boss_door=(9, (0, 0)))
        out = dispatch(w, ctx(w, m, plan))
        self.assertEqual(out.state, "Boss")
        self.assertTrue(out.paced)
        self.assertTrue(all(i.get("verb") == "Use" for i in out.intents[:3]))
        self.assertNotIn("Step", {i.get("verb") for i in out.intents[:3]})

    def test_goal_done_when_boss_defeated(self):
        w = grid(["."])
        m = Memory(boss_engaged=True)
        plan = Plan([{"op": "fight_boss", "x": 0, "y": 0}], dict(PARAM_DEFAULTS))
        self.assertFalse(goal_done(plan.current(), w, plan, memory=m))
        w.entities = [Entity("npc", 1, (0, 0), "boss", health=0, max_health=10)]
        self.assertTrue(goal_done(plan.current(), w, plan, memory=m))


class BossClockTest(unittest.TestCase):
    def test_in_boss_fight_from_served_clock(self):
        w = WorldModel(character_id=1, tick=100)
        w.apply_observation(
            {"version": "1", "complete": True, "snapshot": {"boss_fight_end_tick": 250}}
        )
        self.assertTrue(w.in_boss_fight())
        self.assertEqual(w.boss_fight_ticks_left(), 150)
        w.tick = 260
        self.assertFalse(w.in_boss_fight())


if __name__ == "__main__":
    unittest.main()
