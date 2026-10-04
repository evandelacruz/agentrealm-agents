"""A34: plan schema, goal stack, param limits, built-in plan."""

import json
import os
import random
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agentrealm_agent import config
from agentrealm_agent.config import CharacterConfig, Policy
from agentrealm_agent.directives import PARAM_DEFAULTS
from agentrealm_agent.memory import Memory
from agentrealm_agent.pathing import path_for_plan_op, replan
from agentrealm_agent.plan import (
    EXPLORE_ANYWHERE,
    PLAN_STALL_SECONDS,
    Plan,
    apply_strategist_params,
    builtin_goals,
    goal_done,
    load_plan_json,
    parse_directives_goals,
    parse_plan_payload,
    validate_goal_op,
)
from agentrealm_agent.runner import Runner
from agentrealm_agent.states.explore import scripted_outcome
from agentrealm_agent.world import WorldModel
from tests.test_cost_grid import grid
from tests.test_runner import FakeClient


def open_world() -> WorldModel:
    """A 4x4 dirt patch at (0, 0) with fog around it."""
    return grid(["....", "....", "....", "...."], at=(0, 0))


class ValidateOpTest(unittest.TestCase):
    def test_travel_point(self):
        op = validate_goal_op({"op": "travel", "to": "point", "x": 1, "y": 2})
        self.assertEqual(op, {"op": "travel", "to": "point", "x": 1, "y": 2})

    def test_drops_unknown_op(self):
        self.assertIsNone(validate_goal_op({"op": "fly_away"}))

    def test_drops_bad_break_capability(self):
        self.assertIsNone(validate_goal_op({"op": "break_block", "x": 0, "y": 0, "capability": "magic"}))

    def test_fight_boss_preconditions(self):
        op = validate_goal_op({"op": "fight_boss", "x": 1, "y": 2, "min_potions": 1})
        self.assertEqual(op, {"op": "fight_boss", "x": 1, "y": 2, "min_potions": 1})
        self.assertIsNone(validate_goal_op({"op": "fight_boss", "x": 1, "y": 2, "min_health": -1}))

    def test_say_needs_one_npc_field(self):
        self.assertIsNone(validate_goal_op({"op": "say", "text": "hi"}))
        self.assertIsNotNone(validate_goal_op({"op": "say", "npc_type": "guard", "text": "hi"}))

    def test_set_param_rejects_never_attack(self):
        self.assertIsNone(validate_goal_op({"op": "set_param", "name": "never_attack", "value": 1}))


class DirectivesGoalsTest(unittest.TestCase):
    def test_shorthand(self):
        ops = parse_directives_goals(["gather_gems:20", "buy:bronze_mail"])
        self.assertEqual(ops[0]["op"], "gather_gems")
        self.assertEqual(ops[0]["count"], 20)
        self.assertEqual(ops[1], {"op": "buy", "code": "bronze_mail"})

    def test_bad_shorthand_ignored(self):
        self.assertEqual(parse_directives_goals(["not-an-op"]), [])

    def test_travel_goals_are_left_to_travel(self):
        with self.assertNoLogs("agentrealm_agent.plan", level="WARNING"):
            self.assertEqual(parse_directives_goals(["travel:town", "travel:point:3:4"]), [])
            self.assertIsNone(Plan.from_directives(directive_goals=["travel:shop"], directive_params=dict(PARAM_DEFAULTS)))
        plan = Plan.from_directives(directive_goals=["travel:town", "buy:torch"], directive_params=dict(PARAM_DEFAULTS))
        self.assertEqual(plan.goals, [{"op": "buy", "code": "torch"}])


class ParamLimitsTest(unittest.TestCase):
    def test_strategist_may_raise_fight_margin(self):
        floor = dict(PARAM_DEFAULTS)
        floor["fight_margin"] = 2.0
        out = apply_strategist_params(floor, dict(floor), {"fight_margin": 2.5})
        self.assertEqual(out["fight_margin"], 2.5)

    def test_strategist_cannot_loosen_fight_margin(self):
        floor = dict(PARAM_DEFAULTS)
        floor["fight_margin"] = 2.0
        out = apply_strategist_params(floor, dict(floor), {"fight_margin": 1.5})
        self.assertEqual(out["fight_margin"], 2.0)

    def test_strategist_may_lower_risk(self):
        floor = dict(PARAM_DEFAULTS)
        out = apply_strategist_params(floor, dict(floor), {"risk": 0.2})
        self.assertEqual(out["risk"], 0.2)

    def test_strategist_cannot_raise_risk(self):
        floor = dict(PARAM_DEFAULTS)
        out = apply_strategist_params(floor, dict(floor), {"risk": 0.9})
        self.assertEqual(out["risk"], floor["risk"])


