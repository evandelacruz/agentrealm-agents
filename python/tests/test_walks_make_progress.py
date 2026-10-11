"""A125: a walk on an op's behalf makes net progress toward its target.

- A detour is taken only when its gain beats its cost, and once taken it is
  kept until it reaches its find or a concrete reason drops it, never for
  the time it takes: repeated detours to one cluster of gem piles complete.
- Each leg brings the target nearer by what the walk has learned: a corridor
  searched again keeps the window search's learning, so no leg leads back
  over ground the walk learned leads nowhere.
- A walk that stands no nearer over ``NO_NEARER_MOVES`` moves is given up
  whatever its ladder level, and the executor asks the planner, saying what
  was tried.
"""

from __future__ import annotations

import random
import unittest

from agentrealm_agent.config import Policy
from agentrealm_agent.directives import PARAM_DEFAULTS, default_directives
from agentrealm_agent.memory import Memory
from agentrealm_agent.navigation import CostGridParams, NavSearchState, cost_path
from agentrealm_agent.navigation import stuck as nav_stuck
from agentrealm_agent.plan import Plan
from agentrealm_agent.states import PlayContext, dispatch
from agentrealm_agent.world import Entity, WorldModel, chebyshev

MAP = 7


def field(width: int = 30, height: int = 16, at=(2, 4)) -> WorldModel:
    """Open dirt walled in."""
    w = WorldModel(character_id=1, map_id=MAP, pos=at, perception=14)
    for x in range(width):
        for y in range(height):
            w.view.tiles[(x, y)] = "wall" if x in (0, width - 1) or y in (0, height - 1) else "dirt"
    w.terrain_center, w.terrain_map = at, MAP
    w.alive, w.health, w.max_health = True, 100, 100
    w.attack_range, w.gems = 1, 0
    return w


def ctx(m: Memory, *ops: dict, **policy) -> PlayContext:
    plan = Plan([dict(op) for op in ops], dict(PARAM_DEFAULTS))
    kw = {"on_hostile": "ignore", **policy}
    return PlayContext(m, Policy(kind="scripted", **kw), random.Random(0), plan=plan, directives=default_directives())


def travel(x: int, y: int) -> dict:
    return {"op": "travel", "to": "point", "x": x, "y": y}


class DetoursCompleteTest(unittest.TestCase):
    def play(self, w: WorldModel, m: Memory, op: dict, decisions: int, ticks: int) -> list:
        outs = []
        for _ in range(decisions):
            out = dispatch(w, ctx(m, op, pickup=True))
            outs.append(out)
            for i in out.intents or []:
                if i["verb"] == "SetPosition":
                    w.pos = (i["x"], i["y"])
                    if m.path[:1] == [w.pos]:
                        m.path = m.path[1:]
                    nav_stuck.on_step(m, w)
                elif i["verb"] == "Take":
                    w.entities = [e for e in w.entities if e.id != i["supply_id"]]
                    w.gems += 1
            w.tick += ticks
        return outs

    def test_repeated_detours_to_one_cluster_complete(self):
        # A cluster of three piles well off the walk: each detour takes many
        # decisions, far apart, and every pile is taken; none is given up.
        w = field()
        m = Memory()
        op = travel(27, 4)
        self.play(w, m, op, 1, 150)
        w.entities = [Entity("supply", 70 + i, (8 + i, 10), "gem") for i in range(3)]
        outs = self.play(w, m, op, 40, 150)
        self.assertEqual(outs[0].state, "Detour", outs[0].reason)
        self.assertEqual(w.gems, 3, [o.reason for o in outs])
        self.assertEqual(m.detour_skipped, set(), "no detour given up")
        self.assertEqual(w.pos, (27, 4), "the walk resumed and arrived")

    def test_a_detour_whose_find_is_gone_is_dropped(self):
        w = field()
        m = Memory()
        op = travel(27, 4)
        self.play(w, m, op, 1, 150)
        w.entities = [Entity("supply", 70, (8, 10), "gem")]
        self.assertEqual(self.play(w, m, op, 1, 150)[0].state, "Detour")
        w.entities = []  # someone else took it
        out = self.play(w, m, op, 1, 150)[0]
        self.assertEqual(out.state, "Travel", out.reason)
        self.assertIsNone(m.detour)


