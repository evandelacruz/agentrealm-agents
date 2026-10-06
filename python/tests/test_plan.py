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
from agentrealm_agent.pathing import note_goto_reached, path_for_plan_op, path_owned_by, replan
from agentrealm_agent.plan import (
    EXPLORE_ANYWHERE,
    MAX_WAIT_SECONDS,
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
from agentrealm_agent.states import PlayContext, dispatch
from agentrealm_agent.world import Entity, WorldModel
from tests.test_cost_grid import grid
from tests.test_runner import FakeClient


def open_world() -> WorldModel:
    """A 4x4 dirt patch at (0, 0) with fog around it."""
    return grid(["....", "....", "....", "...."], at=(0, 0))


def run(w: WorldModel, m: Memory, plan: Plan | None, policy: Policy | None = None, knowledge=None):
    """One decision through the state machine with ``plan``."""
    pol = policy if policy is not None else Policy(kind="scripted")
    return dispatch(w, PlayContext(m, pol, random.Random(0), knowledge=knowledge, plan=plan))


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

    def test_wait_needs_a_reason_and_a_short_time(self):
        ok = {"op": "wait", "seconds": MAX_WAIT_SECONDS, "why": "let the guard pass"}
        self.assertEqual(validate_goal_op(ok), ok)
        self.assertIsNone(validate_goal_op({"op": "wait", "seconds": 5}))
        self.assertIsNone(validate_goal_op({"op": "wait", "seconds": 5, "why": ""}))
        self.assertIsNone(validate_goal_op({"op": "wait", "seconds": MAX_WAIT_SECONDS + 1, "why": "x"}))
        self.assertIsNone(validate_goal_op({"op": "wait", "seconds": -1, "why": "x"}))

    def test_equip_code_is_optional(self):
        self.assertEqual(validate_goal_op({"op": "equip"}), {"op": "equip"})
        self.assertIsNotNone(validate_goal_op({"op": "equip", "code": "bronze_sword"}))
        self.assertIsNone(validate_goal_op({"op": "equip", "code": ""}))


class DirectivesGoalsTest(unittest.TestCase):
    def test_shorthand(self):
        ops = parse_directives_goals(["gather_gems:20", "buy:bronze_mail"])
        self.assertEqual(ops[0]["op"], "gather_gems")
        self.assertEqual(ops[0]["count"], 20)
        self.assertEqual(ops[1], {"op": "buy", "code": "bronze_mail"})

    def test_bad_shorthand_ignored(self):
        self.assertEqual(parse_directives_goals(["not-an-op"]), [])

    def test_travel_goals_become_travel_ops(self):
        self.assertEqual(
            parse_directives_goals(["travel:town", "travel:point:3:4"]),
            [{"op": "travel", "to": "town", "x": 0, "y": 0}, {"op": "travel", "to": "point", "x": 3, "y": 4}],
        )
        plan = Plan.from_directives(directive_goals=["travel:shop", "buy:torch"], directive_params=dict(PARAM_DEFAULTS))
        self.assertEqual(
            plan.goals, [{"op": "travel", "to": "shop", "x": 0, "y": 0}, {"op": "buy", "code": "torch"}]
        )


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
            [{"op": "set_param", "name": "curiosity", "value": 0.0}, {"op": "wait", "seconds": 0, "why": "t"}],
            dict(floor),
            floor_params=floor,
        )
        self.assertEqual(plan.current()["op"], "set_param", "current() only reads")
        plan.advance(WorldModel(character_id=1, map_id=1, pos=(0, 0)))
        self.assertEqual(plan.params["curiosity"], 0.0)
        self.assertIsNone(plan.current())

    def test_buy_stays_on_stack_for_shop(self):
        # No shop known: Shop sends nothing, the safe default moves, and the
        # op stays on top while its stall clock runs.
        plan = Plan([{"op": "buy", "code": "torch"}], dict(PARAM_DEFAULTS))
        m = Memory()
        out = run(open_world(), m, plan)
        self.assertEqual(plan.current(), {"op": "buy", "code": "torch"})
        self.assertEqual((out.state, m.goal), ("Explore", "explore"))
        self.assertIsNotNone(plan.stalled_since_tick)

    def test_ops_without_an_executor_are_dropped(self):
        plan = Plan(
            [{"op": "hunt", "npc_type": "rat"}, {"op": "avoid", "npc_type": "rat"},
             {"op": "travel", "to": "point", "x": 3, "y": 0}],
            dict(PARAM_DEFAULTS),
        )
        with self.assertLogs("agentrealm_agent.plan", "WARNING"):
            out = run(open_world(), Memory(), plan)
        self.assertEqual(plan.current()["op"], "travel")
        self.assertEqual(out.state, "Travel")

    def test_travel_shop_paths_to_a_known_cell(self):
        from agentrealm_agent.knowledge_base import KnowledgeBase
        from agentrealm_agent.travel.knowledge import record_shop_cell

        kb = KnowledgeBase.empty("sandbox")
        record_shop_cell(kb, 1, (3, 0))
        plan = Plan([{"op": "travel", "to": "shop", "x": 0, "y": 0}], dict(PARAM_DEFAULTS))
        m = Memory()
        out = run(grid(["...."], at=(0, 0)), m, plan, knowledge=kb)
        self.assertEqual(out.state, "Travel")
        self.assertEqual(plan.current()["op"], "travel")
        self.assertEqual(m.goal, "travel:shop")
        self.assertEqual(m.path[-1], (3, 0))

    def test_travel_shop_pops_at_a_bought_out_known_cell(self):
        from agentrealm_agent.knowledge_base import KnowledgeBase
        from agentrealm_agent.travel.knowledge import record_shop_cell

        kb = KnowledgeBase.empty("sandbox")
        record_shop_cell(kb, 1, (3, 0))
        for x, y in ((0, 0), (3, 0)):
            with self.subTest(target=(x, y)):
                plan = Plan([{"op": "travel", "to": "shop", "x": x, "y": y}], dict(PARAM_DEFAULTS))
                run(grid(["...."], at=(3, 0)), Memory(), plan, knowledge=kb)
                self.assertIsNone(plan.current())
                self.assertIsNone(plan.stalled_since_tick)

    def test_unpathable_op_is_dropped_after_the_stall_timeout(self):
        # Walled in: (9, 9) is never reachable, so the op must not hold the stack forever.
        w = grid(["###", "#.#", "###"], at=(1, 1))
        plan = Plan([{"op": "travel", "to": "point", "x": 9, "y": 9}, {"op": "wait", "seconds": 5, "why": "t"}],
                    dict(PARAM_DEFAULTS), tick_hz=10)
        m = Memory()
        w.tick = 100
        run(w, m, plan)
        self.assertEqual(plan.current()["op"], "travel", "not dropped on the first miss")
        w.tick = 100 + PLAN_STALL_SECONDS * 10 - 1
        run(w, m, plan)
        self.assertEqual(plan.current()["op"], "travel")
        w.tick += 1
        with self.assertLogs("agentrealm_agent.plan", "WARNING"):
            run(w, m, plan)
        self.assertEqual(plan.current()["op"], "wait")

    def test_safe_default_moving_is_not_acting_on_a_stalled_op(self):
        # A36: the safe default moves while the head op has no path, so the
        # round did not act on the op.
        w = grid(["###", "#.#", "###"], at=(1, 1))
        plan = Plan([{"op": "travel", "to": "point", "x": 9, "y": 9}], dict(PARAM_DEFAULTS))
        run(w, Memory(), plan)
        self.assertIsNotNone(plan.stalled_since_tick)
        self.assertIsNone(plan.acted)
        plan2 = Plan([{"op": "travel", "to": "point", "x": 3, "y": 3}], dict(PARAM_DEFAULTS))
        run(open_world(), Memory(), plan2)
        self.assertEqual(plan2.acted, plan2.current(), "a step toward the op is acting on it")

    def test_break_block_is_left_to_break_then_stalls_out(self):
        # Break owns `break_block`: dispatch leaves it on the stack (A36)
        # until it has stalled as long as any other op.
        w = open_world()
        brk = {"op": "break_block", "x": 3, "y": 3, "capability": "burn"}
        plan = Plan([brk, {"op": "wait", "seconds": 5, "why": "t"}], dict(PARAM_DEFAULTS), tick_hz=10)
        m = Memory()
        w.tick = 100
        out = run(w, m, plan)
        self.assertEqual(plan.current(), brk, "not dropped for want of a state")
        self.assertNotEqual(out.state, "Break", "nothing to burn with: Break sends nothing")
        w.tick = 100 + PLAN_STALL_SECONDS * 10
        with self.assertLogs("agentrealm_agent.plan", "WARNING"):
            run(w, m, plan)
        self.assertEqual(plan.current()["op"], "wait")

    def test_break_block_done_once_the_block_changes(self):
        w = open_world()
        w.view.tiles[(3, 3)] = "hedge"
        plan = Plan([{"op": "break_block", "x": 3, "y": 3, "capability": "burn"}], dict(PARAM_DEFAULTS))
        plan.advance(w)
        self.assertIsNotNone(plan.current(), "the hedge still stands")
        w.view.tiles[(3, 3)] = "grass"
        plan.advance(w)
        self.assertIsNone(plan.current())

    def test_a_step_toward_the_op_resets_the_stall_clock(self):
        w = open_world()
        plan = Plan([{"op": "travel", "to": "point", "x": 3, "y": 3}], dict(PARAM_DEFAULTS))
        plan.stalled_since_tick = 0
        w.tick = 5
        m = Memory()
        run(w, m, plan)
        self.assertEqual(m.goal, "travel:point")
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
        self.assertEqual(builtin_goals(Policy(kind="scripted", goals=[])), [])
        doors = builtin_goals(Policy(kind="scripted", goals=["doors"]))
        self.assertEqual([(o["op"], o["to"]) for o in doors], [("travel", "entrance")])

    def test_builtin_explore_walks_to_the_frontier(self):
        pol = Policy(kind="scripted", goals=["explore"])
        plan = Plan.from_policy(pol, dict(PARAM_DEFAULTS))
        w = open_world()
        m = Memory()
        out = run(w, m, plan, pol)
        self.assertEqual(out.state, "Explore")
        self.assertEqual(plan.current()["op"], "explore_area", "not popped while frontier remains")
        self.assertEqual(m.goal, "explore_area")
        self.assertEqual(m.goal_op, plan.current())
        self.assertIn(m.path[-1], w.view.frontier())

    def test_builtin_goto_is_walked_by_travel(self):
        pol = Policy(kind="scripted", goals=["goto"], goto=(3, 0))
        plan = Plan.from_policy(pol, dict(PARAM_DEFAULTS))
        m = Memory()
        out = run(open_world(), m, plan, pol)
        self.assertEqual(out.state, "Travel")
        self.assertEqual((m.goal, m.path[-1]), ("travel:point", (3, 0)))

    def test_replan_without_op_is_the_safe_default(self):
        w = open_world()
        m = Memory()
        self.assertIsNone(replan(w, m, Policy(kind="scripted"), set(), set()))
        self.assertEqual((m.goal, m.goal_op), ("explore", None))
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
        text = json.dumps({"goals": [{"op": "wait", "seconds": 0, "why": "t"}], "notes": "n"})
        plan = load_plan_json(text, floor_params=dict(PARAM_DEFAULTS))
        self.assertEqual(plan.notes, "n")
        self.assertEqual(plan.current()["op"], "wait")

    def test_load_plan_json_drops_a_wait_without_a_reason(self):
        text = json.dumps({"goals": [{"op": "wait", "seconds": 5}]})
        with self.assertLogs("agentrealm_agent.plan", "WARNING"):
            plan = load_plan_json(text, floor_params=dict(PARAM_DEFAULTS))
        self.assertIsNone(plan.current())


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

    def test_travel_shop_done_at_cell_or_priced_supply(self):
        op = {"op": "travel", "to": "shop", "x": 4, "y": 0}
        plan = Plan([op], dict(PARAM_DEFAULTS))
        self.assertTrue(goal_done(op, WorldModel(character_id=1, map_id=1, pos=(4, 0)), plan))
        w = WorldModel(character_id=1, map_id=1, pos=(2, 0))
        w.entities = [Entity("supply", 1, (2, 0), "torch", gem_price=3)]
        self.assertTrue(goal_done({"op": "travel", "to": "shop", "x": 0, "y": 0}, w, plan))
        # An explicit cell is not done at a priced supply elsewhere.
        self.assertFalse(goal_done(op, w, plan))

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
        op = {"op": "wait", "seconds": 2, "why": "t"}
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


