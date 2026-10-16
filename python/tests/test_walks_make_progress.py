"""A125: a walk on an op's behalf makes net progress toward its target.

- A detour is taken only when its gain beats its cost, and once taken it is
  kept until it reaches its find or a concrete reason drops it, never for
  the time it takes: repeated detours to one cluster of gem piles complete.
- Each leg brings the target nearer by what the walk has learned: a corridor
  searched again keeps the window search's learning, so no leg leads back
  over ground the walk learned leads nowhere.
- Each level of a walk's stuck ladder must beat the walk's best plan
  measure, so a fresh level never forgives standing no nearer; a route round
  an obstacle lowers the measure, so it is progress. Once the whole ladder
  gives up, the executor asks the planner, saying what each level tried.
"""

from __future__ import annotations

import random
import unittest

from agentrealm_adk.config import Policy
from agentrealm_adk.directives import PARAM_DEFAULTS, default_directives
from agentrealm_adk.memory import Memory
from agentrealm_adk.navigation import CostGridParams, NavSearchState, cost_path
from agentrealm_adk.navigation import stuck as nav_stuck
from agentrealm_adk.pathing import guided_step
from agentrealm_agent.plan import Plan
from agentrealm_agent.states import PlayContext, dispatch
from agentrealm_adk.world import Entity, WorldModel

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