class LegsBringTheGoalNearerTest(unittest.TestCase):
    """A corridor that leads into a pocket: the walk learns the pocket is dear
    and leaves it. A corridor searched again (a reveal made a tile on it
    dearer) keeps that learning, so no later leg leads back into the pocket."""

    GOAL = (100, 8)

    def pocket(self) -> WorldModel:
        w = WorldModel(character_id=1, map_id=MAP, pos=(8, 8), perception=3)
        for x in range(32):
            for y in range(32):
                wall = (16 <= x and y < 16) or (y == 9 and x < 16) or (x == 7 and y < 16)
                w.view.tiles[(x, y)] = "wall" if wall else "dirt"
        return w

    def test_legs_that_do_not_bring_the_goal_nearer_are_not_chosen(self):
        w = self.pocket()
        nav, trail = NavSearchState(goal=self.GOAL, map_id=MAP), [w.pos]
        for _ in range(100):
            nav.reset(self.GOAL)  # the corridor is searched again every decision
            path = cost_path(w, self.GOAL, CostGridParams(), nav=nav)
            self.assertTrue(path, f"no path at {w.pos}")
            trail += path
            w.pos = path[-1]
            if w.pos == self.GOAL:
                break
        self.assertEqual(w.pos, self.GOAL)
        self.assertEqual(len(trail), len(set(trail)), "no leg leads back over ground already walked")

    def test_a_new_goal_starts_its_learning_afresh(self):
        nav = NavSearchState(goal=self.GOAL)
        nav.learned = {(1, 1): 50}
        nav.reset(self.GOAL)
        self.assertEqual(nav.learned, {(1, 1): 50})
        nav.reset((0, 0))
        self.assertEqual(nav.learned, {})


class NoNetProgressAsksTest(unittest.TestCase):
    GOAL = (1, -20)

    def walk(self) -> tuple[WorldModel, PlayContext]:
        w = WorldModel(character_id=1, map_id=MAP, pos=(1, 1), perception=3)
        w.view.tiles.update({(x, y): "dirt" for x in range(-3, 6) for y in range(3)})  # unseen ground beyond
        w.terrain_center, w.terrain_map = w.pos, MAP
        w.alive, w.health, w.max_health = True, 10, 10
        c = ctx(Memory(), travel(*self.GOAL))
        out = dispatch(w, c)
        self.assertEqual(out.state, "Travel", out.reason)
        self.assertTrue(out.intents, out.reason)
        return w, c

    def wander(self, w: WorldModel, c: PlayContext, moves: int) -> None:
        """The walk's own moves, none of them nearer the goal."""
        cells = [(0, 1), (1, 1), (2, 1), (1, 1)]
        for i in range(moves):
            w.pos = cells[i % len(cells)]
            nav_stuck.on_step(c.memory, w)
            w.tick += 4

    def asks(self, c: PlayContext) -> list[dict]:
        return [s for s in c.memory.strategist_signals if s["trigger"] == "no_way_on"]

    def test_no_net_progress_over_the_window_asks_the_planner(self):
        w, c = self.walk()
        self.wander(w, c, nav_stuck.NO_NEARER_MOVES)
        out = dispatch(w, c)
        self.assertEqual((out.state, out.intents, out.wait), ("Travel", None, True), out.reason)
        self.assertTrue(out.reason.endswith("asked the planner"), out.reason)
        (ask,) = self.asks(c)
        self.assertEqual(ask["op"], travel(*self.GOAL))
        self.assertTrue(ask["tried"].startswith("the walk, given up (no_nearer)"), ask["tried"])
        (signal,) = c.memory.nav_stuck.stuck_signals
        self.assertEqual(signal["escalation"], ["no_nearer"])
        out = dispatch(w, c)
        self.assertEqual((out.state, out.intents, out.wait), ("Travel", None, True), "it holds; no wandering")
        self.assertEqual(len(self.asks(c)), 1, "one ask")

    def test_a_walk_that_gets_nearer_goes_on(self):
        w, c = self.walk()
        self.wander(w, c, nav_stuck.NO_NEARER_MOVES - 1)
        w.pos = (1, 0)  # nearer: the window starts again
        nav_stuck.on_step(c.memory, w)
        self.wander(w, c, nav_stuck.NO_NEARER_MOVES - 1)
        out = dispatch(w, c)
        self.assertEqual(out.state, "Travel", out.reason)
        self.assertTrue(out.intents, out.reason)
        self.assertEqual(self.asks(c), [])

    def test_the_window_spans_ladder_levels(self):
        # A fresh level window does not forgive the walk for standing no nearer.
        w, c = self.walk()
        att = nav_stuck.active(c.memory, w)
        self.wander(w, c, nav_stuck.NO_NEARER_MOVES // 2)
        att.level = nav_stuck.CAUTIOUS
        nav_stuck._fresh_window(att, w.tick)
        self.wander(w, c, nav_stuck.NO_NEARER_MOVES // 2)
        dispatch(w, c)
        self.assertEqual(len(self.asks(c)), 1)
        self.assertGreater(chebyshev(w.pos, self.GOAL), 20)


if __name__ == "__main__":
    unittest.main()