class GoalStackTest(unittest.TestCase):
    def test_set_param_runs_when_reached(self):
        floor = dict(PARAM_DEFAULTS)
        plan = Plan(
            [{"op": "set_param", "name": "curiosity", "value": 0.0}, {"op": "wait", "seconds": 0}],
            dict(floor),
            floor_params=floor,
        )
        self.assertEqual(plan.current()["op"], "set_param", "current() only reads")
        plan.advance(WorldModel(character_id=1, map_id=1, pos=(0, 0)))
        self.assertEqual(plan.params["curiosity"], 0.0)
        self.assertIsNone(plan.current())

    def test_buy_skipped_without_shop_state(self):
        plan = Plan([{"op": "buy", "code": "torch"}], dict(PARAM_DEFAULTS))
        m = Memory()
        with self.assertLogs("agentrealm_agent.plan", "WARNING"):
            replan(open_world(), m, Policy(kind="scripted", goals=["explore"]), random.Random(0),
                   set(), set(), plan=plan)
        self.assertIsNone(plan.current())
        self.assertEqual(m.goal, "explore")

    def test_travel_to_an_unpathed_destination_is_dropped_at_once(self):
        for to in ("shop", "hunting_ground"):
            with self.subTest(to=to):
                plan = Plan([{"op": "travel", "to": to, "x": 1, "y": 1}], dict(PARAM_DEFAULTS))
                m = Memory()
                with self.assertLogs("agentrealm_agent.plan", "WARNING"):
                    replan(open_world(), m, Policy(kind="scripted", goals=["explore"]), random.Random(0),
                           set(), set(), plan=plan)
                self.assertIsNone(plan.current())
                self.assertEqual(m.goal, "explore")

    def test_unpathable_op_is_dropped_after_the_stall_timeout(self):
        # Walled in: (9, 9) is never reachable, so the op must not hold the stack forever.
        w = grid(["###", "#.#", "###"], at=(1, 1))
        plan = Plan([{"op": "travel", "to": "point", "x": 9, "y": 9}, {"op": "wait", "seconds": 5}],
                    dict(PARAM_DEFAULTS), tick_hz=10)
        pol = Policy(kind="scripted", goals=["hold"])
        w.tick = 100
        replan(w, Memory(), pol, random.Random(0), set(), set(), plan=plan)
        self.assertEqual(plan.current()["op"], "travel", "not dropped on the first miss")
        w.tick = 100 + PLAN_STALL_SECONDS * 10 - 1
        replan(w, Memory(), pol, random.Random(0), set(), set(), plan=plan)
        self.assertEqual(plan.current()["op"], "travel")
        w.tick += 1
        with self.assertLogs("agentrealm_agent.plan", "WARNING"):
            replan(w, Memory(), pol, random.Random(0), set(), set(), plan=plan)
        self.assertEqual(plan.current()["op"], "wait")

    def test_a_found_path_resets_the_stall_clock(self):
        w = open_world()
        plan = Plan([{"op": "travel", "to": "point", "x": 3, "y": 3}], dict(PARAM_DEFAULTS))
        plan.stalled_since_tick = 0
        w.tick = 5
        m = Memory()
        replan(w, m, Policy(kind="scripted", goals=[]), random.Random(0), set(), set(), plan=plan)
        self.assertEqual(m.goal, "plan_travel")
        self.assertIsNone(plan.stalled_since_tick)


