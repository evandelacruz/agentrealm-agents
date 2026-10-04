"""Stuck detection, escalation, and navigation fixtures (A15)."""

from __future__ import annotations

import json
import random
import unittest
from unittest import mock

from agentrealm_agent.brain import Memory, decide
from agentrealm_agent.config import Policy
from agentrealm_agent.navigation import CostGridParams, learn_step_rejection
from agentrealm_agent.navigation.planner import FOG
from agentrealm_agent.navigation import stuck as nav_stuck
from agentrealm_agent.navigation.stuck import FOG_CAUTIOUS, NavAttempt
from agentrealm_agent.pathing import grid_params, plan_goal, replan
from agentrealm_agent.world import WorldModel
from tests.fixtures.navigation import grids
from tests.test_cost_grid import grid


def scripted(**kw) -> Policy:
    kw.setdefault("pickup", False)
    return Policy(kind="scripted", **kw)


class StuckUnitTest(unittest.TestCase):
    def test_planning_params_raise_fog_at_escalation_one(self):
        m = Memory()
        m.nav_stuck.attempt = NavAttempt(key="goto:1:5,0", target=(5, 0), map_id=1, escalation=1)
        base = CostGridParams()
        tuned = nav_stuck.planning_params(m, base)
        self.assertEqual(tuned.fog_cost, FOG_CAUTIOUS)
        self.assertEqual(base.fog_cost, FOG)

    def test_rejections_escalate_then_give_up(self):
        w = grid(["...", "..."], at=(0, 0))
        w.tick = 100
        m = Memory()
        nav_stuck.track_plan(m, w, "goto", (2, 0))
        for _ in range(nav_stuck.REJECT_STREAK_LIMIT):
            nav_stuck.on_rejection(m)
        level = nav_stuck.maybe_escalate(m, w, (2, 0), CostGridParams())
        self.assertEqual(level, 1)
        att = m.nav_stuck.attempt
        assert att is not None
        att.escalation = 1
        att.moves_without_progress = nav_stuck.PROGRESS_MOVE_LIMIT
        level = nav_stuck.maybe_escalate(m, w, (2, 0), CostGridParams())
        self.assertEqual(level, 3)
        att.escalation = 3
        att.reveal_left = 0
        att.moves_without_progress = nav_stuck.PROGRESS_MOVE_LIMIT
        level = nav_stuck.maybe_escalate(m, w, (2, 0), CostGridParams())
        self.assertEqual(level, 5)
        self.assertEqual(m.nav_stuck.attempt, None)
        self.assertEqual(len(m.nav_stuck.stuck_signals), 1)

    def test_frontier_backoff_filters_explore_targets(self):
        w = grid(["....", "...."], at=(0, 0))
        m = Memory()
        key = nav_stuck.goal_key("frontier", 1, (3, 0))
        m.nav_stuck.backoff_until[key] = 1000
        w.tick = 10
        targets = nav_stuck.filter_frontiers(m.nav_stuck, 1, w.view.frontier(), w.tick)
        self.assertNotIn((3, 0), targets)

    def test_learn_step_rejection_counts_for_stuck(self):
        w = grid(["...", "..."], at=(0, 0))
        m = Memory()
        nav_stuck.track_plan(m, w, "goto", (2, 0))
        learn_step_rejection(m, w, None, (1, 0), "not_traversable", 1)
        self.assertEqual(m.nav_stuck.attempt.reject_streak, 1)


class NavigationFixtureTest(unittest.TestCase):
    def test_maze_fixture_loads_goal_cell(self):
        w = grids.simple_maze()
        self.assertEqual(w.view.tiles.get((7, 5)), "dirt")

    def test_unreachable_goto_escalates_to_give_up(self):
        w = grids.hedge_line()
        w.tick = 10
        m = Memory()
        target = (7, 4)
        nav_stuck.track_plan(m, w, "goto", target)
        params = CostGridParams()
        with mock.patch.object(nav_stuck, "PROGRESS_MOVE_LIMIT", 2):
            att = m.nav_stuck.attempt
            assert att is not None
            att.moves_without_progress = 2
            self.assertEqual(nav_stuck.maybe_escalate(m, w, target, params), 1)
            att.moves_without_progress = 2
            self.assertEqual(nav_stuck.maybe_escalate(m, w, target, params), 3)
            att.reveal_left = 0
            att.moves_without_progress = 2
            self.assertEqual(nav_stuck.maybe_escalate(m, w, target, params), 5)
        self.assertIn(nav_stuck.goal_key("goto", 1, target), m.nav_stuck.backoff_until)
        self.assertEqual(m.nav_stuck.stuck_signals[0]["trigger"], "stuck")

    def test_fog_dead_end_fixture_has_walkable_corridor(self):
        w = grids.fog_dead_end()
        self.assertEqual(w.view.tiles[(5, 0)], "dirt")


class TraceReplayTest(unittest.TestCase):
    def test_trace_lines_replay_same_step_choices(self):
        w = grid([".....", ".....", "....."], at=(0, 1))
        policy = scripted(goals=["goto"], goto=(4, 1))
        rng = random.Random(7)
        trace: list[dict] = []
        m = Memory()
        for tick in range(6):
            w.tick = tick
            d = decide(w, m, policy, rng)
            trace.append(
                {
                    "tick": tick,
                    "pos": list(w.pos),
                    "intent": d.intent,
                    "reason": d.reason,
                }
            )
            if d.intent and d.intent.get("verb") == "Step":
                direction = d.intent["direction"]
                dx, dy = {"left": (-1, 0), "right": (1, 0), "up": (0, -1), "down": (0, 1)}[direction]
                w.pos = (w.pos[0] + dx, w.pos[1] + dy)

        w2 = grid([".....", ".....", "....."], at=(0, 1))
        m2 = Memory()
        for row in trace:
            w2.tick = row["tick"]
            d2 = decide(w2, m2, policy, rng)
            self.assertEqual(d2.intent, row["intent"], row)
            if d2.intent and d2.intent.get("verb") == "Step":
                direction = d2.intent["direction"]
                dx, dy = {"left": (-1, 0), "right": (1, 0), "up": (0, -1), "down": (0, 1)}[direction]
                w2.pos = (w2.pos[0] + dx, w2.pos[1] + dy)

        # JSON round-trip matches replay harness expectations.
        json.dumps(trace)


if __name__ == "__main__":
    unittest.main()
