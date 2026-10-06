"""A58 run 5 offline: the walker flipped between two equal routes (A15).

Live, the goto walk stepped back to the cell it had just left (393↔394,
then 425↔426) until the oscillation guard gave the goto up, four times in
6000 ticks, and the smoke run aborted. Nothing blocked the route it was on:
the walker re-picked its path every decision, and a route that fog had just
made a little cheaper won over the one it was walking.

Rebuilt here through the real dispatcher, Explore and planner. Two routes of
equal length go round a wall to the goto, one north and one south. The agent
starts north, and its next cell is still fog when terrain reads begin to
show the south route, one tile per decision. Re-picking each decision, the
walker steps back to its start for the cheaper-looking south route. A walker
that commits waits for its own route to be seen, then walks it.
"""

import random
import unittest

from agentrealm_agent.config import Policy
from agentrealm_agent.directives import PARAM_DEFAULTS
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.memory import Memory
from agentrealm_agent.navigation import CostGridParams, learn_step_rejection
from agentrealm_agent.navigation import stuck as nav_stuck
from agentrealm_agent.navigation import walk as nav_walk
from agentrealm_agent.navigation.walk import Walk
from agentrealm_agent.pathing import bounded_step, commit_walk, guided_step
from agentrealm_agent.states import dispatch
from agentrealm_agent.states.base import PlayContext
from agentrealm_agent.world import WorldModel

START = (0, 0)
GOTO = (6, 0)
# A wall column at x=3 between them; the way round is over its top or under
# its bottom, six steps either way.
WALL = [(3, y) for y in range(-2, 3)]
NORTH = [(1, -1), (2, -2), (3, -3), (4, -2), (5, -1), GOTO]
SOUTH = [(1, 1), (2, 2), (3, 3), (4, 2), (5, 1), GOTO]


def world() -> WorldModel:
    """Map 1, open dirt but for the wall. Seen at the start: the wall, the
    start and the first cell of each route; the rest is fog."""
    w = WorldModel(character_id=1, map_id=1, pos=START, perception=8, health=10, max_health=10)
    for p in WALL:
        w.view.tiles[p] = "stone"
    for p in (START, NORTH[0], SOUTH[0]):
        w.view.tiles[p] = "dirt"
    w.terrain_center, w.terrain_map = START, 1
    return w


def ctx() -> PlayContext:
    return PlayContext(
        Memory(),
        Policy(kind="scripted", goals=["goto"], goto=GOTO, pickup=False, hostile=[]),
        random.Random(0),
        params=dict(PARAM_DEFAULTS),
        knowledge=KnowledgeBase.empty("sandbox"),
    )


def walk(reveals: list, decisions: int = 24):
    """Decide, land the move, then let one more tile of fog show as dirt.

    Returns the context and the cell after each decision.
    """
    w, c = world(), ctx()
    cells = [w.pos]
    reveals = list(reveals)
    for _ in range(decisions):
        if w.pos == GOTO:
            break
        out = dispatch(w, c)
        w.tick += 10
        if out.intents and out.intents[0]["verb"] == "SetPosition":
            w.pos = (out.intents[0]["x"], out.intents[0]["y"])
            if c.memory.path[:1] == [w.pos]:
                c.memory.path = c.memory.path[1:]
            nav_stuck.on_step(c.memory, w)
        cells.append(w.pos)
        if reveals:
            w.view.tiles[reveals.pop(0)] = "dirt"
    return c, cells


def back_steps(cells: list) -> list:
    """Each A → B → A in the walk, waits in between left out."""
    cells = [p for i, p in enumerate(cells) if i == 0 or p != cells[i - 1]]
    return [(a, b) for a, b, again in zip(cells, cells[1:], cells[2:]) if a == again and a != b]