class ExecutorTest(unittest.TestCase):
    def test_plan_wait_holds_without_moving(self):
        w = open_world()
        w.tick = 7
        plan = Plan([{"op": "wait", "seconds": 1, "why": "let the gate open"}], dict(PARAM_DEFAULTS), tick_hz=10)
        m = Memory()
        out = run(w, m, plan)
        self.assertEqual(out.state, "Wait")
        self.assertIsNone(out.intents)
        self.assertTrue(out.wait)
        self.assertEqual(out.reason, "plan wait: let the gate open")
        self.assertEqual((plan.wait_started_tick, m.path), (7, []))
        self.assertIsNone(plan.stalled_since_tick, "holding for its own op is not a stall")
        w.tick = 17
        out = run(w, m, plan)
        self.assertIsNone(plan.current(), "wait done after its seconds")
        self.assertEqual(out.state, "Explore")
        self.assertIsNotNone(out.intents, "the safe default moves")

    def test_stale_path_does_not_override_the_plan_head(self):
        w = open_world()
        plan = Plan([{"op": "travel", "to": "point", "x": 0, "y": 3}], dict(PARAM_DEFAULTS))
        m = Memory()
        m.path, m.goal = [(1, 0)], "explore"
        out = run(w, m, plan)
        self.assertEqual(m.goal, "travel:point")
        self.assertEqual(m.path[-1], (0, 3))
        self.assertNotEqual((out.intents[0]["x"], out.intents[0]["y"]), (1, 0), "stale explore step not taken")

    def test_same_kind_head_swap_replans(self):
        # A new stack (strategist, directives reload) clears the walk, as the runner does.
        w = open_world()
        m = Memory()
        old = Plan([{"op": "travel", "to": "point", "x": 3, "y": 0}], dict(PARAM_DEFAULTS))
        run(w, m, old)
        self.assertEqual((m.goal, m.path[-1]), ("travel:point", (3, 0)))
        m.path, m.goal, m.goal_op = [], "", None
        new = Plan([{"op": "travel", "to": "point", "x": 0, "y": 3}], dict(PARAM_DEFAULTS))
        out = run(w, m, new)
        self.assertEqual(m.path[-1], (0, 3))
        self.assertEqual((out.intents[0]["x"], out.intents[0]["y"]), m.path[0])

    def test_dropped_travel_op_path_does_not_drive_the_next_one(self):
        w = open_world()
        m = Memory()
        plan = Plan([{"op": "travel", "to": "point", "x": 3, "y": 0},
                     {"op": "travel", "to": "point", "x": 0, "y": 3}], dict(PARAM_DEFAULTS))
        run(w, m, plan)
        plan.drop_current("test")
        out = run(w, m, plan)
        self.assertEqual(m.path[-1], (0, 3), "path toward the dropped op's target is not walked")
        self.assertEqual((out.intents[0]["x"], out.intents[0]["y"]), m.path[0])

    def test_explore_area_head_swap_replans(self):
        w = grid(["........", "........", "........", "........"], at=(3, 0))
        m = Memory()
        old = Plan([{"op": "explore_area", "x": 7, "y": 0, "radius": 1}], dict(PARAM_DEFAULTS))
        run(w, m, old)
        self.assertEqual(m.goal_op, old.current())
        new_op = {"op": "explore_area", "x": 0, "y": 3, "radius": 1}
        run(w, m, Plan([new_op], dict(PARAM_DEFAULTS)))
        self.assertEqual(m.goal_op, new_op, "a path for the old op does not keep driving movement")
        self.assertLessEqual(max(abs(m.path[-1][0]), abs(m.path[-1][1] - 3)), 1)


