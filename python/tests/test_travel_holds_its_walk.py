"""A120: Travel keeps its walk; the safe default never stands in for it.

On unexplored ground Travel walks toward its destination through unseen
cells. With no step toward it, it raises one ``no_way_on`` trigger with why
and holds while the planner answers; the survival reflexes still act above
it. At stuck step 2 it yields to Break, and Explore holds rather than walk
the character off the way. With no planner (``--no-planner``) the built-in
plan answers the ask by exploring (``plan.builtin_way_on``).
"""

from __future__ import annotations

import random
import unittest
from types import SimpleNamespace
from unittest import mock

from agentrealm_agent.brain import decide
from agentrealm_agent.config import Policy
from agentrealm_agent.directives import PARAM_DEFAULTS, default_directives
from agentrealm_agent.memory import Memory
from agentrealm_agent.navigation import stuck as nav_stuck
from agentrealm_agent.plan import BUILTIN_EXPLORE_RADIUS, EXPLORE_ANYWHERE, Plan, builtin_way_on, validate_goal_op
from agentrealm_agent.states import PlayContext, dispatch
from agentrealm_agent.states.ask import ask_the_planner
from agentrealm_agent.strategist import MAP_RADIUS, Strategist
from agentrealm_agent.world import Entity, WorldModel, chebyshev

GLYPHS = {".": "dirt", "#": "wall"}

# Walled ground with the destination (5, 1) in a pocket of its own: no route.
POCKET = [
    "#######",
    "#...#.#",
    "#...#.#",
    "#######",
]
WHY = "travel:point 5,1: no step toward it"


def world(rows: list[str], at=(1, 1)) -> WorldModel:
    w = WorldModel(character_id=1, map_id=7, pos=at, perception=3)
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            w.view.tiles[(x, y)] = GLYPHS[ch]
    w.terrain_center, w.terrain_map = at, 7
    w.alive, w.health, w.max_health = True, 10, 10
    return w


def travel(x: int, y: int) -> dict:
    return {"op": "travel", "to": "point", "x": x, "y": y}


def ctx(*ops: dict, m: Memory | None = None, **policy) -> PlayContext:
    plan = Plan([dict(op) for op in ops], dict(PARAM_DEFAULTS))
    return PlayContext(
        m or Memory(), Policy(kind="scripted", **policy), random.Random(0), plan=plan, directives=default_directives()
    )


def asks(c: PlayContext) -> list[dict]:
    return [s for s in c.memory.strategist_signals if s["trigger"] == "no_way_on"]


class TravelKeepsItsWalkTest(unittest.TestCase):
    def test_on_unexplored_ground_travel_walks_on_toward_its_target(self):
        w = world(["...", "...", "..."])  # unseen ground all round
        c = ctx(travel(1, -20))
        for _ in range(4):
            out = dispatch(w, c)
            self.assertEqual(out.state, "Travel", out.reason)
            step = (out.intents[0]["x"], out.intents[0]["y"])
            self.assertLess(chebyshev(step, (1, -20)), chebyshev(w.pos, (1, -20)), "each step nearer it")
            w.pos = step
            if c.memory.path[:1] == [step]:
                c.memory.path = c.memory.path[1:]
            for dx in (-1, 0, 1):
                w.view.tiles.setdefault((step[0] + dx, step[1] - 1), "dirt")  # the step reveals the ground ahead
            w.tick += 10

    def test_with_no_way_on_it_asks_once_and_holds(self):
        w = world(POCKET)
        c = ctx(travel(5, 1))
        for _ in range(3):
            out = dispatch(w, c)
            self.assertEqual((out.state, out.intents, out.wait), ("Travel", None, True), out.reason)
            w.tick += 4
        self.assertEqual([(a["why"], a["op"]) for a in asks(c)], [(WHY, travel(5, 1))], "one ask per cell and reason")

    def test_explore_never_stands_in_at_stuck_step_two(self):
        # Step 2: Travel yields to Break; Break has no block to open here. Explore holds, never
        # walking the character off the way while step 2 runs, so the ladder goes on.
        w = world(["....", "....", "...."])
        c = ctx(travel(1, -20))
        att = nav_stuck.track(c.memory, w, "travel:point", nav_stuck.Leg((1, -20)))
        att.level = nav_stuck.BREAK
        out = dispatch(w, c)
        self.assertIn("Travel: travel:point 1,-20: no step toward it", out.yielded)
        self.assertEqual((out.state, out.intents, out.wait), ("Explore", None, True), out.reason)
        self.assertEqual(asks(c), [], "Break was due: no ask")
        # Break found nothing to open and moved the ladder on: the next decision is Travel's own walk again.
        w.tick += 10
        out = dispatch(w, c)
        self.assertEqual(out.state, "Travel", out.reason)
        self.assertTrue(out.intents, out.reason)

    def test_survival_still_acts_during_the_hold(self):
        w = world(POCKET)
        w.health, w.lives = 500, 10
        w.entities = [Entity("npc", 5, (2, 1), code="snotling")]
        w.threat.record(("npc", "snotling"), 1)
        c = ctx(travel(5, 1), on_hostile="fight", hostile=["npc"], hostile_range=2)
        c.params = {**PARAM_DEFAULTS, "risk": 1.0, "lives_floor": 1}
        out = dispatch(w, c)
        self.assertIn(out.state, ("Fight", "Flee"), out.reason)
        self.assertTrue(out.intents, out.reason)
        w.entities = []
        out = dispatch(w, c)
        self.assertEqual((out.state, out.intents, out.wait), ("Travel", None, True), "the hold resumes once it is over")