class Run5FlipTest(unittest.TestCase):
    def test_the_walker_waits_for_its_route_rather_than_stepping_back(self):
        """Fog shows the south route one tile at a time while the north
        route's next cell is still unseen; then the north route shows."""
        c, cells = walk(SOUTH[1:-1] + NORTH[1:])
        self.assertEqual(back_steps(cells), [], cells)
        self.assertEqual(cells[1], NORTH[0])
        self.assertEqual(cells[-1], GOTO, cells)
        self.assertTrue(set(NORTH) >= set(cells[1:]), "stays on the north route")
        self.assertEqual(c.memory.nav_stuck.oscillations, [], "the guard is never needed")

    def test_two_equal_routes_never_alternate(self):
        """Fog shows the two routes a tile each in turn, so each in turn
        looks a little cheaper: the walker keeps the route it chose."""
        interleaved = [p for pair in zip(SOUTH[1:], NORTH[1:]) for p in pair]
        c, cells = walk(interleaved)
        self.assertEqual(back_steps(cells), [], cells)
        self.assertEqual(cells[-1], GOTO, cells)
        self.assertTrue(set(NORTH) >= set(cells[1:]), "stays on the north route")

    def test_a_route_that_turns_out_walled_is_dropped_for_the_other(self):
        """Newly seen terrain that blocks the route is a reason to switch,
        a step back included."""
        w, c = world(), ctx()
        out = dispatch(w, c)
        self.assertEqual((out.intents[0]["x"], out.intents[0]["y"]), NORTH[0])
        w.pos, c.memory.path = NORTH[0], c.memory.path[1:]
        nav_stuck.on_step(c.memory, w)
        for p in [(0, -2), (1, -2), (2, -2), (2, -1), (0, -1), (1, 0)]:
            w.view.tiles[p] = "stone"
        for p in SOUTH[1:]:
            w.view.tiles[p] = "dirt"
        out = dispatch(w, c)
        self.assertEqual((out.intents[0]["x"], out.intents[0]["y"]), START, "back for the south route")

class WalkLifecycleTest(unittest.TestCase):
    """Each goal keeps its own walk, and a dropped route is never picked up again."""

    def setUp(self):
        self.w = WorldModel(character_id=1, map_id=1, pos=(1, 0), perception=8)
        for x in range(-5, 12):
            for y in range(-5, 6):
                self.w.view.tiles[(x, y)] = "dirt"
        self.m = Memory()
        self.target = (10, 0)
        self.kept = [(x, 0) for x in range(1, 11)]
        self.m.walks["explore"] = Walk("explore", 1, self.target, [(0, 0), *self.kept])
        self.back = [(0, 0), (1, 1)] + [(x, 1) for x in range(2, 10)] + [self.target]

    def test_another_goals_walk_leaves_this_ones_alone(self):
        """Explore waits on its kept path while a later goal takes the move;
        on the next decision Explore's walk is still there."""
        commit_walk(self.m, self.w, "doors", (1, 5), [(1, 1), (1, 2), (1, 3), (1, 4), (1, 5)], CostGridParams())
        path = commit_walk(self.m, self.w, "explore", self.target, self.back, CostGridParams())
        self.assertEqual(path, self.kept[1:], "explore keeps its route")
        self.assertEqual(set(self.m.walks), {"explore", "doors"})

    def test_a_given_up_route_never_comes_back(self):
        self.m.path, self.m.goal = self.kept[1:], "explore"
        att = nav_stuck.track(self.m, self.w, "explore", self.target)
        nav_stuck.give_up(self.m, self.w, att, "time")
        self.assertNotIn("explore", self.m.walks)
        other = [(2, 1)] + [(x, 1) for x in range(3, 10)] + [self.target]
        path = commit_walk(self.m, self.w, "explore", self.target, other, CostGridParams())
        self.assertEqual(path, other, "the new plan, not the given-up route")

    def test_a_given_up_route_held_by_another_path_is_dropped_too(self):
        self.m.path, self.m.goal = [(1, 1)], "doors"
        att = nav_stuck.track(self.m, self.w, "explore", self.target)
        nav_stuck.give_up(self.m, self.w, att, "pacing")
        self.assertNotIn("explore", self.m.walks)

    def test_an_opened_break_drops_the_walk_for_a_fresh_route(self):
        self.m.path, self.m.goal = self.kept[1:], "explore"
        att = nav_stuck.track(self.m, self.w, "explore", self.target)
        nav_stuck.on_break_opened(self.m, self.w, att)
        self.assertNotIn("explore", self.m.walks)

    def test_a_backed_off_guided_target_drops_its_walk(self):
        att = nav_stuck.track(self.m, self.w, "explore", self.target)
        nav_stuck.give_up(self.m, self.w, att, "time")
        self.m.walks["explore"] = Walk("explore", 1, self.target, [(0, 0), *self.kept])
        self.assertIsNone(guided_step(self.m, self.w, "explore", self.target, set(), lambda att: None))
        self.assertNotIn("explore", self.m.walks)

    def test_the_walk_prices_on_the_grid_the_planner_searched(self):
        """A cell the planner priced as costly (a hazard to escape over) on the
        kept path lets the detour in; on a bare grid the kept path would win."""
        self.m.walks["loot"] = Walk("loot", 1, self.target, [(0, 0), *self.kept])
        self.m.path, self.m.goal = [], "loot"
        detour = [(2, 1)] + [(x, 1) for x in range(3, 10)] + [self.target]
        costly = CostGridParams(costly={(5, 0)})
        step = bounded_step(self.m, self.w, "loot", self.target, set(), lambda: list(detour), params=lambda: costly)
        self.assertEqual(step, (2, 1))
        self.m.walks["loot"] = Walk("loot", 1, self.target, [(0, 0), *self.kept])
        self.m.path = []
        step = bounded_step(self.m, self.w, "loot", self.target, set(), lambda: list(detour))
        self.assertEqual(step, (2, 0), "bare grid: the kept path")


