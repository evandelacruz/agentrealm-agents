"""Stuck detection, escalation, and the navigation fixtures (A15)."""

from __future__ import annotations

import json
import random
import unittest

from agentrealm_agent.brain import Memory, decide
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.knowledge_maps import record_warp, sync_map_from_view
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
        (grids.NPC_CORRIDOR, "abandoned", "time", 800),
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
                r = sim.run(sc, sim.scripted(goals=["goto"], goto=sc.goal), max_decisions=800)
                self.check(sc, r, outcome, reason, budget)
                if outcome == "abandoned":
                    key = nav_stuck.goal_key("goto", 1, sc.goal)
                    self.assertTrue(nav_stuck.is_backed_off(r.memory.nav_stuck, key, r.world.tick))

    def test_travel_point(self):
        for sc, outcome, reason, budget in self.CASES:
            with self.subTest(sc.name):
                m = Memory()
                m.travel_ops = [TravelOp("point", *sc.goal)]
                r = sim.run(sc, sim.scripted(goals=["hold"]), memory=m, max_decisions=800)
                self.check(sc, r, outcome, reason, budget)
                if outcome == "abandoned":
                    self.assertEqual(r.signal["goal"], "travel:point")

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

    def test_npc_corridor_waits_before_giving_up(self):
        sc = grids.NPC_CORRIDOR
        r = sim.run(sc, sim.scripted(goals=["goto"], goto=sc.goal))
        self.assertGreaterEqual(r.world.tick, 2 * nav_stuck.PROGRESS_TICK_LIMIT, "one window per level")
        self.assertIn("no_frontier", r.signal["escalation"])
        self.assertEqual(r.signal["escalation"][-1], "no_path", "alt route after reveal")
        self.assertIn("time", r.signal["escalation"])


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
        self.assertIsNone(dispatch(w, c).intents, "Recover yields; hold sends nothing")
        sig = c.memory.nav_stuck.stuck_signals[0]
        self.assertEqual((sig["goal"], sig["reason"]), ("chest", "no_path"))
        dispatch(w, c)
        self.assertEqual(len(c.memory.nav_stuck.stuck_signals), 1, "backed off: not retried each round")