class BuiltinPlanTest(unittest.TestCase):
    def test_maps_policy_goals(self):
        pol = Policy(kind="scripted", goals=["explore", "goto"], goto=(3, 4))
        ops = builtin_goals(pol)
        self.assertEqual([o["op"] for o in ops], ["explore_area", "travel"])
        self.assertEqual(ops[1]["to"], "point")
        self.assertEqual((ops[1]["x"], ops[1]["y"]), (3, 4))

    def test_mirrors_policy_goals_without_an_extra_entrance(self):
        self.assertEqual([o["op"] for o in builtin_goals(Policy(kind="scripted", goals=["explore"]))],
                         ["explore_area"])
        self.assertEqual(builtin_goals(Policy(kind="scripted", goals=["wander"])), [])
        doors = builtin_goals(Policy(kind="scripted", goals=["doors"]))
        self.assertEqual([(o["op"], o["to"]) for o in doors], [("travel", "entrance")])

    def test_builtin_explore_walks_to_the_frontier(self):
        pol = Policy(kind="scripted", goals=["explore"])
        plan = Plan.from_policy(pol, dict(PARAM_DEFAULTS))
        w = open_world()
        m = Memory()
        replan(w, m, pol, random.Random(0), set(), set(), plan=plan)
        self.assertEqual(plan.current()["op"], "explore_area", "not popped while frontier remains")
        self.assertEqual(m.goal, "explore_area")
        self.assertIn(m.path[-1], w.view.frontier())

    def test_all_invalid_directive_goals_fall_back_with_a_log(self):
        with self.assertLogs("agentrealm_agent.plan", "WARNING") as logs:
            self.assertIsNone(Plan.from_directives(directive_goals=["nope"], directive_params={}))
        self.assertTrue(any("built-in plan" in line for line in logs.output))


class JsonPlanTest(unittest.TestCase):
    def test_parse_payload(self):
        raw = {
            "goals": [{"op": "buy", "code": "torch"}, {"op": "travel", "to": "town", "x": 0, "y": 0}],
            "params": {"fight_margin": 2.0},
            "notes": "test",
        }
        goals, params, notes = parse_plan_payload(raw, floor_params=dict(PARAM_DEFAULTS))
        self.assertEqual(len(goals), 2)
        self.assertEqual(notes, "test")
        self.assertEqual(params["fight_margin"], 2.0)

    def test_load_plan_json(self):
        text = json.dumps({"goals": [{"op": "wait", "seconds": 0}], "notes": "n"})
        plan = load_plan_json(text, floor_params=dict(PARAM_DEFAULTS))
        self.assertEqual(plan.notes, "n")
        self.assertEqual(plan.current()["op"], "wait")


class GoalDoneTest(unittest.TestCase):
    def test_travel_point_done_at_target(self):
        op = {"op": "travel", "to": "point", "x": 2, "y": 3}
        w = WorldModel(character_id=1, map_id=1, pos=(2, 3))
        self.assertTrue(goal_done(op, w, Plan([op], dict(PARAM_DEFAULTS))))
        self.assertFalse(goal_done({**op, "map_id": 2}, w, Plan([op], dict(PARAM_DEFAULTS))))

    def test_travel_entrance_done_on_a_door(self):
        op = {"op": "travel", "to": "entrance", "x": 0, "y": 0}
        plan = Plan([op], dict(PARAM_DEFAULTS))
        self.assertTrue(goal_done(op, grid(["D."], at=(0, 0)), plan))
        self.assertFalse(goal_done(op, grid(["D."], at=(1, 0)), plan))

    def test_unbounded_explore_done_only_without_frontier(self):
        op = {"op": "explore_area", "x": 0, "y": 0, "radius": EXPLORE_ANYWHERE}
        plan = Plan([op], dict(PARAM_DEFAULTS))
        self.assertFalse(goal_done(op, open_world(), plan), "frontier left: not done where we stand")
        self.assertTrue(goal_done(op, grid(["###", "#.#", "###"], at=(1, 1)), plan))

    def test_bounded_explore_needs_the_area_seen(self):
        w = grid(["###", "#.#", "###"], at=(1, 1))
        far = {"op": "explore_area", "x": 50, "y": 50, "radius": 2}
        plan = Plan([far], dict(PARAM_DEFAULTS))
        self.assertFalse(goal_done(far, w, plan), "nothing seen there yet")
        near = {"op": "explore_area", "x": 1, "y": 1, "radius": 2}
        self.assertTrue(goal_done(near, w, plan))
        self.assertFalse(goal_done(near, open_world(), plan))

    def test_wait_counts_seconds_at_the_world_tick_rate(self):
        op = {"op": "wait", "seconds": 2}
        plan = Plan([op], dict(PARAM_DEFAULTS), tick_hz=4)
        w = WorldModel(character_id=1, map_id=1, pos=(0, 0))
        w.tick = 10
        self.assertFalse(goal_done(op, w, plan))
        self.assertIsNone(plan.wait_started_tick, "goal_done does not mutate")
        plan.advance(w)
        self.assertEqual(plan.wait_started_tick, 10)
        w.tick = 17
        plan.advance(w)
        self.assertIsNotNone(plan.current())
        w.tick = 18
        plan.advance(w)
        self.assertIsNone(plan.current())