class NetProgressTest(unittest.TestCase):
    TARGET = (200, 1)

    def test_a_route_round_a_wall_is_progress_and_arrives(self):
        # The way to the goal runs 36 cells east, away from it in a straight
        # line, through a gap and back: the plan's measure falls every step.
        width, gap, goal = 42, 39, (2, 2)
        w = field(width=width, height=13, at=(2, 10))
        for x in range(1, width - 1):
            if x != gap:
                w.view.tiles[(x, 6)] = "wall"
        c = ctx(Memory(), travel(*goal))
        for _ in range(120):
            out = dispatch(w, c)
            if c.plan.current() is None:
                break
            self.assertEqual(out.state, "Travel", out.reason)
            self.assertTrue(out.intents, out.reason)
            w.pos = (out.intents[0]["x"], out.intents[0]["y"])
            if c.memory.path[:1] == [w.pos]:
                c.memory.path = c.memory.path[1:]
            nav_stuck.on_step(c.memory, w)
            w.tick += 10
        self.assertEqual(w.pos, goal)
        self.assertEqual(c.memory.nav_stuck.stuck_signals, [], "never stuck")
        asks = [s for s in c.memory.strategist_signals if s["trigger"] == "no_way_on" and s.get("op")]
        self.assertEqual(asks, [], "the walk never asked")

    def wander(self, m: Memory, w: WorldModel, levels: list[int]) -> None:
        """Walk a plan that only ever steps sideways, until the walk is given up."""

        def sideways(_att) -> list:
            return [(w.pos[0], 0 if w.pos[1] else 2)]

        for _ in range(1000):
            step = guided_step(m, w, "travel:point", self.TARGET, set(), sideways)
            att = m.nav_stuck.attempts.get(nav_stuck.goal_key("travel:point", MAP, self.TARGET))
            if att is None:
                return
            levels.append(att.level)
            if step is not None:
                w.pos = step
                nav_stuck.on_step(m, w)
            w.tick += 10
        self.fail("never given up")

    def test_no_net_progress_runs_the_whole_ladder_then_gives_up(self):
        # Each level must beat the walk's best measure, so none forgives the
        # wandering; and none is cut short: the ladder reaches ALT_ROUTE first.
        w = WorldModel(character_id=1, map_id=MAP, pos=(0, 1), perception=40)
        w.view.tiles.update({(x, y): "dirt" for x in range(60) for y in range(3)})
        m, levels = Memory(), []
        self.wander(m, w, levels)
        for level in (nav_stuck.CAUTIOUS, nav_stuck.REVEAL):
            self.assertIn(level, levels)
        (signal,) = m.nav_stuck.stuck_signals
        # WALK, CAUTIOUS, BREAK (nothing to open), REVEAL, then ALT_ROUTE: each level failed in turn.
        self.assertEqual(signal["escalation"], ["oscillation", "time", "no_break", "reveal_spent", "no_path"])
        self.assertTrue(nav_stuck.backed_off(m, "travel:point", MAP, self.TARGET, w.tick))

    def test_a_fresh_level_does_not_forgive_standing_no_nearer(self):
        w = WorldModel(character_id=1, map_id=MAP, pos=(0, 1), perception=40)
        w.view.tiles.update({(x, y): "dirt" for x in range(60) for y in range(3)})
        m = Memory()
        att = nav_stuck.track(m, w, "travel:point", self.TARGET)
        nav_stuck.observe(att, w, [(x, 1) for x in range(1, 11)])  # measure 10 + 190
        self.assertTrue(nav_stuck.escalate(m, w, att, "moves"))  # CAUTIOUS, a fresh window
        att.moves = 5
        nav_stuck.observe(att, w, [(0, 0)] * 20)  # a leg that leaves the target no nearer
        self.assertEqual(att.moves, 5, "no progress: the window runs on")
        w.pos = (3, 1)
        nav_stuck.observe(att, w, [(x, 1) for x in range(4, 11)])  # nearer than ever
        self.assertEqual(att.moves, 0)

    def corridor_round(self, target) -> NavSearchState:
        """A corridor that goes round an obstacle wider than the window: north
        one tile, east three, then south to the target's tile, far south."""
        tiles = [(0, 0), (0, -1), (1, -1), (2, -1), (3, -1), *((3, y) for y in range(0, 13)), (2, 12), (1, 12), (0, 12)]
        nav = NavSearchState(goal=target, map_id=MAP)
        nav.came = dict(zip(tiles, tiles[1:]))
        return nav

    def walk_round(self, corridor: NavSearchState | None) -> list[str]:
        """Walk the corridor's first two legs (north, then east: away from the
        target in a straight line), one decision per step; the windows that failed."""
        target = (8, 200)
        w = WorldModel(character_id=1, map_id=MAP, pos=(8, 8), perception=12)
        m = Memory()
        att = nav_stuck.track(m, w, "travel:point", target)
        m.goal = "travel:point"
        legs = [[(8, y) for y in range(7, -9, -1)], [(x, -8) for x in range(9, 57)]]
        failed = []
        for leg in legs:
            for i, cell in enumerate(leg):
                nav_stuck.observe(att, w, leg[i:], corridor)
                if reason := nav_stuck.stuck_reason(att, w.tick):
                    failed.append(reason)
                w.pos = cell
                nav_stuck.on_step(m, w)
                w.tick += 4
        return failed

    def test_a_route_round_an_obstacle_wider_than_the_window_is_progress(self):
        target = (8, 200)
        self.assertEqual(self.walk_round(self.corridor_round(target)), [], "the corridor's way round is progress")
        self.assertIn("moves", self.walk_round(None), "a straight-line rest would call it stuck")

    def test_after_the_ladder_gives_up_the_executor_asks_with_what_it_tried(self):
        w = field(width=30, height=6, at=(2, 2))
        c = ctx(Memory(), travel(20, 2))
        att = nav_stuck.track(c.memory, w, "travel:point", (20, 2))
        att.reasons = ["moves", "moves", "no_known_opener", "reveal_spent"]
        nav_stuck.give_up(c.memory, w, att, "moves")
        out = dispatch(w, c)
        self.assertEqual((out.state, out.intents, out.wait), ("Travel", None, True), out.reason)
        (ask,) = [s for s in c.memory.strategist_signals if s["trigger"] == "no_way_on"]
        self.assertTrue(
            ask["tried"].startswith("the walk, given up (moves, moves, no_known_opener, reveal_spent, moves)"),
            ask["tried"],
        )


if __name__ == "__main__":
    unittest.main()
