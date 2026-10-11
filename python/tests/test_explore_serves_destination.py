"""A120: a walk on the top op's behalf serves the op's destination.

When Travel has no step this decision, the safe default (Explore, last in
dispatch) runs for the travel op: it explores only a frontier that brings
the destination nearer, and with none it asks the planner, saying why,
rather than wander. Explore is run here as dispatch runs it once Travel has
handed over; ``test_states`` covers the hand-over itself.
"""

from __future__ import annotations

import random
import unittest

from agentrealm_agent.config import Policy
from agentrealm_agent.directives import PARAM_DEFAULTS, default_directives
from agentrealm_agent.memory import SAFE_EXPLORE_GOAL, Memory
from agentrealm_agent.plan import Plan
from agentrealm_agent.states import PlayContext
from agentrealm_agent.states.explore import ExploreState
from agentrealm_agent.world import WorldModel, chebyshev

DEST = (12, 2)
TRAVEL = {"op": "travel", "to": "point", "x": DEST[0], "y": DEST[1]}


def field(east_open: bool = True, at=(2, 2)) -> WorldModel:
    """Seen dirt from x=1 to 7, y=0 to 4, unseen ground round it, the
    destination off to the east. The nearest frontier by walk is the west
    edge, away from it; with ``east_open`` false a wall closes off every side
    but the west, so no frontier brings it nearer."""
    w = WorldModel(character_id=1, map_id=7, pos=at, perception=3)
    for x in range(1, 8):
        for y in range(5):
            w.view.tiles[(x, y)] = "dirt"
    if not east_open:
        for x in range(0, 9):
            w.view.tiles[(x, -1)] = w.view.tiles[(x, 5)] = "wall"
        for y in range(-1, 6):
            w.view.tiles[(8, y)] = "wall"
    w.terrain_center, w.terrain_map = at, 7
    w.alive, w.health, w.max_health = True, 10, 10
    return w


def ctx(m: Memory, *ops: dict) -> PlayContext:
    plan = Plan([dict(op) for op in ops], dict(PARAM_DEFAULTS)) if ops else None
    return PlayContext(m, Policy(kind="scripted"), random.Random(0), plan=plan, directives=default_directives())


def explore(w: WorldModel, c: PlayContext):
    return ExploreState().act(w, c)


class ExploreServesTravelTest(unittest.TestCase):
    def test_with_frontiers_both_ways_it_picks_one_toward_the_destination(self):
        w, m = field(), Memory()
        out = explore(w, ctx(m, TRAVEL))
        self.assertTrue(out.intents, out.reason)
        self.assertGreaterEqual(out.intents[0]["x"], w.pos[0], "never west, away from the destination")
        self.assertEqual(m.goal, SAFE_EXPLORE_GOAL)
        self.assertLess(chebyshev(m.path[-1], DEST), chebyshev(w.pos, DEST), m.path)

    def test_with_no_op_it_takes_the_nearest_frontier(self):
        # The control: with nothing to serve, the west edge is nearest.
        w, m = field(), Memory()
        out = explore(w, ctx(m))
        self.assertTrue(out.intents, out.reason)
        self.assertEqual(out.intents[0]["x"], 1)

    def test_a_walk_picked_for_no_op_is_not_kept_for_the_travel(self):
        w, m = field(), Memory()
        explore(w, ctx(m))  # the safe default heads west
        self.assertEqual(m.path[-1][0], 1)
        out = explore(w, ctx(m, TRAVEL))
        self.assertTrue(out.intents, out.reason)
        self.assertLess(chebyshev(m.path[-1], DEST), chebyshev(w.pos, DEST), m.path)

    def test_with_no_frontier_toward_it_the_planner_is_asked(self):
        w, m = field(east_open=False), Memory()
        c = ctx(m, TRAVEL)
        for _ in range(3):
            out = explore(w, c)
            self.assertEqual((out.state, out.intents, out.wait), ("Explore", None, True), out.reason)
            w.tick += 4
        why = f"no frontier brings travel:point {DEST[0]},{DEST[1]} nearer"
        self.assertEqual(out.reason, f"{why}: asked the planner")
        asks = [s for s in m.strategist_signals if s["trigger"] == "no_way_on"]
        self.assertEqual([s["why"] for s in asks], [why], "one ask per cell")


if __name__ == "__main__":
    unittest.main()