class ScriptedOutcomeTest(unittest.TestCase):
    def test_plan_wait_holds_without_moving(self):
        w = open_world()
        w.tick = 7
        plan = Plan([{"op": "wait", "seconds": 1}], dict(PARAM_DEFAULTS), tick_hz=10)
        m = Memory()
        out = scripted_outcome(w, m, Policy(kind="scripted", goals=["explore"]), random.Random(0),
                               never_attack=[], plan=plan)
        self.assertIsNone(out.intents)
        self.assertEqual(out.reason, "plan wait")
        self.assertEqual((plan.wait_started_tick, m.path), (7, []))
        w.tick = 17
        out = scripted_outcome(w, m, Policy(kind="scripted", goals=["explore"]), random.Random(0),
                               never_attack=[], plan=plan)
        self.assertIsNone(plan.current(), "wait done after its seconds")
        self.assertIsNotNone(out.intents, "falls back to policy goals")

    def test_stale_path_does_not_override_the_plan_head(self):
        w = open_world()
        plan = Plan([{"op": "travel", "to": "point", "x": 0, "y": 3}], dict(PARAM_DEFAULTS))
        m = Memory()
        m.path, m.goal = [(1, 0)], "explore"
        out = scripted_outcome(w, m, Policy(kind="scripted", goals=["explore"]), random.Random(0),
                               never_attack=[], plan=plan)
        self.assertEqual(m.goal, "plan_travel")
        self.assertEqual(m.path[-1], (0, 3))
        self.assertNotEqual((out.intents[0]["x"], out.intents[0]["y"]), (1, 0), "stale explore step not taken")

    def test_same_kind_head_swap_replans(self):
        w = open_world()
        pol = Policy(kind="scripted")
        old = Plan([{"op": "travel", "to": "point", "x": 3, "y": 0}], dict(PARAM_DEFAULTS))
        m = Memory()
        scripted_outcome(w, m, pol, random.Random(0), never_attack=[], plan=old)
        self.assertEqual((m.goal, m.path[-1]), ("plan_travel", (3, 0)))
        new = Plan([{"op": "travel", "to": "point", "x": 0, "y": 3}], dict(PARAM_DEFAULTS))
        out = scripted_outcome(w, m, pol, random.Random(0), never_attack=[], plan=new)
        self.assertEqual(m.path[-1], (0, 3), "path toward the old head's target is dropped")
        self.assertEqual((out.intents[0]["x"], out.intents[0]["y"]), m.path[0])


    def test_stalled_head_keeps_the_wander_step(self):
        w = open_world()
        pol = Policy(kind="scripted", goals=["wander"])
        plan = Plan([{"op": "travel", "to": "town", "x": 0, "y": 0}], dict(PARAM_DEFAULTS))
        m = Memory()
        rng = random.Random(0)
        first = scripted_outcome(w, m, pol, rng, never_attack=[], plan=plan)
        self.assertIsNotNone(plan.stalled_since_tick, "no town known: the head stalls")
        self.assertEqual(m.goal, "wander")
        steps = {(first.intents[0]["x"], first.intents[0]["y"])}
        for tick in range(1, 6):
            w.tick = tick
            out = scripted_outcome(w, m, pol, rng, never_attack=[], plan=plan)
            steps.add((out.intents[0]["x"], out.intents[0]["y"]))
        self.assertEqual(len(steps), 1, "wander is not re-rolled while the head stalls")
        self.assertEqual(plan.current()["to"], "town", "still within the stall window")


