"""Stuck detection, escalation, and the navigation fixtures (A15)."""

from __future__ import annotations

import json
import random
import unittest

from agentrealm_agent.executor.constants import DEFAULT_TICK_RATE_HZ
from agentrealm_agent.plan import PLAN_STALL_SECONDS
from unittest import mock

from agentrealm_agent.brain import Memory, decide
from agentrealm_agent.directives import PARAM_DEFAULTS
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.knowledge_maps import record_warp, sync_map_from_view
from agentrealm_agent.navigation import CostGridParams
from agentrealm_agent.navigation import stuck as nav_stuck
from agentrealm_agent.navigation.planner import FOG
from agentrealm_agent.pathing import escalation_step, guided_step, path_for_plan_op, replan
from agentrealm_agent.plan import Plan
from agentrealm_agent.states import PlayContext, dispatch
from agentrealm_agent.world import Entity, WorldModel
from tests.fixtures.navigation import grids, sim
from tests.test_cost_grid import grid
from tests.test_recover import apply_zone, ctx, died_at, world


# A policy ``goto`` is a plan ``travel`` point op, walked by Travel under this label.
WALK = "travel:point"


def open_world(width: int = 12, at=(0, 0)) -> WorldModel:
    """Seen ground ``width`` wide and 3 deep, fog beyond."""
    return grid(["." * width] * 3, at=at)


def builtin_plan(policy) -> Plan:
    return Plan.from_policy(policy, dict(PARAM_DEFAULTS))


def run_plan(sc, policy, plan: Plan, **kw) -> sim.Run:
    """``sim.run`` with ``plan`` in place of the built-in plan for ``policy``."""
    with mock.patch.object(sim.Plan, "from_policy", lambda *a, **k: plan):
        return sim.run(sc, policy, **kw)


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
        plan = builtin_plan(policy)
        for _ in range(3):
            d = decide(w, m, policy, random.Random(1), plan=plan)
            self.assertIsNone(plan.current(), "the travel op is done on arrival")
            self.assertFalse(d.reason.startswith(WALK), d.reason)
            w.tick += nav_stuck.PROGRESS_TICK_LIMIT
        self.assertNotIn(WALK, {a.goal for a in m.nav_stuck.attempts.values()})
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
        self.assertIn(att.level, (nav_stuck.ALT_ROUTE, nav_stuck.WALK))

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
        self.assertIsNone(
            escalation_step(m, w, att, set(), lambda _a: None, "moves"),
            "failing again gives up after alt route",
        )
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


class BreakResetTest(unittest.TestCase):
    """A break resets the ladder to WALK only if the walk then gets nearer (A16 Walk run 3)."""

    def _climb_to_break(self, m, w, att):
        while att.level != nav_stuck.BREAK:
            self.assertTrue(nav_stuck.escalate(m, w, att, "moves"))

    def test_break_then_no_progress_gives_up_within_a_bounded_number_of_attempts(self):
        w, m = open_world(at=(0, 1)), Memory()
        target = (150, 1)
        att = nav_stuck.track(m, w, WALK, target)
        m.goal = WALK
        given_up = False
        for failures in range(1, 20):
            if att.level == nav_stuck.BREAK:
                nav_stuck.on_break_opened(m, w, att)  # grass cut beside the route
                self.assertEqual(att.level, nav_stuck.WALK)
            m.goal = WALK
            w.pos = (failures % 3, 1)  # paces near the cut, never nearer the target
            nav_stuck.on_step(m, w)
            if not nav_stuck.escalate(m, w, att, "moves"):
                given_up = True
                break
        self.assertTrue(given_up, "the break loop never gave up")
        self.assertLessEqual(failures, 6, "WALK, CAUTIOUS, BREAK, WALK, REVEAL, ALT_ROUTE")
        self.assertIn((w.map_id, target), m.nav_stuck.given_up_travel)
        self.assertEqual(len(m.nav_stuck.stuck_signals), 1)

    def test_break_then_real_progress_restarts_the_ladder(self):
        w, m = open_world(width=40, at=(0, 1)), Memory()
        att = nav_stuck.track(m, w, WALK, (39, 1))
        m.goal = WALK
        nav_stuck.on_step(m, w)
        self._climb_to_break(m, w, att)
        nav_stuck.on_break_opened(m, w, att)
        m.goal = WALK  # the walk takes the route again
        w.pos = (nav_stuck.RESET_PROGRESS_CELLS, 1)
        nav_stuck.on_step(m, w)
        self.assertTrue(nav_stuck.escalate(m, w, att, "moves"))
        self.assertEqual(att.level, nav_stuck.CAUTIOUS, "the break earned its reset")