class AskDedupTest(unittest.TestCase):
    def test_a_new_reason_on_the_same_cell_is_a_new_ask(self):
        w, c = world(POCKET), ctx(travel(5, 1))
        ask_the_planner(w, c, "nothing left to explore", "Explore")
        ask_the_planner(w, c, "nothing left to explore", "Explore")
        ask_the_planner(w, c, WHY, "Travel", travel(5, 1))
        self.assertEqual([a["why"] for a in asks(c)], ["nothing left to explore", WHY])
        self.assertEqual(c.memory.no_way_on[:3], (7, (1, 1), WHY))

    def test_a_new_op_on_the_same_cell_and_reason_is_a_new_ask(self):
        w, c = world(POCKET), ctx(travel(5, 1))
        ask_the_planner(w, c, WHY, "Travel", travel(5, 1))
        ask_the_planner(w, c, WHY, "Travel", travel(5, 1))
        ask_the_planner(w, c, WHY, "Travel", travel(6, 1))
        self.assertEqual([a["op"] for a in asks(c)], [travel(5, 1), travel(6, 1)])


class NoPlannerAnswersTest(unittest.TestCase):
    def test_the_builtin_plan_explores_round_the_cell_then_travels_again(self):
        w = world(["...", "...", "..."])
        plan = Plan([travel(1, -20)], dict(PARAM_DEFAULTS))
        builtin_way_on(plan, {"trigger": "no_way_on", "x": 1, "y": 1, "why": WHY, "op": travel(1, -20)}, w)
        area = {"op": "explore_area", "x": 1, "y": 1, "radius": BUILTIN_EXPLORE_RADIUS}
        self.assertEqual(plan.goals[plan.index :], [area, travel(1, -20)])

    def test_with_the_ground_round_it_explored_it_explores_anywhere(self):
        w = world(POCKET)
        plan = Plan([travel(5, 1)], dict(PARAM_DEFAULTS))
        builtin_way_on(plan, {"trigger": "no_way_on", "x": 1, "y": 1, "why": WHY, "op": travel(5, 1)}, w)
        self.assertEqual(plan.current()["radius"], EXPLORE_ANYWHERE)

    def test_an_ask_for_an_op_no_longer_on_top_is_ignored(self):
        w = world(POCKET)
        plan = Plan([travel(3, 1)], dict(PARAM_DEFAULTS))
        builtin_way_on(plan, {"trigger": "no_way_on", "x": 1, "y": 1, "why": WHY, "op": travel(5, 1)}, w)
        self.assertEqual(plan.current(), travel(3, 1))

    def test_with_no_planner_the_strategist_answers_and_nothing_freezes(self):
        # No shop known: Travel cannot resolve its destination, so it asks and holds.
        w = world(["...", "...", "..."])
        m = Memory()
        shop = validate_goal_op({"op": "travel", "to": "shop"})
        plan = Plan([shop], dict(PARAM_DEFAULTS))
        policy = Policy(kind="scripted")
        d = decide(w, m, policy, random.Random(0), plan=plan, directives=default_directives())
        self.assertEqual((d.state, d.intent), ("Travel", None), d.reason)
        runner = SimpleNamespace(plan=plan, world=w, mem=m, log=lambda *a: None)
        strategist = Strategist.off()
        with mock.patch.object(Strategist, "_collect", lambda self, r: self.inbox.extend(m.strategist_signals)):
            strategist.on_window(runner)
        self.assertEqual(plan.current()["op"], "explore_area")
        d = decide(w, m, policy, random.Random(0), plan=plan, directives=default_directives())
        self.assertIsNotNone(d.intent, d.reason)
        self.assertEqual(d.state, "Explore")


class TraceYieldedTest(unittest.TestCase):
    def test_the_decision_carries_why_the_states_above_sent_nothing(self):
        w = world(["....", "....", "...."])
        c = ctx(travel(1, -20))
        att = nav_stuck.track(c.memory, w, "travel:point", nav_stuck.Leg((1, -20)))
        att.level = nav_stuck.BREAK
        d = decide(w, c.memory, c.policy, c.rng, plan=c.plan, directives=c.directives)
        self.assertIn("Travel: travel:point 1,-20: no step toward it", d.yielded)


class BuiltinRadiusTest(unittest.TestCase):
    def test_builtin_explore_radius_is_state_map_reach(self) -> None:
        self.assertEqual(BUILTIN_EXPLORE_RADIUS, MAP_RADIUS)


if __name__ == "__main__":
    unittest.main()