class PathForPlanOpTest(unittest.TestCase):
    def test_explore_area_paths_to_frontier_in_the_area(self):
        op = {"op": "explore_area", "x": 3, "y": 0, "radius": 1}
        path, label, _ = path_for_plan_op(op, open_world(), Memory(), Policy(kind="scripted"), set(), set(), None)
        self.assertEqual(label, "explore_area")
        self.assertLessEqual(max(abs(path[-1][0] - 3), abs(path[-1][1])), 1)

    def test_travel_ops_have_no_plan_path(self):
        # Travel walks `travel` ops itself (A27).
        op = {"op": "travel", "to": "point", "x": 3, "y": 2}
        self.assertIsNone(path_for_plan_op(op, open_world(), Memory(), Policy(kind="scripted"), set(), set(), None))

    def test_path_owned_by(self):
        op = {"op": "explore_area", "x": 3, "y": 0, "radius": 1}
        m = Memory()
        m.goal, m.goal_op = "explore_area", dict(op)
        self.assertTrue(path_owned_by(op, m))
        self.assertFalse(path_owned_by({**op, "x": 0}, m), "same kind, other target")
        self.assertFalse(path_owned_by(None, m))
        m.goal, m.goal_op = "explore", None
        self.assertTrue(path_owned_by(None, m), "the safe default's walk")
        self.assertFalse(path_owned_by(op, m))

    def test_other_ops_have_no_path(self):
        self.assertIsNone(path_for_plan_op({"op": "wait", "seconds": 1, "why": "t"}, open_world(), Memory(),
                                           Policy(kind="scripted"), set(), set(), None))

    def test_compose_goal_done_when_whole_held(self):
        from agentrealm_agent.item_table import InventorySupply

        op = {"op": "compose", "composes_into": "master_key"}
        w = WorldModel(character_id=1, map_id=1, pos=(0, 0))
        w.held_supplies = [InventorySupply(1, "master_key")]
        self.assertTrue(goal_done(op, w, Plan([op], dict(PARAM_DEFAULTS))))


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
        cfg = CharacterConfig("T", "sandbox",
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
        r.mem.path, r.mem.goal, r.mem.goal_op = [(1, 0), (2, 0), (3, 0)], "travel:point", op
        self.reload(r, 'goals = ["buy:torch"]\nparams = { risk = 0.3 }\n')
        self.assertEqual(r.mem.goal_op, op, "same goals: path kept")
        self.reload(r, 'goals = ["buy:lamp"]\n')
        self.assertEqual((r.mem.path, r.mem.goal, r.mem.goal_op), ([], "", None))

    def test_a_rebuilt_builtin_plan_does_not_walk_back_to_a_reached_goto(self):
        # A16: a goals reload that falls back to the built-in plan must not add
        # back the travel op for a goto the agent already stood on.
        r = self.runner('goals = ["buy:torch"]\n', ["goto", "explore"])
        r.cfg.policy.goto = (3, 0)
        r.world.pos = (3, 0)
        note_goto_reached(r.world, r.mem, r.cfg.policy)
        r.world.pos = (0, 0)
        self.reload(r, "goals = []\n")
        self.assertNotIn("travel", [o["op"] for o in r.plan.goals])
        self.assertEqual([o["op"] for o in r.plan.goals], ["explore_area"])
        r._decide(r.world, r.mem, plan=r.plan)
        self.assertNotEqual(r.mem.goal, "travel:point", "no walk back")

    def test_a_rebuilt_builtin_plan_keeps_an_unreached_goto(self):
        r = self.runner('goals = ["buy:torch"]\n', ["goto", "explore"])
        r.cfg.policy.goto = (3, 0)
        self.reload(r, "goals = []\n")
        self.assertEqual([o["op"] for o in r.plan.goals], ["travel", "explore_area"])

    def test_reflex_probe_leaves_the_plan_alone(self):
        r = self.runner("", ["explore"])
        r.plan = Plan([{"op": "buy", "code": "torch"}, {"op": "wait", "seconds": 0, "why": "t"}], dict(PARAM_DEFAULTS))
        self.assertIsNone(r.reflex_while_held())
        self.assertEqual(r.plan.index, 0)
        self.assertIsNone(r.plan.stalled_since_tick)

    def test_reflex_probe_restores_params_and_wait_clock(self):
        r = self.runner("", ["explore"])
        r.world.tick = 40
        floor = dict(PARAM_DEFAULTS)
        r.plan = Plan([{"op": "set_param", "name": "curiosity", "value": 0.0}, {"op": "wait", "seconds": 5, "why": "t"}],
                      dict(floor), floor_params=floor)
        self.assertIsNone(r.reflex_while_held())
        self.assertEqual((r.plan.index, r.plan.params), (0, floor))
        self.assertIsNone(r.plan.wait_started_tick)
        r._decide(r.world, r.mem, plan=r.plan)
        self.assertEqual((r.plan.index, r.plan.params["curiosity"]), (1, 0.0), "the real window does advance")
        self.assertEqual(r.plan.wait_started_tick, 40)


if __name__ == "__main__":
    unittest.main()
