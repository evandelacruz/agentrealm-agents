"""A67 free-play run 6 offline: Park's dead end, and the walk queued before the first plan.

1. Park timed out (60 s at (383, 369)): the corridor branch of ``cost_path``
   led two cells south-west into a pocket, the window search found no cell
   better than that one, and ``retreat_step`` sent nothing, marked nothing,
   and planned the same nothing at every decision. The planner now never
   ends a partial path in a pocket it can see all round, and learns its way
   out of a dead end; a safe cell with no step is ruled out, and a park
   with every safe cell ruled out ends.
2. Before the first plan landed, the safe default's ten-step walk queue
   took the hurt character into a hostile. The first plan cuts that queue
   at its next step, and hurt with a known hostile near, the safe default
   steps away or holds instead of exploring.
"""

from __future__ import annotations

import random
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agentrealm_agent import config, runner
from agentrealm_agent.brain import walkable_prefix
from agentrealm_agent.config import CharacterConfig, Policy
from agentrealm_agent.directives import PARAM_DEFAULTS
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.memory import Memory
from agentrealm_agent.navigation.stuck import PROGRESS_TICK_LIMIT
from agentrealm_agent.navigation import planner
from agentrealm_agent.navigation.planner import CostGridParams, NavSearchState, cost_path
from agentrealm_agent.park import PARK_NO_PATH
from agentrealm_agent.plan import Plan
from agentrealm_agent.states import PlayContext, dispatch
from agentrealm_agent.pathing import SAFE_UNREACHABLE_TICKS, retreat_safe_goal
from agentrealm_agent.states.explore import KEEP_AWAY_HOLD_TICKS
from agentrealm_agent.states.retreat import retreat_step
from agentrealm_agent.travel import sync_town
from agentrealm_agent.world import Entity, Pos, WorldModel
from agentrealm_agent.zone_discovery import ZoneFact
from tests.test_park import WalkServer
from tests.test_strategist import FakeLLM, fake_runner, make, round_trip

MAP = 7
START = (383, 369)  # where run 6's park stood for 60 s
TOWN = (330, 420)  # out of sight to the south-west: the corridor branch plans the walk
PERCEPTION = 4


def segments(*segs: tuple[int, int, int, bool]) -> set:
    return {(x + k, y) if horizontal else (x, y + k) for x, y, length, horizontal in segs for k in range(length)}


# The pocket two cells south-west of the start: a wall above it, a wall
# west of it, and a long wall south-west that only a walk south rounds.
WALLS = segments((381, 366, 5, False), (380, 371, 7, False), (379, 368, 7, False), (374, 371, 7, True))
POCKET = (381, 371)


def reveal(w: WorldModel) -> None:
    """A terrain read of the perception window; past it is fog."""
    x0, y0 = w.pos
    for x in range(x0 - PERCEPTION, x0 + PERCEPTION + 1):
        for y in range(y0 - PERCEPTION, y0 + PERCEPTION + 1):
            w.view.tiles[(x, y)] = "wall" if (x, y) in WALLS else "dirt"
    w.terrain_center, w.terrain_map = w.pos, MAP


def field(at: Pos = START) -> WorldModel:
    w = WorldModel(character_id=1, map_id=MAP, pos=at, perception=PERCEPTION, health=10, max_health=10)
    reveal(w)
    return w


def town_kb() -> KnowledgeBase:
    kb = KnowledgeBase.empty("sandbox")
    sync_town(kb, {"map_id": MAP, "x": TOWN[0], "y": TOWN[1]})
    return kb


class PlannerLeavesNoPocketTest(unittest.TestCase):
    """The corridor branch neither walks into the pocket nor stands in it (A13)."""

    def test_no_partial_path_ends_in_the_pocket(self):
        path = cost_path(field(), TOWN, CostGridParams(), nav=NavSearchState(goal=TOWN, map_id=MAP))
        self.assertTrue(path)
        self.assertNotEqual(path[-1], POCKET, "a dead end the search saw all round is no end")

    def test_from_inside_the_pocket_there_is_still_a_step(self):
        # Where run 6's park stood still: the old window search had no cell better than this one.
        w = field(POCKET)
        self.assertTrue(cost_path(w, TOWN, CostGridParams(), nav=NavSearchState(goal=TOWN, map_id=MAP)))

    def test_the_walk_reaches_town(self):
        w, nav = field(), NavSearchState(goal=TOWN, map_id=MAP)
        for _ in range(60):
            path = cost_path(w, TOWN, CostGridParams(), nav=nav)
            self.assertTrue(path, f"no path at {w.pos}")
            for cell in path[:10]:
                w.pos = cell
            reveal(w)
            if w.pos == TOWN:
                break
        self.assertEqual(w.pos, TOWN)


