"""A34: plan schema, goal stack, param limits, built-in plan."""

import json
import unittest

from agentrealm_agent.config import Policy
from agentrealm_agent.directives import PARAM_DEFAULTS
from agentrealm_agent.memory import Memory
from agentrealm_agent.plan import (
    Plan,
    apply_strategist_params,
    builtin_goals,
    goal_done,
    load_plan_json,
    parse_directives_goals,
    parse_plan_payload,
    validate_goal_op,
)
from agentrealm_agent.world import WorldModel


class ValidateOpTest(unittest.TestCase):
    def test_travel_point(self):
        op = validate_goal_op({"op": "travel", "to": "point", "x": 1, "y": 2})
        self.assertEqual(op, {"op": "travel", "to": "point", "x": 1, "y": 2})

    def test_drops_unknown_op(self):
        self.assertIsNone(validate_goal_op({"op": "fly_away"}))

    def test_drops_bad_break_capability(self):
        self.assertIsNone(validate_goal_op({"op": "break_block", "x": 0, "y": 0, "capability": "magic"}))

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
        w = WorldModel(character_id=1, map_id=1, pos=(0, 0))
        plan.advance_if_done(w, Memory(), Policy(kind="scripted"))
        self.assertEqual(plan.params["curiosity"], 0.0)
        self.assertIsNone(plan.current())

    def test_buy_skipped_without_shop_state(self):
        plan = Plan([{"op": "buy", "code": "torch"}], dict(PARAM_DEFAULTS))
        w = WorldModel(character_id=1, map_id=1, pos=(0, 0), perception=3)
        for x in range(-1, 4):
            for y in range(-1, 4):
                w.view.tiles[(x, y)] = "dirt"
        from agentrealm_agent.pathing import replan

        m = Memory()
        replan(w, m, Policy(kind="scripted", goals=["explore"]), __import__("random").Random(0), set(), set(), plan=plan)
        self.assertIsNone(plan.current())
        self.assertEqual(m.goal, "explore")


class BuiltinPlanTest(unittest.TestCase):
    def test_maps_policy_goals(self):
        pol = Policy(kind="scripted", goals=["explore", "goto"], goto=(3, 4))
        ops = builtin_goals(pol)
        self.assertEqual(ops[0]["op"], "explore_area")
        self.assertEqual(ops[1]["to"], "point")
        self.assertEqual((ops[1]["x"], ops[1]["y"]), (3, 4))


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
        plan = Plan([op], dict(PARAM_DEFAULTS))
        self.assertTrue(goal_done(op, w, Memory(), Policy(kind="scripted"), plan))


if __name__ == "__main__":
    unittest.main()