class LevelStuckTest(unittest.TestCase):
    def test_walled_door_escalates_then_backs_off(self):
        sc = grids.LEVEL_WALLED_DOOR
        policy = sim.scripted(goals=["hold"])
        r = sim.run(sc, policy)
        self.assertEqual(r.outcome, "abandoned")
        self.assertTrue(all(row["reason"].startswith("level →") for row in r.trace[:-1]), r.trace)
        sig = r.signal
        self.assertEqual((sig["goal"], sig["target"]), ("level:door", list(sc.goal)))
        self.assertIn("no_frontier", sig["escalation"])
        self.assertEqual(sig["escalation"][-1], "no_path", "alt route after reveal")
        w, m = r.world, r.memory
        key = nav_stuck.goal_key("level:door", 1, sc.goal)
        self.assertEqual(key, nav_stuck.goal_key("doors", 1, sc.goal), "the doors goal skips it too")
        d = decide(w, m, policy, random.Random(1))
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
        key = nav_stuck.goal_key("goto", 2, (0, 0))
        self.assertTrue(nav_stuck.is_backed_off(r.memory.nav_stuck, key, r.world.tick))
        self.assertEqual(r.signal["goal"], "goto")

    def test_goto_map_reaches_goal_through_door(self):
        sc = grids.CROSS_MAP_OPEN
        cross = _cross_map_run(sc, (4, 0), landing=(5, 0))
        policy = sim.scripted(goals=["goto"], goto=(0, 0), goto_map=2)
        r = sim.run(sc, policy, cross=cross, max_decisions=80)
        self.assertEqual(r.outcome, "reached")
        self.assertEqual(r.world.map_id, 2)
        walked = [row for row in r.trace if row["map_id"] == 2 and row.get("applied")]
        self.assertEqual(len(walked), 5, "walks from the landing to the goal on map 2")
        self.assertTrue(all(row["reason"].startswith("goto → ") for row in walked))
        self.assertEqual(r.memory.nav_stuck.stuck_signals, [])

    def test_goto_leg_tracks_the_door_and_backs_off_the_destination(self):
        sc = grids.CROSS_MAP_OPEN
        cross = _cross_map_run(sc, (4, 0), landing=(5, 0))
        w = sim.world_for(sc, cross=cross)
        m = Memory()
        policy = sim.scripted(goals=["goto"], goto=(0, 0), goto_map=2)
        self.assertIsNone(replan(w, m, policy, random.Random(0), set(), set(), cross.kb))
        att = nav_stuck.active(m, w)
        self.assertEqual((att.goal, att.target), ("goto", (4, 0)), "the door on this map")
        self.assertEqual(att.backoff_key, nav_stuck.goal_key("goto", 2, (0, 0)))

    def test_plan_travel_point_across_maps_tracks_the_door(self):
        sc = grids.CROSS_MAP_OPEN
        cross = _cross_map_run(sc, (4, 0), landing=(5, 0))
        w = sim.world_for(sc, cross=cross)
        pol = sim.scripted()
        point = {"op": "travel", "to": "point", "x": 0, "y": 0, "map_id": 2}
        path, label, leg = path_for_plan_op(point, w, Memory(), pol, set(), set(), cross.kb)
        self.assertEqual(path[-1], (4, 0))
        self.assertEqual(leg, nav_stuck.Leg((4, 0), nav_stuck.goal_key(label, 2, (0, 0))))
        town = {"op": "travel", "to": "town", "x": 0, "y": 0}
        w.respawn_anchors.append((2, (0, 0)))
        self.assertIsNone(path_for_plan_op(town, w, Memory(), pol, set(), set(), cross.kb), "town is this map's only")
        w.respawn_anchors.append((1, (6, 2)))
        _, _, leg = path_for_plan_op(town, w, Memory(), pol, set(), set(), cross.kb)
        self.assertEqual(leg, nav_stuck.Leg((6, 2)), "no ultimate key on this map")

    def test_leg_toward_keeps_a_lost_route_only_for_its_own_destination(self):
        sc = grids.CROSS_MAP_OPEN
        w = sim.world_for(sc)
        m = Memory()
        ultimate = nav_stuck.goal_key("goto", 2, (0, 0))
        self.assertIsNone(nav_stuck.leg_toward(m, w, "goto", 2, (0, 0), None), "no route ever known")
        nav_stuck.track(m, w, "goto", nav_stuck.Leg((4, 0), ultimate))
        self.assertEqual(nav_stuck.leg_toward(m, w, "goto", 2, (0, 0), None), nav_stuck.Leg((4, 0), ultimate))
        self.assertIsNone(nav_stuck.leg_toward(m, w, "goto", 2, (1, 0), None), "another destination")
        self.assertIsNone(nav_stuck.leg_toward(m, w, "travel:point", 2, (0, 0), None), "another goal")
        self.assertEqual(nav_stuck.leg_toward(m, w, "goto", 1, (3, 2), None), nav_stuck.Leg((3, 2)))

    def test_travel_point_cross_map_abandons_when_door_unreachable(self):
        sc = grids.CROSS_MAP_HEDGE
        cross = _cross_map_run(sc, (6, 3))
        m = Memory()
        m.travel_ops = [TravelOp("point", 0, 0, map_id=2)]
        r = sim.run(sc, sim.scripted(goals=["hold"]), memory=m, cross=cross)
        self.assertEqual(r.outcome, "abandoned")
        self.assertEqual(r.signal["goal"], "travel:point")
        key = nav_stuck.goal_key("travel:point", 2, (0, 0))
        self.assertTrue(nav_stuck.is_backed_off(r.memory.nav_stuck, key, r.world.tick))


    def test_travel_with_no_known_route_yields_without_backoff(self):
        sc = grids.CROSS_MAP_OPEN
        cross = _cross_map_run(sc, (4, 0), landing=(5, 0))
        m = Memory()
        m.travel_ops = [TravelOp("point", 2, 1, map_id=3)]
        r = sim.run(sc, sim.scripted(goals=["hold"]), memory=m, cross=cross, max_decisions=100)
        self.assertEqual(r.outcome, "budget")
        self.assertEqual(r.moves, 0)
        self.assertEqual(r.memory.nav_stuck.stuck_signals, [])
        self.assertEqual(r.memory.nav_stuck.backoff_until, {})
        self.assertEqual(r.memory.nav_stuck.attempts, {})
        self.assertIn("travel:point blocked", r.trace[-1]["reason"])


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
        sim.quiet_investigate(w, m, cross.kb)
        rng = random.Random(7)
        map2 = sim.map2_view()
        for row in trace:
            w.tick = row["tick"]
            w.map_id = row.get("map_id", 1)
            self.assertEqual(list(w.pos), row["pos"], row)
            d = decide(w, m, policy, rng, knowledge=cross.kb)
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
        for row in trace:
            w.tick = row["tick"]
            self.assertEqual(list(w.pos), row["pos"], row)
            d = decide(w, m, policy, rng)
            self.assertEqual((d.intent, d.reason), (row["intent"], row["reason"]), row)
            if d.intent is not None:
                self.assertEqual(sim.apply(w, m, sc, (d.intent["x"], d.intent["y"]), {}), row["applied"], row)
        self.assertEqual(m.nav_stuck.stuck_signals, recorded.memory.nav_stuck.stuck_signals)
        self.assertEqual(m.nav_stuck.backoff_until, recorded.memory.nav_stuck.backoff_until)


if __name__ == "__main__":
    unittest.main()
