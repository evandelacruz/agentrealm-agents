"""The oscillation guard (A15, A58): no pacing between two cells, whoever causes it.

Live run 3 of A58 paced between two cells for minutes while the goto walk and
another state took turns. The dispatch tests below rebuild that through the
real dispatcher: a stand-in for the competing state sits above the real
Explore goto walk and steps the character back each time it arrives.
"""

import importlib
import random
import unittest
from unittest import mock

from agentrealm_agent.config import Policy
from agentrealm_agent.directives import PARAM_DEFAULTS
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.memory import Memory
from agentrealm_agent.navigation import oscillation
from agentrealm_agent.navigation import stuck as nav_stuck
from agentrealm_agent.pathing import goto_navigation_pending
from agentrealm_agent.states.base import PlayContext, State, StateOutcome
from agentrealm_agent.states.explore import ExploreState
from agentrealm_agent.states.intents import set_position
from agentrealm_agent.world import WorldModel

# The module, not the ``dispatch`` function the package re-exports.
dispatch_module = importlib.import_module("agentrealm_agent.states.dispatch")

GOTO = (11, 0)
A, B = (2, 0), (3, 0)


def world(at=A) -> WorldModel:
    """Map 1: a walled corridor of dirt from x=0 to 11, open to the fog only
    past its west end, so once the goto is given up Explore heads west."""
    w = WorldModel(character_id=1, map_id=1, pos=at, perception=12, gems=10, health=10, max_health=10)
    for x in range(-1, 13):
        w.view.tiles[(x, -1)] = w.view.tiles[(x, 1)] = "stone"
    for x in range(12):
        w.view.tiles[(x, 0)] = "dirt"
    w.view.tiles[(12, 0)] = "stone"
    w.terrain_center, w.terrain_map = at, 1
    return w


def ctx() -> PlayContext:
    return PlayContext(
        Memory(),
        Policy(kind="scripted", goals=["goto", "explore"], goto=GOTO),
        random.Random(0),
        params=dict(PARAM_DEFAULTS),
        knowledge=KnowledgeBase.empty("sandbox"),
    )


class BreakWalkBack(State):
    """Like Break at the goto's step 2 walking back to a block behind it:
    on B it takes the round and walks to A with a path of its own."""

    name = "BreakWalkBack"

    def guard(self, world, ctx):
        return world.pos == B and goto_navigation_pending(world, ctx.memory, ctx.policy)

    def done(self, world, ctx):
        return not self.guard(world, ctx)

    def act(self, world, ctx):
        ctx.memory.path, ctx.memory.goal = [A], "break"
        return StateOutcome([set_position(A)], "break → A", state=self.name)


class DetourBack(BreakWalkBack):
    """Like Equip or Loot stepping aside mid-goto: one step to A, no path kept."""

    name = "DetourBack"

    def act(self, world, ctx):
        return StateOutcome([set_position(A)], "detour", state=self.name)


def land(w: WorldModel, c: PlayContext, out: StateOutcome) -> None:
    """The decision's SetPosition lands at once; the runner trims the walked cell off the path."""
    w.tick += 10
    if out.intents and out.intents[0]["verb"] == "SetPosition":
        w.pos = (out.intents[0]["x"], out.intents[0]["y"])
        if c.memory.path[:1] == [w.pos]:
            c.memory.path = c.memory.path[1:]


def play(competitor: State, decisions: int = 20):
    """Run the real dispatcher with ``competitor`` above Explore. Returns the
    world, the context and the cell after each decision."""
    w, c = world(), ctx()
    cells = []
    with mock.patch.object(dispatch_module, "STATES", (competitor, ExploreState())):
        for _ in range(decisions):
            land(w, c, dispatch_module.dispatch(w, c))
            cells.append(w.pos)
    return w, c, cells


class RetreatPace(State):
    """A survival state pacing on its own: walks its ``safe`` path from A to
    B, then a bare step back from B to A, as a Retreat/Fight pair might."""

    name = "RetreatPace"

    def guard(self, world, ctx):
        return world.pos in (A, B)

    def done(self, world, ctx):
        return not self.guard(world, ctx)

    def act(self, world, ctx):
        if world.pos == A:
            ctx.memory.path, ctx.memory.goal = [B], "safe"
            return StateOutcome([set_position(B)], "retreat → B", state=self.name)
        return StateOutcome([set_position(A)], "close in", state=self.name)