class ParkWalksOutTest(unittest.TestCase):
    """Run 6's park, through the dispatcher: Park walks to town (A66)."""

    def test_park_reaches_town_from_the_run_6_start(self):
        w, m = field(), Memory()
        m.parking = True
        c = PlayContext(m, Policy(kind="scripted", goals=[]), random.Random(0), knowledge=town_kb())
        for _ in range(80):
            out = dispatch(w, c)
            if w.pos == TOWN:
                break
            self.assertEqual(out.state, "Park")
            self.assertTrue(out.intents, f"Park sent nothing at {w.pos}: {out.reason}")
            step = (out.intents[0]["x"], out.intents[0]["y"])
            prefix = walkable_prefix(w, m, c.policy, m.path)
            cells = prefix[:10] if prefix[:1] == [step] else [step]
            m.path = m.path[len(cells) :] if prefix[:1] == [step] else m.path
            for cell in cells:
                w.pos, w.tick = cell, w.tick + 4
            reveal(w)
        self.assertEqual(w.pos, TOWN)


def walled_in():
    """No path, proven from the goal's side (``no_way``): the goal is walled in."""
    return mock.patch.multiple(
        "agentrealm_agent.states.retreat", cost_path=mock.Mock(return_value=None), no_way=mock.Mock(return_value=True)
    )


class NoStepRulesTheCellOutTest(unittest.TestCase):
    """A safe cell with no path is ruled out, never planned again at once."""

    def stuck(self):
        w, m = field(), Memory()
        m.parking = True
        w.zones[MAP] = {(390, 369): ZoneFact(safe=True)}
        c = PlayContext(m, Policy(kind="scripted", goals=[]), random.Random(0), knowledge=town_kb())
        return w, c

    def test_the_nearest_safe_cell_is_ruled_out_and_the_next_is_taken(self):
        w, c = self.stuck()
        with walled_in():
            out = retreat_step(w, c, "Park")
        self.assertIsNone(out.intents)
        self.assertEqual(out.reason, "safe (390, 369): no path, ruled out")
        self.assertIn((MAP, (390, 369)), c.memory.safe_unreachable)
        w.tick += 1
        out = retreat_step(w, c, "Park")
        self.assertEqual(out.reason, f"retreat → safe {TOWN}", "the next decision walks to the next one")

    def test_a_cell_blocked_only_by_someone_standing_there_is_not_ruled_out(self):
        # Occupants are priced (``OCCUPANT``), never impassable: a crowd round
        # us leaves a path whose first cell is taken, not "no path", so it
        # waits and rules nothing out (review on #165).
        w, c = self.stuck()
        x, y = w.pos
        w.entities = [Entity("npc", 20 + i, (x + dx, y + dy), code="townsfolk") for i, (dx, dy) in enumerate(
            [(-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0), (-1, 1), (0, 1), (1, 1)])]
        out = retreat_step(w, c, "Park")
        self.assertEqual((out.intents, out.reason), (None, "safe (390, 369): next step not open"))
        self.assertEqual(c.memory.safe_unreachable, {})
        w.entities = []  # they walk on
        self.assertTrue(retreat_step(w, c, "Park").intents)

    def test_a_ruled_out_cell_is_tried_again_once_the_mark_lapses(self):
        w, c = self.stuck()
        with walled_in():
            retreat_step(w, c, "Park")
        w.tick += SAFE_UNREACHABLE_TICKS
        pick = retreat_safe_goal(c.memory, w, c.policy, c.knowledge, set(), set())
        self.assertEqual(pick, (390, 369))

    def test_no_step_found_is_not_no_path(self):
        # A wall across the corridor band in the window, with a way round
        # outside it: the budgeted window search finds no step, which proves
        # nothing (review on #165). Town is not ruled out at once; only a whole
        # stuck window with no step to it rules it out.
        w, c = self.stuck()
        c.memory.safe_unreachable[(MAP, (390, 369))] = w.tick  # only town is left: far, out of sight
        with mock.patch.object(planner, "_fine_path", return_value=None):
            out = retreat_step(w, c, "Park")
            self.assertEqual(out.reason, f"safe {TOWN}: no step found")
            self.assertNotIn((MAP, TOWN), c.memory.safe_unreachable)
            w.tick += PROGRESS_TICK_LIMIT
            retreat_step(w, c, "Park")
        self.assertIn((MAP, TOWN), c.memory.safe_unreachable, "a stuck window with no step rules town out")

    def test_a_one_decision_block_round_us_rules_nothing_out(self):
        # Shut in for this decision only (learned rejections round us): the
        # search from our side runs out of cells, which proves nothing about
        # the goal (review on #165).
        w, c = self.stuck()
        x, y = w.pos
        shut = {(x + dx, y + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)} - {w.pos}
        blocked = (shut, set(shut), set())
        with mock.patch("agentrealm_agent.states.retreat.plan_sets", return_value=blocked):
            out = retreat_step(w, c, "Park")
        self.assertEqual(out.reason, f"safe {TOWN}: no step found")
        self.assertNotIn((MAP, TOWN), c.memory.safe_unreachable)

    def test_the_town_cell_is_ruled_out_too(self):
        w, c = self.stuck()
        c.memory.safe_unreachable[(MAP, (390, 369))] = w.tick
        with walled_in():
            out = retreat_step(w, c, "Park")
        self.assertEqual(out.reason, f"safe {TOWN}: no path, ruled out")
        self.assertIn((MAP, TOWN), c.memory.safe_unreachable)


