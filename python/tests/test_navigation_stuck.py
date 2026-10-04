"""Stuck detection, escalation, and the navigation fixtures (A15)."""

from __future__ import annotations

import json
import random
import unittest

from agentrealm_agent.brain import Memory, decide
from agentrealm_agent.navigation import CostGridParams
from agentrealm_agent.navigation import stuck as nav_stuck
from agentrealm_agent.navigation.planner import FOG
from agentrealm_agent.pathing import escalation_step, guided_step, path_for_plan_op, replan
from agentrealm_agent.states import PlayContext, dispatch
from agentrealm_agent.travel.ops import TravelOp
from agentrealm_agent.world import Entity, WorldModel
from tests.fixtures.navigation import grids, sim
from tests.test_cost_grid import grid
from tests.test_recover import apply_zone, ctx, died_at, world


def open_world(width: int = 12, at=(0, 0)) -> WorldModel:
    """Seen ground ``width`` wide and 3 deep, fog beyond."""
    return grid(["." * width] * 3, at=at)


class StuckWindowTest(unittest.TestCase):
    def test_cautious_planning_raises_fog(self):
        w, m = open_world(), Memory()
        att = nav_stuck.track(m, w, "goto", (5, 0))
        base = CostGridParams()
        self.assertEqual(nav_stuck.planning_params(m, base).fog_cost, FOG)
        att.level = nav_stuck.CAUTIOUS
        self.assertEqual(nav_stuck.planning_params(m, base).fog_cost, nav_stuck.FOG_CAUTIOUS)
        self.assertEqual(base.fog_cost, FOG)

    def test_each_level_waits_for_its_own_window_to_fail(self):
        w, m = open_world(), Memory()
        att = nav_stuck.track(m, w, "goto", (11, 0))
        m.goal = "goto"
        att.moves = nav_stuck.PROGRESS_MOVE_LIMIT
        self.assertEqual(nav_stuck.stuck_reason(att, w.tick), "moves")
        self.assertTrue(nav_stuck.escalate(m, w, att, "moves"))
        self.assertEqual(att.level, nav_stuck.CAUTIOUS)
        self.assertIsNone(nav_stuck.stuck_reason(att, w.tick), "a fresh window: not stuck again at once")
        m.goal = "goto"
        for x in range(1, nav_stuck.PROGRESS_MOVE_LIMIT):
            w.pos = (x % 12, x // 12)
            nav_stuck.on_step(m, w)
        self.assertIsNone(nav_stuck.stuck_reason(att, w.tick))
        w.pos = (0, 2)
        nav_stuck.on_step(m, w)
        self.assertEqual(nav_stuck.stuck_reason(att, w.tick), "moves")

    def test_time_without_progress_is_stuck(self):
        w, m = open_world(), Memory()
        att = nav_stuck.track(m, w, "goto", (11, 0))
        nav_stuck.observe(att, w, [(1, 0), (2, 0)])
        w.tick += nav_stuck.PROGRESS_TICK_LIMIT - 1
        self.assertIsNone(nav_stuck.stuck_reason(att, w.tick))
        w.tick += 1
        self.assertEqual(nav_stuck.stuck_reason(att, w.tick), "time")

    def test_shorter_remaining_path_is_progress(self):
        w, m = open_world(), Memory()
        att = nav_stuck.track(m, w, "goto", (5, 0))
        nav_stuck.observe(att, w, [(1, 0), (2, 0), (3, 0), (4, 0), (5, 0)])
        att.moves = 10
        nav_stuck.observe(att, w, [(2, 0), (3, 0), (4, 0), (5, 0), (6, 1), (5, 0)])
        self.assertEqual(att.moves, 10, "a longer plan is not progress")
        nav_stuck.observe(att, w, [(3, 0), (4, 0), (5, 0)])
        self.assertEqual(att.moves, 0)

    def test_rejections_count_only_in_a_row(self):
        w, m = open_world(), Memory()
        att = nav_stuck.track(m, w, "goto", (5, 0))
        m.goal = "goto"
        nav_stuck.on_rejection(m)
        nav_stuck.on_rejection(m)
        nav_stuck.on_step(m, w)
        nav_stuck.on_rejection(m)
        nav_stuck.on_rejection(m)
        self.assertIsNone(nav_stuck.stuck_reason(att, w.tick), "an applied move broke the run")
        nav_stuck.on_rejection(m)
        self.assertEqual(nav_stuck.stuck_reason(att, w.tick), "rejections")

    def test_rejections_of_another_goals_steps_do_not_count(self):
        w, m = open_world(), Memory()
        att = nav_stuck.track(m, w, "goto", (5, 0))
        m.goal = "safe"
        for _ in range(nav_stuck.REJECT_STREAK_LIMIT):
            nav_stuck.on_rejection(m)
            nav_stuck.on_step(m, w)
        self.assertEqual((att.reject_streak, att.moves), (0, 0))

    def test_switching_targets_keeps_each_attempts_level(self):
        w, m = open_world(), Memory()
        a = nav_stuck.track(m, w, "goto", (5, 0))
        a.level = nav_stuck.CAUTIOUS
        nav_stuck.track(m, w, "explore", (11, 2))
        self.assertIs(nav_stuck.track(m, w, "goto", (5, 0)), a)
        self.assertEqual(a.level, nav_stuck.CAUTIOUS)


    def test_standing_on_the_goto_target_is_not_stuck(self):
        w, m = open_world(at=(3, 1)), Memory()
        policy = sim.scripted(goals=["goto"], goto=(3, 1))
        for _ in range(3):
            d = decide(w, m, policy, random.Random(1))
            self.assertIsNone(d.intent)
            w.tick += nav_stuck.PROGRESS_TICK_LIMIT
        self.assertEqual(m.nav_stuck.stuck_signals, [])


class RevealTest(unittest.TestCase):
    def test_reveal_runs_its_whole_budget_then_gives_up(self):
        # A long open strip: there is always a frontier to walk toward, and the
        # plan never gets shorter, so only the budget ends the reveal.
        w = grid(["." * 60], at=(0, 0))
        m = Memory()
        att = nav_stuck.track(m, w, "goto", (200, 0))
        att.level, att.best_ever = nav_stuck.CAUTIOUS, 5
        m.goal = "goto"
        stale = lambda _att: [(1, 0)] * 10  # measure never below best_ever
        step = escalation_step(m, w, att, set(), stale, "moves")
        self.assertEqual(att.level, nav_stuck.REVEAL)
        moves = 0
        while step is not None:
            w.pos = step
            nav_stuck.on_step(m, w)
            moves += 1
            step = escalation_step(m, w, att, set(), stale, None)
        self.assertEqual(moves, nav_stuck.REVEAL_MOVE_BUDGET)
        sig = m.nav_stuck.stuck_signals[-1]
        self.assertEqual((sig["reason"], sig["escalation"]), ("moves", ["moves", "reveal_spent"]))

    def test_reveal_that_finds_a_shorter_plan_walks_it(self):
        w = grid(["." * 12] * 3, at=(0, 1))
        m = Memory()
        att = nav_stuck.track(m, w, "goto", (11, 1))
        att.level, att.best_ever, att.reveal_left = nav_stuck.REVEAL, 30, 10
        m.goal = "goto"
        path = [(x, 1) for x in range(1, 12)]
        step = escalation_step(m, w, att, set(), lambda _a: path, None)
        self.assertEqual(step, (1, 1))
        self.assertEqual(att.level, nav_stuck.REVEALED)
        self.assertEqual(m.path, path)
        att.moves = nav_stuck.PROGRESS_MOVE_LIMIT
        self.assertIsNone(escalation_step(m, w, att, set(), lambda _a: path, "moves"), "failing again gives up")
        self.assertEqual(len(m.nav_stuck.stuck_signals), 1)

    def test_occupied_first_step_waits_out_the_window(self):
        w = grid(["#####", "....."], at=(0, 1))
        w.entities = [Entity("character", 5, (1, 1))]
        m = Memory()
        path = [(1, 1), (2, 1), (3, 1)]
        self.assertIsNone(guided_step(m, w, "goto", (3, 1), set(), lambda _a: path))
        att = nav_stuck.active(m, w)
        self.assertEqual(att.level, nav_stuck.WALK, "a taken cell is not a failed level")
        w.tick += nav_stuck.PROGRESS_TICK_LIMIT
        self.assertIsNone(guided_step(m, w, "goto", (3, 1), set(), lambda _a: path))
        self.assertEqual((att.level, att.reasons), (nav_stuck.CAUTIOUS, ["time"]))


class BackoffTest(unittest.TestCase):
    def test_given_up_target_is_retried_after_a_doubling_backoff(self):
        sc = grids.WATER_ENCLOSURE
        r = sim.run(sc, sim.scripted(goals=["goto"], goto=sc.goal), max_decisions=500, stop_on_signal=False)
        sigs = r.memory.nav_stuck.stuck_signals
        self.assertGreaterEqual(len(sigs), 2, "the stuck trigger re-arms once the backoff runs out")
        first, second = sigs[0]["tick"], sigs[1]["tick"]
        self.assertGreaterEqual(second - first, nav_stuck.BACKOFF_BASE_TICKS)
        key = nav_stuck.goal_key("goto", 1, sc.goal)
        n = len(sigs)
        self.assertEqual(r.memory.nav_stuck.backoff_power[key], n)
        self.assertEqual(
            r.memory.nav_stuck.backoff_until[key] - sigs[-1]["tick"], nav_stuck.BACKOFF_BASE_TICKS * 2 ** (n - 1)
        )

    def test_signal_queue_is_bounded(self):
        w, m = open_world(), Memory()
        for x in range(nav_stuck.SIGNALS_KEPT + 4):
            nav_stuck.give_up(m, w, nav_stuck.track(m, w, "goto", (x, 2)), "no_path")
        self.assertEqual(len(m.nav_stuck.stuck_signals), nav_stuck.SIGNALS_KEPT)
        self.assertEqual(m.nav_stuck.stuck_signals[-1]["target"], [nav_stuck.SIGNALS_KEPT + 3, 2])


class FrontierDropTest(unittest.TestCase):
    def test_given_up_frontier_is_dropped_by_explore_and_explore_area(self):
        # Seen ground with fog east and west: two frontier columns.
        w = grid(["?......?"] * 3, at=(2, 1))
        policy = sim.scripted(goals=["explore"])
        m = Memory()
        replan(w, m, policy, random.Random(1), set(), set())
        first = m.path[-1]
        att = nav_stuck.active(m, w)
        self.assertEqual((att.goal, att.target), ("explore", first))
        att.level, att.moves = nav_stuck.REVEALED, nav_stuck.PROGRESS_MOVE_LIMIT
        out = dispatch(w, sim_ctx(m, policy))
        self.assertIn(nav_stuck.goal_key("explore", 1, first), m.nav_stuck.backoff_until)
        self.assertEqual(m.nav_stuck.stuck_signals[0]["goal"], "explore")
        self.assertNotEqual(m.path[-1], first, "the dropped frontier is not picked again")
        self.assertEqual(out.intents[0]["verb"], "SetPosition")
        op = {"op": "explore_area", "x": first[0], "y": first[1], "radius": 0}
        self.assertIsNone(path_for_plan_op(op, w, m, policy, set(), set(), None), "plan exploration skips it too")


def sim_ctx(m: Memory, policy) -> PlayContext:
    return PlayContext(m, policy, random.Random(1), never_attack=[])


class NavigationFixtureTest(unittest.TestCase):
    """PLAYABLE_AGENT_PLAN Navigation **Tests**: each fixture is reached, or
    abandoned with the right reason, within a move budget, through the real
    dispatcher (Explore's ``goto`` and Travel's ``travel:point``).

    The hedge "with the tool" case needs Break (M9, A28); until then a hedge is
    a wall and only "without the tool" applies.
    """

    CASES = [
        # scenario, outcome, first stuck reason, move budget
        (grids.U_TRAP, "reached", None, 30),
        (grids.MAZE, "reached", None, 35),
        (grids.FOG_DEAD_END, "reached", None, 35),
        (grids.HEDGE_LINE, "abandoned", "no_path", 20),
        (grids.WATER_ENCLOSURE, "abandoned", "no_path", nav_stuck.REVEAL_MOVE_BUDGET),
        (grids.NPC_CORRIDOR, "abandoned", "time", 5),
        (grids.FOG_DEAD_END_CLOSED, "abandoned", "no_path", 12),
    ]

    def check(self, sc, r, outcome, reason, budget):
        self.assertEqual(r.outcome, outcome, sc.name)
        self.assertLessEqual(r.moves, budget, sc.name)
        if reason is None:
            self.assertEqual(r.memory.nav_stuck.stuck_signals, [], sc.name)
        else:
            self.assertEqual(r.signal["reason"], reason, sc.name)
            self.assertEqual(r.signal["target"], list(sc.goal), sc.name)

    def test_explore_goto(self):
        for sc, outcome, reason, budget in self.CASES:
            with self.subTest(sc.name):
                r = sim.run(sc, sim.scripted(goals=["goto"], goto=sc.goal))
                self.check(sc, r, outcome, reason, budget)
                if outcome == "abandoned":
                    key = nav_stuck.goal_key("goto", 1, sc.goal)
                    self.assertTrue(nav_stuck.is_backed_off(r.memory.nav_stuck, key, r.world.tick))

    def test_travel_point(self):
        for sc, outcome, reason, budget in self.CASES:
            with self.subTest(sc.name):
                m = Memory()
                m.travel_ops = [TravelOp("point", *sc.goal)]
                r = sim.run(sc, sim.scripted(goals=["hold"]), memory=m)
                self.check(sc, r, outcome, reason, budget)
                if outcome == "abandoned":
                    self.assertEqual(r.signal["goal"], "travel:point")

    def test_npc_corridor_waits_before_giving_up(self):
        sc = grids.NPC_CORRIDOR
        r = sim.run(sc, sim.scripted(goals=["goto"], goto=sc.goal))
        self.assertGreaterEqual(r.world.tick, 2 * nav_stuck.PROGRESS_TICK_LIMIT, "one window per level")
        self.assertEqual(r.signal["escalation"], ["time", "time", "no_frontier"])


class TravelBackoffTest(unittest.TestCase):
    def test_travel_yields_while_backed_off_and_retries_after(self):
        sc = grids.HEDGE_LINE
        m = Memory()
        m.travel_ops = [TravelOp("point", *sc.goal)]
        policy = sim.scripted(goals=["hold"])
        r = sim.run(sc, policy, memory=m)
        self.assertEqual(r.outcome, "abandoned")
        w = r.world
        d = decide(w, m, policy, random.Random(1))
        self.assertIsNone(d.intent, "backed off: Travel yields to Explore, which holds")
        self.assertIn("blocked", d.reason)
        w.tick = m.nav_stuck.backoff_until[nav_stuck.goal_key("travel:point", 1, sc.goal)]
        d = decide(w, m, policy, random.Random(1))
        self.assertEqual(len(m.nav_stuck.stuck_signals), 2, "retried after the backoff, and stuck again")


class RecoverStuckTest(unittest.TestCase):
    def test_walled_off_chest_is_given_up_and_backed_off(self):
        w = world(["######", "#.#..#", "######"], at=(3, 1))
        died_at(w, 1, 1)
        apply_zone(w, 7, 1, 1, {"safe": True, "brightness": 1})
        c = ctx(sim.scripted(goals=["hold"], pickup=True))
        out = dispatch(w, c)
        self.assertEqual(out.state, "Recover")
        self.assertIn("chest not reachable", out.reason)
        sig = c.memory.nav_stuck.stuck_signals[0]
        self.assertEqual((sig["goal"], sig["reason"]), ("chest", "no_path"))
        dispatch(w, c)
        self.assertEqual(len(c.memory.nav_stuck.stuck_signals), 1, "backed off: not retried each round")


class TraceReplayTest(unittest.TestCase):
    def test_trace_replays_through_stuck_reveal_and_backoff(self):
        sc = grids.WATER_ENCLOSURE
        policy = sim.scripted(goals=["goto"], goto=sc.goal)
        recorded = sim.run(sc, policy)
        reasons = [row["reason"] for row in recorded.trace]
        self.assertTrue(any("[reveal]" in r for r in reasons), "the trace covers reveal")
        self.assertEqual(recorded.outcome, "abandoned")
        trace = json.loads(json.dumps(recorded.trace))

        # Replay: the same decisions from the recorded trace, applied the same way.
        w = sim.world_for(sc)
        m = Memory()
        rng = random.Random(7)
        for row in trace:
            w.tick = row["tick"]
            self.assertEqual(list(w.pos), row["pos"], row)
            d = decide(w, m, policy, rng)
            self.assertEqual((d.intent, d.reason), (row["intent"], row["reason"]), row)
            if d.intent is not None:
                self.assertEqual(sim.apply(w, m, sc, (d.intent["x"], d.intent["y"])), row["applied"], row)
        self.assertEqual(m.nav_stuck.stuck_signals, recorded.memory.nav_stuck.stuck_signals)
        self.assertEqual(m.nav_stuck.backoff_until, recorded.memory.nav_stuck.backoff_until)


if __name__ == "__main__":
    unittest.main()