class PacingDispatchTest(unittest.TestCase):
    def _assert_guard_stopped_it(self, competitor):
        w, c, cells = play(competitor)
        events = c.memory.nav_stuck.oscillations
        self.assertEqual(len(events), 1, cells)
        self.assertEqual(events[0]["goal"], "goto")
        self.assertEqual(events[0]["cells"], [list(A), list(B)])
        signals = c.memory.nav_stuck.stuck_signals
        self.assertEqual([(s["goal"], s["reason"]) for s in signals], [("goto", "pacing")])
        self.assertFalse(goto_navigation_pending(w, c.memory, c.policy), "goto backed off")
        # Explore takes the next goal and walks west to the fog, never back.
        after = cells[oscillation.OSCILLATION_STEPS - 1 :]
        self.assertEqual(after[:4], [(2, 0), (1, 0), (0, 0), (0, 0)], cells)

    def test_break_walk_against_the_goto_walk(self):
        self._assert_guard_stopped_it(BreakWalkBack())

    def test_detour_against_the_goto_walk(self):
        self._assert_guard_stopped_it(DetourBack())

    def test_survival_pacing_does_not_back_off_the_goto(self):
        w, c = world(at=(1, 0)), ctx()
        with mock.patch.object(dispatch_module, "STATES", (ExploreState(),)):
            land(w, c, dispatch_module.dispatch(w, c))  # the goto walk reaches A
        self.assertEqual(nav_stuck.active(c.memory, w).goal, "goto")
        with mock.patch.object(dispatch_module, "STATES", (RetreatPace(), ExploreState())):
            for _ in range(14):
                land(w, c, dispatch_module.dispatch(w, c))
        events = c.memory.nav_stuck.oscillations
        self.assertEqual(len(events), 2)
        for event in events:
            self.assertNotIn("goal", event, "nothing given up")
            self.assertEqual(event["states"], ["RetreatPace"])
        self.assertEqual(c.memory.nav_stuck.stuck_signals, [])
        self.assertTrue(goto_navigation_pending(w, c.memory, c.policy), "goto not backed off")

    def test_a_straight_walk_never_fires(self):
        w, c = world(), ctx()
        with mock.patch.object(dispatch_module, "STATES", (ExploreState(),)):
            for _ in range(8):
                land(w, c, dispatch_module.dispatch(w, c))
        self.assertEqual(w.pos, (10, 0))
        self.assertEqual(c.memory.nav_stuck.oscillations, [])


class DetectorTest(unittest.TestCase):
    def test_pacing_needs_six_cells_over_two(self):
        self.assertTrue(oscillation.pacing([A, B, A, B, A, B]))
        self.assertFalse(oscillation.pacing([A, B, A, B, A]), "too short")
        self.assertFalse(oscillation.pacing([A, B, (4, 0), A, B, (4, 0)]), "three cells")
        self.assertTrue(oscillation.pacing([(4, 0), A, B, A, B, A, B]), "only the last six count")

    def _see(self, m, w, cells, move=("", "")):
        """Stand on each cell in turn, ``move`` (walk goal, state) having led there."""
        fired = []
        for p in cells:
            w.pos = p
            w.tick += 1
            m.nav_stuck.last_move = move
            fired.append(oscillation.check(m, w))
        return fired

    def test_standing_still_is_not_pacing(self):
        m, w = Memory(), world()
        fired = self._see(m, w, [A] * 10 + [B] * 10)
        self.assertEqual(fired, [None] * 20)
        self.assertEqual(m.nav_stuck.recent_cells, [A, B])

    def test_fires_once_and_gives_up_the_active_attempt(self):
        m, w = Memory(), world()
        nav_stuck.track(m, w, "goto", GOTO)
        m.path, m.goal = [B, GOTO], "goto"
        fired = self._see(m, w, [A, B, A, B, A, B], move=("goto", "Explore"))
        self.assertEqual(fired[:5], [None] * 5)
        self.assertEqual(fired[5]["reason"], "pacing")
        self.assertEqual(fired[5]["target"], list(GOTO))
        self.assertTrue(nav_stuck.backed_off(m, "goto", 1, GOTO, w.tick))
        self.assertEqual((m.path, m.goal), ([], ""))
        self.assertEqual(m.nav_stuck.recent_cells, [], "starts counting afresh")

    def test_an_attempt_that_made_none_of_the_moves_is_kept(self):
        m, w = Memory(), world()
        nav_stuck.track(m, w, "goto", GOTO)
        fired = self._see(m, w, [A, B, A, B, A, B], move=("safe", "Retreat"))
        self.assertNotIn("goal", fired[5])
        self.assertEqual(fired[5]["states"], ["Retreat"])
        self.assertFalse(nav_stuck.backed_off(m, "goto", 1, GOTO, w.tick))
        self.assertIsNotNone(nav_stuck.active(m, w))

    def test_note_move_names_the_walk_only_for_a_step_along_its_path(self):
        m = Memory()
        m.path, m.goal = [B, GOTO], "goto"
        oscillation.note_move(m, [set_position(B)], "Explore")
        self.assertEqual(m.nav_stuck.last_move, ("goto", "Explore"))
        oscillation.note_move(m, [set_position(A)], "Fight")
        self.assertEqual(m.nav_stuck.last_move, ("", "Fight"), "a step off the path is not the walk's")
        oscillation.note_move(m, None, "")
        self.assertEqual(m.nav_stuck.last_move, ("", ""))

    def test_a_map_change_starts_over(self):
        m, w = Memory(), world()
        self._see(m, w, [A, B, A, B, A])
        w.map_id = 2
        self.assertEqual(self._see(m, w, [B]), [None])
        self.assertEqual(m.nav_stuck.recent_cells, [B])


if __name__ == "__main__":
    unittest.main()