class EveryWalkCommitsTest(unittest.TestCase):
    """``guided_step`` (Travel, Recover, Level, Investigate, Boss) and
    ``bounded_step`` (Heal, Loot) keep their path the same way."""

    def setUp(self):
        self.w = world()
        self.m = Memory()
        self.back = [START] + SOUTH  # cheaper once the south route shows, but back first
        for p in SOUTH[1:]:
            self.w.view.tiles[p] = "dirt"

    def walk_north_one_step(self, goal):
        self.m.walks[goal] = nav_walk.start(self.w, goal, GOTO, NORTH)
        self.m.path, self.m.goal = NORTH[1:], goal
        self.w.pos = NORTH[0]

    def test_guided_step_waits_for_its_route(self):
        self.walk_north_one_step("travel:point")
        step = guided_step(self.m, self.w, "travel:point", GOTO, set(), lambda att: list(self.back))
        self.assertIsNone(step, "no step back")
        self.assertEqual(self.m.path, NORTH[1:])

    def test_bounded_step_waits_for_its_route(self):
        self.walk_north_one_step("loot")
        step = bounded_step(self.m, self.w, "loot", GOTO, set(), lambda: list(self.back))
        self.assertIsNone(step, "no step back")
        self.assertEqual(self.m.walks["loot"].cells, [START, *NORTH])


class CommitRulesTest(unittest.TestCase):
    """``navigation.walk.commit``, one rule at a time, on an open known map."""

    def setUp(self):
        self.w = WorldModel(character_id=1, map_id=1, pos=(0, 0), perception=8)
        for x in range(-5, 12):
            for y in range(-5, 6):
                self.w.view.tiles[(x, y)] = "dirt"
        self.target = (10, 0)
        self.kept = [(x, 0) for x in range(1, 11)]
        self.params = CostGridParams()
        self.walk = nav_walk.start(self.w, "goto", self.target, self.kept)

    def commit(self, found, goal="goto", target=None):
        return nav_walk.commit(self.walk, self.w, goal, target or self.target, found, self.params)

    def test_an_equal_path_never_replaces_the_kept_one(self):
        other = [(1, 1)] + [(x, 1) for x in range(2, 10)] + [self.target]
        path, walk = self.commit(other)
        self.assertEqual(path, self.kept)
        self.assertIs(walk, self.walk)

    def test_a_path_less_than_a_fifth_cheaper_does_not(self):
        for p in [(5, 0), (6, 0)]:
            del self.w.view.tiles[p]  # fog: 12 to walk the kept path, against 10
        path, _ = self.commit([(1, 1)] + [(x, 1) for x in range(2, 10)] + [self.target])
        self.assertEqual(path, self.kept)

    def test_a_path_more_than_a_fifth_cheaper_replaces_it(self):
        self.w.view.tiles[(5, 0)] = "fire"  # on the kept path, still passable at a price
        path, walk = self.commit([(1, 1)] + [(x, 1) for x in range(2, 10)] + [self.target])
        self.assertEqual(path[0], (1, 1))
        self.assertEqual(walk.cells[0], (0, 0))

    def test_a_blocked_path_is_dropped(self):
        self.w.view.tiles[(5, 0)] = "stone"
        other = [(1, 1)] + [(x, 1) for x in range(2, 10)] + [self.target]
        self.assertEqual(self.commit(other)[0], other)

    def test_a_new_target_or_goal_takes_the_new_path(self):
        other = [(1, 1), (2, 2)]
        self.assertEqual(self.commit(other, target=(2, 2))[0], other)
        self.assertEqual(self.commit(other, goal="explore")[0], other)

    def test_a_step_back_is_taken_only_once_the_kept_path_is_blocked(self):
        self.w.pos = (1, 0)
        self.w.view.tiles[(5, 0)] = "fire"
        back = [(0, 0), (1, 1)] + [(x, 1) for x in range(2, 10)] + [self.target]
        self.assertEqual(self.commit(back)[0], self.kept[1:], "cheaper, but steps back")
        self.w.view.tiles[(5, 0)] = "stone"
        self.assertEqual(self.commit(back)[0], back, "blocked: back it is")

    def test_a_walked_path_lets_the_next_plan_in(self):
        """A window path that ends short of its target (A13) is walked; the next one is taken as it comes."""
        self.walk = nav_walk.start(self.w, "goto", self.target, self.kept[:5])
        self.w.pos = self.kept[4]
        nxt = [(4, 1)] + [(x, 1) for x in range(5, 10)] + [self.target]
        self.assertEqual(self.commit(nxt)[0], nxt)

    def test_a_rejected_step_ends_the_walk(self):
        m = Memory()
        m.walks["goto"], m.path, m.goal = self.walk, list(self.kept), "goto"
        learn_step_rejection(m, self.w, None, (1, 0), "block_occupied", 0)
        self.assertEqual(m.walks, {})