class PathForPlanOpTest(unittest.TestCase):
    def test_explore_area_paths_to_frontier_in_the_area(self):
        op = {"op": "explore_area", "x": 3, "y": 0, "radius": 1}
        path, label, _ = path_for_plan_op(op, open_world(), Memory(), Policy(kind="scripted"), set(), set(), None)
        self.assertEqual(label, "explore_area")
        self.assertLessEqual(max(abs(path[-1][0] - 3), abs(path[-1][1])), 1)

    def test_travel_point_and_town(self):
        w = open_world()
        pol = Policy(kind="scripted")
        path, label, _ = path_for_plan_op({"op": "travel", "to": "point", "x": 3, "y": 2}, w, Memory(), pol,
                                       set(), set(), None)
        self.assertEqual((path[-1], label), ((3, 2), "plan_travel"))
        town = {"op": "travel", "to": "town", "x": 0, "y": 0}
        self.assertIsNone(path_for_plan_op(town, w, Memory(), pol, set(), set(), None), "no anchor known")
        w.respawn_anchors.append((1, (2, 2)))
        path, label, _ = path_for_plan_op(town, w, Memory(), pol, set(), set(), None)
        self.assertEqual((path[-1], label), ((2, 2), "plan_town"))

    def test_other_ops_have_no_path(self):
        self.assertIsNone(path_for_plan_op({"op": "wait", "seconds": 1}, open_world(), Memory(),
                                           Policy(kind="scripted"), set(), set(), None))


class RunnerPlanTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        patch = mock.patch.object(config, "STATE_DIR", self.dir)
        patch.start()
        self.addCleanup(patch.stop)

    def runner(self, directives: str, goals: list[str]) -> Runner:
        (self.dir / "T.directives.toml").write_text(directives)
        cfg = CharacterConfig("T", "default", "test", "sandbox",
                              Policy(kind="scripted", goals=goals, pickup=False), self.dir / "T.toml")
        r = Runner(cfg, FakeClient([]), 1, threading.Event(), out=lambda _: None)
        self.addCleanup(r.trace.close)
        r.world, r.mem = open_world(), Memory(need_self=False, need_position=False)
        return r

    def reload(self, r: Runner, text: str) -> None:
        path = self.dir / "T.directives.toml"
        path.write_text(text)
        st = path.stat()
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
        old = r.directives.directives.goals
        self.assertTrue(r.directives.maybe_reload())
        r.reload_directives(old)

    def test_reload_keeps_progress_unless_goals_change(self):
        r = self.runner('goals = ["buy:torch", "buy:rope"]\n', ["explore"])
        r.plan.index = 1
        self.reload(r, 'goals = ["buy:torch", "buy:rope"]\nparams = { risk = 0.3 }\n')
        self.assertEqual(r.plan.index, 1, "same goals: progress kept")
        self.assertEqual(r.plan.params["risk"], 0.3)
        self.reload(r, 'goals = ["buy:lamp"]\n')
        self.assertEqual((r.plan.index, r.plan.goals), (0, [{"op": "buy", "code": "lamp"}]))

    def test_goals_reload_drops_the_current_path(self):
        r = self.runner('goals = ["buy:torch"]\n', ["explore"])
        op = {"op": "travel", "to": "point", "x": 3, "y": 0}
        r.mem.path, r.mem.goal, r.mem.goal_op = [(1, 0), (2, 0), (3, 0)], "plan_travel", op
        self.reload(r, 'goals = ["buy:torch"]\nparams = { risk = 0.3 }\n')
        self.assertEqual(r.mem.goal_op, op, "same goals: path kept")
        self.reload(r, 'goals = ["buy:lamp"]\n')
        self.assertEqual((r.mem.path, r.mem.goal, r.mem.goal_op), ([], "", None))

    def test_reflex_probe_leaves_the_plan_alone(self):
        r = self.runner("", ["explore"])
        r.plan = Plan([{"op": "buy", "code": "torch"}, {"op": "wait", "seconds": 0}], dict(PARAM_DEFAULTS))
        self.assertIsNone(r.reflex_while_held())
        self.assertEqual(r.plan.index, 0)
        self.assertIsNone(r.plan.stalled_since_tick)

    def test_reflex_probe_restores_params_and_wait_clock(self):
        r = self.runner("", ["explore"])
        r.world.tick = 40
        floor = dict(PARAM_DEFAULTS)
        r.plan = Plan([{"op": "set_param", "name": "curiosity", "value": 0.0}, {"op": "wait", "seconds": 5}],
                      dict(floor), floor_params=floor)
        self.assertIsNone(r.reflex_while_held())
        self.assertEqual((r.plan.index, r.plan.params), (0, floor))
        self.assertIsNone(r.plan.wait_started_tick)
        r._decide(r.world, r.mem, plan=r.plan)
        self.assertEqual((r.plan.index, r.plan.params["curiosity"]), (1, 0.0), "the real window does advance")
        self.assertEqual(r.plan.wait_started_tick, 40)


if __name__ == "__main__":
    unittest.main()