class BackoffTest(unittest.TestCase):
    def test_given_up_target_is_retried_after_a_doubling_backoff(self):
        sc = grids.WATER_ENCLOSURE
        r = sim.run(sc, sim.scripted(goals=["goto"], goto=sc.goal), max_decisions=500, stop_on_signal=False)
        sigs = r.memory.nav_stuck.stuck_signals
        self.assertGreaterEqual(len(sigs), 2, "the stuck trigger re-arms once the backoff runs out")
        first, second = sigs[0]["tick"], sigs[1]["tick"]
        self.assertGreaterEqual(second - first, nav_stuck.BACKOFF_BASE_TICKS)
        key = nav_stuck.goal_key(WALK, 1, sc.goal)
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
        replan(w, m, policy, set(), set())  # the safe default's walk
        first = m.path[-1]
        att = nav_stuck.active(m, w)
        self.assertEqual((att.goal, att.target), ("explore", first))
        att.level, att.moves = nav_stuck.REVEALED, nav_stuck.PROGRESS_MOVE_LIMIT
        out = dispatch(w, sim_ctx(m, policy))  # no plan: the safe default
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
    dispatcher (a policy ``goto`` is the built-in plan's ``travel`` point op,
    walked by Travel as ``travel:point``).

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

    def test_goto_travel_point(self):
        for sc, outcome, reason, budget in self.CASES:
            with self.subTest(sc.name):
                r = sim.run(sc, sim.scripted(goals=["goto"], goto=sc.goal), max_decisions=800)
                self.check(sc, r, outcome, reason, budget)
                if outcome == "abandoned":
                    self.assertEqual(r.signal["goal"], WALK)
                    key = nav_stuck.goal_key(WALK, 1, sc.goal)
                    self.assertTrue(nav_stuck.is_backed_off(r.memory.nav_stuck, key, r.world.tick))

    def test_hedge_line_reached_with_a_sword(self):
        sc = grids.HEDGE_LINE
        m = Memory()
        r = sim.run(
            sc,
            sim.scripted(goals=["goto"], goto=sc.goal),
            memory=m,
            armed_code="bronze_sword",
            max_decisions=800,
        )
        self.assertEqual(r.outcome, "reached", sc.name)
        self.assertLessEqual(r.moves, 80, sc.name)

    def test_npc_corridor_drops_the_op_after_the_stall_bound(self):
        # An NPC standing in the corridor for good: Travel holds instead of
        # pacing, but the hold is not progress, so the op is dropped (and
        # reported) once it has stalled PLAN_STALL_SECONDS (A34, A61).
        sc = grids.NPC_CORRIDOR
        r = sim.run(sc, sim.scripted(goals=["goto"], goto=sc.goal), max_decisions=120, stop_on_signal=False)
        failed = [s for s in r.memory.strategist_signals if s["trigger"] == "goal_failed"]
        self.assertEqual(len(failed), 1, r.memory.strategist_signals)
        self.assertIn("no progress", failed[0]["reason"])
        held = [row for row in r.trace if row["reason"].startswith(f"{WALK}: way taken")]
        self.assertTrue(held, "Travel held the round while the way was taken")
        self.assertLessEqual(held[-1]["tick"] - held[0]["tick"], PLAN_STALL_SECONDS * DEFAULT_TICK_RATE_HZ)
        during = [row for row in r.trace if held[0]["tick"] <= row["tick"] <= held[-1]["tick"]]
        self.assertTrue(all(row["intent"] is None for row in during), "no pacing while the way was taken")