class ExploreKeepsItsFrontierTest(unittest.TestCase):
    """The run 5 trigger on the most-used walk: a reveal makes the other
    side's frontier the nearest one. Explore keeps heading for the ground it
    was exploring and turns round only once that side has none left."""

    WEST_END, EAST_END = -10, 10

    def world(self) -> WorldModel:
        """A dirt corridor along y=0, its walls seen end to end, only x in -2..2
        of the floor seen: two frontiers, (-2, 0) and (2, 0)."""
        w = WorldModel(character_id=1, map_id=1, pos=(0, 0), perception=8, health=10, max_health=10)
        for x in range(self.WEST_END - 2, self.EAST_END + 3):
            w.view.tiles[(x, -1)] = w.view.tiles[(x, 1)] = "stone"
        for x in range(-2, 3):
            w.view.tiles[(x, 0)] = "dirt"
        w.terrain_center, w.terrain_map = w.pos, 1
        return w

    def floor(self, x: int) -> str:
        return "dirt" if self.WEST_END <= x <= self.EAST_END else "stone"

    def test_a_reveal_ahead_never_turns_explore_round(self):
        """Each move shows three more floor tiles ahead, so the frontier ahead
        recedes faster than the agent walks and the one behind becomes the
        nearest. Explore still walks west until it has seen the west end, and
        turns east only then."""
        w = self.world()
        c = PlayContext(
            Memory(),
            Policy(kind="scripted", goals=["explore"], pickup=False, hostile=[]),
            random.Random(0),
            params=dict(PARAM_DEFAULTS),
            knowledge=KnowledgeBase.empty("sandbox"),
        )
        cells = [w.pos]
        seen_when_turned = None
        for _ in range(30):
            before = w.pos
            out = dispatch(w, c)
            w.tick += 10
            if not (out.intents and out.intents[0]["verb"] == "SetPosition"):
                break
            w.pos = (out.intents[0]["x"], out.intents[0]["y"])
            if c.memory.path[:1] == [w.pos]:
                c.memory.path = c.memory.path[1:]
            nav_stuck.on_step(c.memory, w)
            cells.append(w.pos)
            if w.pos[0] > before[0] and seen_when_turned is None:
                seen_when_turned = set(w.view.tiles)
            ahead = -1 if w.pos[0] < before[0] else 1
            seen = [x for (x, y) in w.view.tiles if y == 0]
            edge = min(seen) if ahead < 0 else max(seen)
            for x in range(edge + ahead, edge + 4 * ahead, ahead):
                w.view.tiles[(x, 0)] = self.floor(x)
        xs = [x for x, _ in cells]
        turn = xs.index(min(xs))
        self.assertEqual(xs[: turn + 1], list(range(0, -turn - 1, -1)), f"straight west first: {cells}")
        self.assertEqual(xs[turn:], list(range(xs[turn], xs[-1] + 1)), f"then straight east: {cells}")
        self.assertIn((self.WEST_END - 1, 0), seen_when_turned, "turns only once the west end is seen")
        self.assertEqual(c.memory.nav_stuck.oscillations, [], "the guard is never needed")


if __name__ == "__main__":
    unittest.main()
