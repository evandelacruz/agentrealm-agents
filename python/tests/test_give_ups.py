"""What failed where: stuck detection's one memory of walk targets given up (A15, A16, A90).

Every give-up is remembered by its walk's goal, map and cell, backed off for
``BACKOFF_BASE_TICKS`` doubled on each give-up there, and aged by time alone:
whatever the goal (a town, a shop, a point, a frontier, a block), it may be
walked to again once its backoff ends, and whether to is the planner's call.
"""

from __future__ import annotations

import json
import unittest

from agentrealm_adk.directives import PARAM_DEFAULTS, Directives
from agentrealm_adk.memory import Memory
from agentrealm_adk.navigation import stuck as nav_stuck
from agentrealm_agent.plan import Plan
from agentrealm_agent.runner import Runner
from agentrealm_agent.states import dispatch
from agentrealm_agent.strategist import build_prompt
from agentrealm_adk.travel.resolve import given_up_travel
from agentrealm_adk.world import WorldModel
from tests.test_strategist import FakeLLM, fake_runner, make, round_trip
from tests.test_survival_runs import MAP, ctx, world

TOWN = (9, 9)
TOWN_OP = {"op": "travel", "to": "town", "x": 0, "y": 0}


def give_up(m: Memory, w: WorldModel, goal: str, cell) -> nav_stuck.GiveUp:
    nav_stuck.give_up(m, w, nav_stuck.track(m, w, goal, cell), "pacing")
    return m.nav_stuck.given_up[nav_stuck.goal_key(goal, w.map_id, cell)]