class ParkEndsWithNoPathLeftTest(unittest.TestCase):
    """With every safe cell ruled out, the park ends at once and clears the queue."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.object(config, "STATE_DIR", Path(tmp.name))
        patch.start()
        self.addCleanup(patch.stop)

    def test_the_park_ends_instead_of_standing_out_its_minute(self):
        s = WalkServer(pos=(0, 0))
        cfg = CharacterConfig("T", "sandbox", Policy(goals=[], entity_refresh=1000), Path("t.toml"))
        r = runner.Runner(cfg, s, 1, threading.Event(), out=lambda _: None, park_seconds=60.0)
        self.addCleanup(r.trace.close)
        w = WorldModel(character_id=1, map_id=MAP, pos=(0, 0), perception=25, tick=100)
        for x in range(-3, 12):
            for y in range(-3, 4):
                w.view.tiles[(x, y)] = "dirt"
        w.zones[MAP] = {(6, 0): ZoneFact(safe=True)}
        w.terrain_center, w.terrain_map, w.entities_tick = (0, 0), MAP, 100
        r.world, r.mem = w, Memory(need_self=False, need_position=False)
        r.pacer.wait_next_window = s.wait
        now = iter(range(1000))
        r.clock = lambda: float(next(now))
        with walled_in():
            report = r.park()
        self.assertEqual(report.outcome, PARK_NO_PATH)
        self.assertLess(report.seconds, 10, "not the whole minute")
        self.assertEqual(s.sent[-1], [], "the queue is still cleared")


def hurt_beside(hostile_at) -> tuple[WorldModel, PlayContext]:
    w = WorldModel(character_id=1, map_id=MAP, pos=(10, 10), perception=8, health=6, max_health=10)
    for x in range(30):
        for y in range(30):
            w.view.tiles[(x, y)] = "dirt"
    w.terrain_center, w.terrain_map = w.pos, MAP
    w.entities = [Entity("npc", 3, hostile_at, code="gristlewick")]
    w.hostile_types.add(("npc", "gristlewick"))
    c = PlayContext(Memory(), Policy(kind="scripted", goals=[], on_hostile="ignore"), random.Random(0), plan=Plan([], dict(PARAM_DEFAULTS)))
    return w, c


class CautiousSafeDefaultTest(unittest.TestCase):
    """Hurt, with a known hostile near, the safe default explores nothing."""

    def test_it_steps_away_from_the_hostile(self):
        w, c = hurt_beside((13, 10))
        out = dispatch(w, c)
        self.assertEqual((out.state, out.reason), ("Explore", "hurt, hostile near: step away"))
        step = (out.intents[0]["x"], out.intents[0]["y"])
        self.assertEqual(step[0], 9, "further from the hostile, to the west")
        self.assertEqual(c.memory.path, [], "one step, no walk queue")

    def boxed_in_as_it_closes(self) -> tuple[WorldModel, PlayContext]:
        w, c = hurt_beside((13, 10))
        w.entity_moves[("npc", 3)] = ((14, 10), w.tick)  # it stepped toward us
        for x in range(30):
            for y in range(30):
                if (x, y) != (10, 10) and x <= 10:
                    w.view.tiles[(x, y)] = "wall"
        return w, c

    def test_it_holds_when_no_step_gets_further(self):
        w, c = self.boxed_in_as_it_closes()
        out = dispatch(w, c)
        self.assertEqual((out.reason, out.intents), ("hurt, hostile near: hold", None))

    def test_a_hostile_that_is_not_closing_holds_nothing(self):
        """A82, free-play run 8: standing off a hostile that stays where it is
        is idle; the safe default explores, still away from it."""
        w, c = self.boxed_in_as_it_closes()
        w.entity_moves.clear()
        out = dispatch(w, c)
        self.assertNotIn("hostile near", out.reason)
        self.assertIsNone(c.memory.keep_away_hold)

    def test_a_pause_in_closing_does_not_restart_the_hold(self):
        w, c = self.boxed_in_as_it_closes()
        self.assertEqual(dispatch(w, c).reason, "hurt, hostile near: hold")
        moved = w.entity_moves.pop(("npc", 3))
        w.tick += KEEP_AWAY_HOLD_TICKS // 2
        self.assertNotIn("hostile near", dispatch(w, c).reason)
        w.tick += KEEP_AWAY_HOLD_TICKS // 2
        w.entity_moves[("npc", 3)] = (moved[0], w.tick)  # closing again
        self.assertNotIn("hostile near", dispatch(w, c).reason, "the bound counts from the first hold")

    def test_a_hold_ends_after_its_bound(self):
        w, c = self.boxed_in_as_it_closes()
        self.assertEqual(dispatch(w, c).reason, "hurt, hostile near: hold")
        w.entity_moves[("npc", 3)] = ((14, 10), w.tick + KEEP_AWAY_HOLD_TICKS)  # still closing
        w.tick += KEEP_AWAY_HOLD_TICKS
        self.assertNotIn("hostile near", dispatch(w, c).reason, "it explores again")

    def test_healing_clears_a_hold_so_a_later_one_starts_afresh(self):
        w, c = hurt_beside((13, 10))
        c.memory.keep_away_hold = w.tick - 10 * KEEP_AWAY_HOLD_TICKS  # left from an earlier hurt spell
        w.health = 10
        dispatch(w, c)
        self.assertIsNone(c.memory.keep_away_hold)

    def test_at_full_health_it_explores(self):
        w, c = hurt_beside((13, 10))
        w.health = 10
        self.assertNotIn("hostile near", dispatch(w, c).reason)

    def test_a_hostile_out_of_the_caution_radius_does_not_stop_it(self):
        w, c = hurt_beside((25, 25))
        self.assertNotIn("hostile near", dispatch(w, c).reason)


class FirstPlanCutsTheSafeDefaultQueueTest(unittest.TestCase):
    """The first plan cuts the safe default's walk at its next step (A71)."""

    def runner(self, goal: str):
        llm = FakeLLM({"goals": [{"op": "travel", "to": "town", "x": 0, "y": 0}]})
        s, r = make(llm), fake_runner()
        r.plan = Plan([], dict(PARAM_DEFAULTS))  # no plan yet: the safe default walks
        r.mem.held_queue = {"queue_id": "q", "next_index": 1}
        r.mem.path, r.mem.goal = [(1, 0), (2, 0)], goal
        round_trip(s, r)
        return r

    def test_the_safe_default_walk_is_cut(self):
        r = self.runner("explore")
        self.assertEqual(r.plan.current()["to"], "town", "applied at once: the walk serves no op")
        self.assertTrue(r.mem.resend_held_queue, "the held queue is replaced at the next poll")
        self.assertTrue(r.mem.need_position)

    def test_a_reflex_walk_is_not_cut(self):
        r = self.runner("safe")  # Retreat's walk: a survival reflex, not the safe default
        self.assertEqual(r.plan.current()["to"], "town")
        self.assertFalse(r.mem.resend_held_queue)


if __name__ == "__main__":
    unittest.main()