class TravelBackoffTest(unittest.TestCase):
    def test_travel_yields_while_backed_off_and_retries_after(self):
        sc = grids.HEDGE_LINE
        policy = sim.scripted(goals=["goto"], goto=sc.goal)
        r = sim.run(sc, policy)
        self.assertEqual(r.outcome, "abandoned")
        w, m = r.world, r.memory
        c = PlayContext(m, policy, random.Random(1), never_attack=[], plan=builtin_plan(policy))
        out = dispatch(w, c)
        self.assertEqual(out.yielded, [f"Travel: {WALK} blocked"], "backed off: Travel yields")
        self.assertEqual(out.state, "Explore", "the safe default moves instead")
        self.assertEqual(len(m.nav_stuck.stuck_signals), 1)
        w.tick = m.nav_stuck.backoff_until[nav_stuck.goal_key(WALK, 1, sc.goal)]
        dispatch(w, c)
        self.assertEqual(len(m.nav_stuck.stuck_signals), 2, "retried after the backoff, and stuck again")


class RecoverStuckTest(unittest.TestCase):
    def test_walled_off_chest_is_given_up_and_backed_off(self):
        w = world(["######", "#.#..#", "######"], at=(3, 1))
        died_at(w, 1, 1)
        apply_zone(w, 7, 1, 1, {"safe": True, "brightness": 1})
        c = ctx(sim.scripted(goals=[], pickup=True))
        out = dispatch(w, c)
        self.assertEqual(out.yielded, ["Recover: chest not reachable"], "Recover yields")
        self.assertEqual(out.state, "Explore", "the safe default moves instead")
        sig = c.memory.nav_stuck.stuck_signals[0]
        self.assertEqual((sig["goal"], sig["reason"]), ("chest", "no_path"))
        dispatch(w, c)
        self.assertEqual(len(c.memory.nav_stuck.stuck_signals), 1, "backed off: not retried each round")


class LevelStuckTest(unittest.TestCase):
    def test_walled_door_escalates_then_backs_off(self):
        sc = grids.LEVEL_WALLED_DOOR
        policy = sim.scripted(goals=[])
        plan = Plan([{"op": "enter_level", "x": sc.goal[0], "y": sc.goal[1]}], dict(PARAM_DEFAULTS))
        r = run_plan(sc, policy, plan)
        self.assertEqual(r.outcome, "abandoned")
        self.assertTrue(all(row["reason"].startswith("level →") for row in r.trace[:-1]), r.trace)
        sig = r.signal
        self.assertEqual((sig["goal"], sig["target"]), ("level:door", list(sc.goal)))
        self.assertIn("no_frontier", sig["escalation"])
        self.assertEqual(sig["escalation"][-1], "no_path", "alt route after reveal")
        w, m = r.world, r.memory
        key = nav_stuck.goal_key("level:door", 1, sc.goal)
        self.assertEqual(key, nav_stuck.goal_key("doors", 1, sc.goal), "the doors goal skips it too")
        self.assertIsNone(plan.current(), "no level step left: the op is done")
        d = decide(w, m, policy, random.Random(1), plan=Plan([dict(plan.goals[0])], dict(PARAM_DEFAULTS)))
        self.assertNotIn(f"level → {sc.goal}", d.reason, "backed off: Level does not walk to it")
        self.assertTrue(nav_stuck.is_backed_off(m.nav_stuck, key, w.tick))
        self.assertEqual(len(m.nav_stuck.stuck_signals), 1)


def _cross_map_run(
    sc: grids.Scenario, door: tuple[int, int], landing: tuple[int, int] = (0, 0)
) -> sim.CrossMapRun:
    kb = KnowledgeBase.empty("sandbox")
    w1 = WorldModel(character_id=1, map_id=1, pos=sc.start, perception=sc.perception)
    for y, row in enumerate(sc.rows):
        for x, g in enumerate(row):
            w1.view.tiles[(x, y)] = grids.GLYPHS[g]
    sync_map_from_view(kb, 1, w1.view)
    goal = (0, 0)
    record_warp(kb, 1, door, "framed_door", 2, landing)
    sync_map_from_view(kb, 2, sim.map2_view())
    return sim.CrossMapRun(door, landing, goal, kb)