class GiveUpMemoryTest(unittest.TestCase):
    def setUp(self):
        self.w = WorldModel(character_id=1, map_id=MAP, pos=(0, 0), tick=100)
        self.m = Memory()

    def test_every_give_up_lapses_when_its_backoff_ends(self):
        for goal in ("travel:town", "travel:point", "explore"):
            rec = give_up(self.m, self.w, goal, TOWN)
            self.assertEqual(rec.until, self.w.tick + nav_stuck.BACKOFF_BASE_TICKS, goal)
            self.assertTrue(nav_stuck.backed_off(self.m, goal, MAP, TOWN, rec.until - 1), goal)
            self.assertFalse(nav_stuck.backed_off(self.m, goal, MAP, TOWN, rec.until), goal)

    def test_a_travel_give_up_is_listed_with_when_it_lapses(self):
        rec = give_up(self.m, self.w, "travel:point", TOWN)
        self.assertEqual(given_up_travel(self.m.nav_stuck, self.w.tick), {(MAP, TOWN): rec.until})
        self.assertEqual(given_up_travel(self.m.nav_stuck, rec.until), {})
        self.assertEqual(self.m.nav_stuck.stuck_signals[-1]["retry_at_tick"], rec.until)

    def test_each_give_up_there_backs_off_twice_as_long(self):
        for n in range(3):
            rec = give_up(self.m, self.w, "travel:town", TOWN)
            self.assertEqual(rec.power, n + 1)
            self.assertEqual(rec.until - self.w.tick, nav_stuck.BACKOFF_BASE_TICKS * 2**n)
            self.w.tick = rec.until  # lapsed, but its power is kept

    def test_a_target_reached_is_forgotten(self):
        give_up(self.m, self.w, "travel:town", TOWN)
        nav_stuck.finish(self.m, nav_stuck.attempt(self.m, self.w, "travel:town", TOWN))
        self.assertEqual(self.m.nav_stuck.given_up, {})

    def test_a_give_up_is_its_maps_only(self):
        give_up(self.m, self.w, "explore", TOWN)
        self.assertTrue(nav_stuck.backed_off(self.m, "explore", MAP, TOWN, self.w.tick))
        self.assertFalse(nav_stuck.backed_off(self.m, "explore", MAP + 1, TOWN, self.w.tick))

    def test_a_lapse_is_reported_once(self):
        rec = give_up(self.m, self.w, "travel:town", TOWN)
        self.assertEqual(nav_stuck.lapsed(self.m.nav_stuck, rec.until - 1), [])
        self.assertEqual(nav_stuck.lapsed(self.m.nav_stuck, rec.until), [rec])
        self.assertEqual(nav_stuck.lapsed(self.m.nav_stuck, rec.until + 1), [])

    def test_lapsed_give_ups_are_forgotten_first_and_an_active_one_never(self):
        travel = give_up(self.m, self.w, "travel:point", TOWN)
        self.w.tick += 1
        for x in range(nav_stuck.GIVE_UPS_KEPT + 3):
            give_up(self.m, self.w, "explore", (x, 50))
        stuck = self.m.nav_stuck
        self.assertEqual(len(stuck.given_up), nav_stuck.GIVE_UPS_KEPT + 4, "all still backed off: none forgotten")
        self.assertIn((MAP, TOWN), given_up_travel(stuck, self.w.tick))
        self.w.tick = travel.until  # the travel lapses; the explores (one tick later) do not
        give_up(self.m, self.w, "explore", (99, 50))
        self.assertNotIn(nav_stuck.goal_key("travel:point", MAP, TOWN), stuck.given_up, "the lapsed one goes first")
        self.assertEqual(len(stuck.given_up), nav_stuck.GIVE_UPS_KEPT + 4)
        self.w.tick += nav_stuck.BACKOFF_BASE_TICKS
        give_up(self.m, self.w, "explore", (98, 50))
        self.assertEqual(len(stuck.given_up), nav_stuck.GIVE_UPS_KEPT, "back to the bound once they lapse")

    def test_travel_walks_there_again_once_its_give_up_lapses(self):
        w, c = world(), ctx()
        w.respawn_anchors = [(MAP, (20, 10))]
        c.plan = Plan([dict(TOWN_OP)], dict(PARAM_DEFAULTS))
        rec = give_up(c.memory, w, "travel:town", (20, 10))
        self.assertTrue(nav_stuck.backed_off(c.memory, "travel:town", MAP, (20, 10), w.tick))
        w.tick = rec.until
        out = dispatch(w, c)
        self.assertEqual(out.state, "Travel")
        self.assertEqual(out.intents[0]["verb"], "SetPosition", out.reason)

    def test_the_planner_sees_each_give_up_and_when_it_lapses(self):
        rec = give_up(self.m, self.w, "travel:town", TOWN)
        messages = build_prompt(
            triggers=[],
            w=self.w,
            plan=Plan([], dict(PARAM_DEFAULTS)),
            directives=Directives(params=dict(PARAM_DEFAULTS)),
            knowledge=None,
            given_up_travel=given_up_travel(self.m.nav_stuck, self.w.tick),
        )
        rows = [{"cell": f"{MAP}:9,9", "retry_at_tick": rec.until}]
        self.assertIn(f"given_up_travel={json.dumps(rows, sort_keys=True)}", messages[1]["content"])
        self.assertIn("Each give-up lapses at its retry_at_tick", messages[0]["content"])

    def test_a_resent_travel_is_filtered_until_its_give_up_lapses(self):
        s = make(FakeLLM({"goals": [TOWN_OP]}, {"goals": [TOWN_OP]}))
        r = fake_runner()
        r.world.respawn_anchors = [(7, TOWN)]
        rec = give_up(r.mem, r.world, "travel:town", TOWN)
        r.mem.nav_stuck.stuck_signals.clear()
        round_trip(s, r)
        self.assertIsNone(r.plan.current(), "given up: the re-sent travel:town never reaches the stack")
        r.world.tick = rec.until
        Runner.note_lapsed_give_ups(r)
        r.log.assert_called()
        s.clock.now += 60
        round_trip(s, r)
        self.assertEqual(r.plan.current()["to"], "town", "lapsed: the planner may send it again")


if __name__ == "__main__":
    unittest.main()