class CrossMapStuckTest(unittest.TestCase):
    def test_goto_map_abandons_door_leg_with_stuck_detection(self):
        sc = grids.CROSS_MAP_HEDGE
        cross = _cross_map_run(sc, (6, 3))
        policy = sim.scripted(goals=["goto"], goto=(0, 0), goto_map=2)
        r = sim.run(sc, policy, cross=cross)
        self.assertEqual(r.outcome, "abandoned")
        self.assertEqual(r.signal["reason"], "no_path")
        key = nav_stuck.goal_key(WALK, 2, (0, 0))
        self.assertTrue(nav_stuck.is_backed_off(r.memory.nav_stuck, key, r.world.tick))
        self.assertEqual(r.signal["goal"], WALK)

    def test_goto_map_reaches_goal_through_door(self):
        sc = grids.CROSS_MAP_OPEN
        cross = _cross_map_run(sc, (4, 0), landing=(5, 0))
        policy = sim.scripted(goals=["goto"], goto=(0, 0), goto_map=2)
        r = sim.run(sc, policy, cross=cross, max_decisions=80)
        self.assertEqual(r.outcome, "reached")
        self.assertEqual(r.world.map_id, 2)
        walked = [row for row in r.trace if row["map_id"] == 2 and row.get("applied")]
        self.assertEqual(len(walked), 5, "walks from the landing to the goal on map 2")
        self.assertTrue(all(row["reason"].startswith(f"{WALK} → ") for row in walked))
        self.assertEqual(r.memory.nav_stuck.stuck_signals, [])

    def test_goto_leg_tracks_the_door_and_backs_off_the_destination(self):
        sc = grids.CROSS_MAP_OPEN
        cross = _cross_map_run(sc, (4, 0), landing=(5, 0))
        w = sim.world_for(sc, cross=cross)
        m = Memory()
        policy = sim.scripted(goals=["goto"], goto=(0, 0), goto_map=2)
        d = decide(w, m, policy, random.Random(0), knowledge=cross.kb, plan=builtin_plan(policy))
        self.assertEqual(d.reason, f"{WALK} → (4, 0)")
        att = nav_stuck.active(m, w)
        self.assertEqual((att.goal, att.target), (WALK, (4, 0)), "the door on this map")
        self.assertEqual(att.backoff_key, nav_stuck.goal_key(WALK, 2, (0, 0)))

    def test_plan_travel_town_across_maps_tracks_the_door(self):
        sc = grids.CROSS_MAP_OPEN
        cross = _cross_map_run(sc, (4, 0), landing=(5, 0))
        w = sim.world_for(sc, cross=cross)
        w.respawn_anchors.append((2, (0, 0)))
        m = Memory()
        plan = Plan([{"op": "travel", "to": "town", "x": 0, "y": 0}], dict(PARAM_DEFAULTS))
        out = dispatch(w, PlayContext(m, sim.scripted(), random.Random(0), knowledge=cross.kb, plan=plan))
        self.assertEqual(out.reason, "travel:town → (4, 0)")
        att = nav_stuck.active(m, w)
        self.assertEqual(att.target, (4, 0), "the door on this map")
        self.assertEqual(att.backoff_key, nav_stuck.goal_key("travel:town", 2, (0, 0)))

    def test_leg_toward_keeps_a_lost_route_only_for_its_own_destination(self):
        sc = grids.CROSS_MAP_OPEN
        w = sim.world_for(sc)
        m = Memory()
        ultimate = nav_stuck.goal_key("goto", 2, (0, 0))
        self.assertIsNone(nav_stuck.leg_toward(m, w, "goto", 2, (0, 0), None), "no route ever known")
        nav_stuck.track(m, w, "goto", nav_stuck.Leg((4, 0), ultimate))
        self.assertEqual(nav_stuck.leg_toward(m, w, "goto", 2, (0, 0), None), nav_stuck.Leg((4, 0), ultimate))
        self.assertIsNone(nav_stuck.leg_toward(m, w, "goto", 2, (1, 0), None), "another destination")
        self.assertIsNone(nav_stuck.leg_toward(m, w, "travel:town", 2, (0, 0), None), "another goal")
        self.assertEqual(nav_stuck.leg_toward(m, w, "goto", 1, (3, 2), None), nav_stuck.Leg((3, 2)))

    def test_travel_with_no_known_route_yields_without_backoff(self):
        sc = grids.CROSS_MAP_OPEN
        cross = _cross_map_run(sc, (4, 0), landing=(5, 0))
        policy = sim.scripted(goals=["goto"], goto=(2, 1), goto_map=3)
        plan = builtin_plan(policy)
        w = sim.world_for(sc, cross=cross)
        out = dispatch(w, PlayContext(Memory(), policy, random.Random(0), knowledge=cross.kb, plan=plan))
        self.assertEqual(out.yielded, [f"Travel: {WALK} blocked"])
        r = run_plan(sc, policy, plan, cross=cross, max_decisions=100)
        self.assertEqual(r.outcome, "budget")
        self.assertFalse(any(row["reason"].startswith(WALK) for row in r.trace), "Travel never stepped")
        self.assertEqual(r.memory.nav_stuck.stuck_signals, [])
        self.assertEqual(r.memory.nav_stuck.backoff_until, {})
        self.assertNotIn(WALK, {a.goal for a in r.memory.nav_stuck.attempts.values()})
        self.assertIsNone(plan.current(), "the stalled op is dropped (A34)")


class CrossMapTraceReplayTest(unittest.TestCase):
    def test_trace_replays_cross_map_goto_stuck(self):
        sc = grids.CROSS_MAP_HEDGE
        cross = _cross_map_run(sc, (6, 3))
        policy = sim.scripted(goals=["goto"], goto=(0, 0), goto_map=2)
        recorded = sim.run(sc, policy, cross=cross)
        self.assertEqual(recorded.outcome, "abandoned")
        trace = json.loads(json.dumps(recorded.trace))

        w = sim.world_for(sc, cross=cross)
        m = Memory()
        rng = random.Random(7)
        map2 = sim.map2_view()
        plan = builtin_plan(policy)
        for row in trace:
            w.tick = row["tick"]
            w.map_id = row.get("map_id", 1)
            self.assertEqual(list(w.pos), row["pos"], row)
            d = decide(w, m, policy, rng, knowledge=cross.kb, plan=plan)
            self.assertEqual((d.intent, d.reason), (row["intent"], row["reason"]), row)
            if d.intent is not None and d.intent.get("verb") == "SetPosition":
                self.assertEqual(
                    sim.apply(w, m, sc, (d.intent["x"], d.intent["y"]), {}, cross=cross, map2=map2),
                    row["applied"],
                    row,
                )
            elif d.intent is not None and d.intent.get("verb") == "Use":
                self.assertEqual(
                    sim.apply_use(w, m, sc, d.intent, {}, knowledge=cross.kb, map2=map2),
                    row.get("applied", True),
                    row,
                )
        self.assertEqual(m.nav_stuck.stuck_signals, recorded.memory.nav_stuck.stuck_signals)


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
        plan = builtin_plan(policy)
        for row in trace:
            w.tick = row["tick"]
            self.assertEqual(list(w.pos), row["pos"], row)
            d = decide(w, m, policy, rng, plan=plan)
            self.assertEqual((d.intent, d.reason), (row["intent"], row["reason"]), row)
            if d.intent is not None:
                self.assertEqual(sim.apply(w, m, sc, (d.intent["x"], d.intent["y"]), {}), row["applied"], row)
        self.assertEqual(m.nav_stuck.stuck_signals, recorded.memory.nav_stuck.stuck_signals)
        self.assertEqual(m.nav_stuck.backoff_until, recorded.memory.nav_stuck.backoff_until)


if __name__ == "__main__":
    unittest.main()
