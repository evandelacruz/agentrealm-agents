"""The oscillation guard (A15, A58, A90): no pacing between two cells, whoever causes it.

Two walks taking turns can pace the character between two cells. The
dispatch tests below rebuild that through the real dispatcher: a stand-in
for the competing state sits above the real Travel walk of the plan's goto
(a ``travel`` point op) and steps the character back each time it arrives.
"""

import importlib
import random
import unittest
from unittest import mock

from agentrealm_agent.break_memory import BREAK_GOAL, given_up_blocks, nominate_on_path
from agentrealm_agent.config import Policy
from agentrealm_agent.directives import PARAM_DEFAULTS
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.memory import Memory
from agentrealm_agent.navigation import oscillation
from agentrealm_agent.navigation import stuck as nav_stuck
from agentrealm_agent.plan import Plan
from agentrealm_agent.states.base import PlayContext, State, StateOutcome, my_op
from agentrealm_agent.states.break_state import break_outcome
from agentrealm_agent.states.explore import ExploreState
from agentrealm_agent.states.intents import set_position
from agentrealm_agent.states.travel import TravelState
from agentrealm_agent.world import WorldModel

# The module, not the ``dispatch`` function the package re-exports.
dispatch_module = importlib.import_module("agentrealm_agent.states.dispatch")

GOTO = (11, 0)
A, B = (2, 0), (3, 0)
WALK = "travel:point"  # the goal label of the goto's travel op walk


def world(at=A) -> WorldModel:
    """Map 1: a walled corridor of dirt from x=0 to 11, open to the fog only
    past its west end, away from the goto: once the goto is given up the safe
    default, which serves it (A120), has no frontier toward it and asks."""
    w = WorldModel(character_id=1, map_id=1, pos=at, perception=12, gems=10, health=10, max_health=10)
    for x in range(-1, 13):
        w.view.tiles[(x, -1)] = w.view.tiles[(x, 1)] = "stone"
    for x in range(12):
        w.view.tiles[(x, 0)] = "dirt"
    w.view.tiles[(12, 0)] = "stone"
    w.terrain_center, w.terrain_map = at, 1
    return w


def ctx(goto: tuple[int, int] = GOTO) -> PlayContext:
    policy = Policy(kind="scripted", goals=["goto", "explore"], goto=goto)
    return PlayContext(
        Memory(),
        policy,
        random.Random(0),
        params=dict(PARAM_DEFAULTS),
        knowledge=KnowledgeBase.empty("sandbox"),
        plan=Plan.from_policy(policy, dict(PARAM_DEFAULTS)),
    )


def goto_pending(world: WorldModel, ctx: PlayContext) -> bool:
    """The plan's top op is the goto's travel, and its walk is not backed off."""
    op = ctx.plan.current() if ctx.plan is not None else None
    if op is None or op["op"] != "travel":
        return False
    return not nav_stuck.backed_off(ctx.memory, WALK, world.map_id, (op["x"], op["y"]), world.tick)


class BreakWalkBack(State):
    """Like Break at the goto's step 2 walking back to a block behind it:
    on B it takes the round and walks to A with a path of its own."""

    name = "BreakWalkBack"

    def guard(self, world, ctx):
        return world.pos == B and goto_pending(world, ctx)

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
    """Run the real dispatcher with ``competitor`` above Travel and Explore.
    Returns the world, the context and the cell after each decision."""
    w, c = world(), ctx()
    cells = []
    with mock.patch.object(dispatch_module, "STATES", (competitor, TravelState(), ExploreState())):
        for _ in range(decisions):
            land(w, c, dispatch_module.dispatch(w, c))
            cells.append(w.pos)
    return w, c, cells


class GotoReplanBack(State):
    """Like Explore in A58 run 5: the goto walk itself, replanned each
    decision, steps east from A and then back west from B. Named Travel,
    so the guard files every move as the goto walk's."""

    name = "Travel"

    def guard(self, world, ctx):
        return world.pos in (A, B) and goto_pending(world, ctx)

    def done(self, world, ctx):
        return not self.guard(world, ctx)

    def act(self, world, ctx):
        m = ctx.memory
        nav_stuck.track(m, world, WALK, GOTO)
        nxt = B if world.pos == A else A
        m.path, m.goal = [nxt, GOTO], WALK
        return StateOutcome([set_position(nxt)], "goto replan", state=self.name)


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
    def _assert_guard_stopped_it(self, competitor, given_up):
        # Twelve decisions.
        w, c, cells = play(competitor, decisions=12)
        events = c.memory.nav_stuck.oscillations
        self.assertEqual(len(events), 1, cells)
        self.assertEqual(sorted(events[0]["goals"]), sorted(given_up))
        self.assertEqual(events[0]["cells"], [list(A), list(B)])
        signals = c.memory.nav_stuck.stuck_signals
        self.assertEqual(sorted((s["goal"], s["reason"]) for s in signals), sorted((g, "pacing") for g in given_up))
        self.assertFalse(goto_pending(w, c), "goto backed off")
        # The safe default serves the goto (A120): the fog lies west, away
        # from it, so it stays put and asks the planner, never pacing again.
        after = cells[oscillation.OSCILLATION_STEPS - 1 :]
        self.assertEqual(set(after), {B}, cells)
        self.assert_asked(c)

    def assert_asked(self, c):
        why = {s.get("why") for s in c.memory.strategist_signals if s["trigger"] == "no_way_on"}
        self.assertEqual(why, {"no frontier brings travel:point 11,0 nearer"})

    def test_break_walk_against_the_goto_walk(self):
        # Both are walks: both are given up.
        self._assert_guard_stopped_it(BreakWalkBack(), [WALK, BREAK_GOAL])

    def test_detour_against_the_goto_walk(self):
        # The detour step is no walk: only the goto's walk is given up.
        self._assert_guard_stopped_it(DetourBack(), [WALK])

    def test_pacing_the_goto_walk_took_no_part_in_does_not_back_it_off(self):
        w, c = world(at=(1, 0)), ctx()
        with mock.patch.object(dispatch_module, "STATES", (TravelState(),)):
            land(w, c, dispatch_module.dispatch(w, c))  # the goto walk reaches A
        self.assertEqual(nav_stuck.active(c.memory, w).goal, WALK)
        with mock.patch.object(dispatch_module, "STATES", (RetreatPace(), TravelState())):
            for _ in range(14):
                land(w, c, dispatch_module.dispatch(w, c))
        events = c.memory.nav_stuck.oscillations
        self.assertTrue(events)
        for event in events:
            self.assertEqual(event["goals"], ["safe"], "only the walk that paced")
            self.assertEqual(event["states"], ["RetreatPace"])
        self.assertEqual({s["goal"] for s in c.memory.nav_stuck.stuck_signals}, {"safe"})
        self.assertTrue(goto_pending(w, c), "goto not backed off")

    def test_the_goto_walk_pacing_on_its_own_is_given_up(self):
        """A58 run 5: the goto walk's own replans stepped it back and forth.

        The guard alone ends it: one give-up, then the safe default, which
        serves the goto (A120) and has no frontier toward it, asks the planner
        and never paces the two cells again while the goto is backed off. No
        back-step block in the walk is needed for that.
        """
        w, c, cells = play(GotoReplanBack(), decisions=12)
        events = c.memory.nav_stuck.oscillations
        self.assertEqual([(e["goal"], e["states"]) for e in events], [(WALK, ["Travel"])], cells)
        self.assertFalse(goto_pending(w, c), "goto backed off")
        after = cells[oscillation.OSCILLATION_STEPS :]
        self.assertEqual(len(set(after)), 1, cells)
        self.assert_asked(c)

    def test_a_goto_behind_the_agent_takes_the_step_back(self):
        """One step back toward a goto behind it is a route, not pacing."""
        w, c = world(at=B), ctx(goto=(0, 0))
        c.memory.nav_stuck.cells_map = w.map_id
        c.memory.nav_stuck.recent_cells = [A, B]
        c.memory.nav_stuck.recent_moves = [(WALK, "Travel", "")] * 2
        with mock.patch.object(dispatch_module, "STATES", (TravelState(),)):
            out = dispatch_module.dispatch(w, c)
        self.assertEqual((out.intents[0]["x"], out.intents[0]["y"]), A)

    def test_a_straight_walk_never_fires(self):
        w, c = world(), ctx()
        with mock.patch.object(dispatch_module, "STATES", (TravelState(),)):
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

    def _see(self, m, w, cells, move=("", "", "")):
        """Stand on each cell in turn, ``move`` (walk goal, state, attempt key) having led there."""
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
        att = nav_stuck.track(m, w, "goto", GOTO)
        m.path, m.goal = [B, GOTO], "goto"
        fired = self._see(m, w, [A, B, A, B, A, B], move=("goto", "Explore", att.key))
        self.assertEqual(fired[:5], [None] * 5)
        self.assertEqual(fired[5]["reason"], "pacing")
        self.assertEqual(fired[5]["target"], list(GOTO))
        self.assertTrue(nav_stuck.backed_off(m, "goto", 1, GOTO, w.tick))
        self.assertEqual((m.path, m.goal), ([], ""))
        self.assertEqual(m.nav_stuck.recent_cells, [], "starts counting afresh")

    def test_an_attempt_that_made_none_of_the_moves_is_kept(self):
        m, w = Memory(), world()
        nav_stuck.track(m, w, "goto", GOTO)
        fired = self._see(m, w, [A, B, A, B, A, B], move=("safe", "Retreat", ""))
        self.assertNotIn("goal", fired[5])
        self.assertEqual(fired[5]["states"], ["Retreat"])
        self.assertFalse(nav_stuck.backed_off(m, "goto", 1, GOTO, w.tick))
        self.assertIsNotNone(nav_stuck.active(m, w))

    def test_note_move_names_the_walk_only_for_a_step_along_its_path(self):
        m, w = Memory(), world()
        m.path, m.goal = [B, GOTO], "goto"
        oscillation.note_move(m, w, [set_position(B)], "Explore")
        key = nav_stuck.goal_key("goto", 1, GOTO)
        self.assertEqual(m.nav_stuck.last_move, ("goto", "Explore", key), "a walk with no attempt is known by where it ends")
        oscillation.note_move(m, w, [set_position(A)], "Fight")
        self.assertEqual(m.nav_stuck.last_move, ("", "Fight", ""), "a step off the path is no walk")
        oscillation.note_move(m, w, None, "")
        self.assertEqual(m.nav_stuck.last_move, ("", "", ""))

    def test_flee_or_retreat_pacing_hands_the_cells_to_the_escape(self):
        m, w = Memory(), world()
        fired = self._see(m, w, [A, B, A, B, A, B], move=("", "Flee", ""))
        self.assertTrue(fired[5]["escape"])
        self.assertEqual(oscillation.take_escape(m, w), {A}, "the cell stood on is not escaped")
        self.assertEqual(oscillation.take_escape(m, w), set(), "read once")

    def test_escape_cells_last_only_for_the_decision_the_guard_fired_on(self):
        m, w = Memory(), world()
        self._see(m, w, [A, B, A, B, A, B], move=("", "Flee", ""))
        # Nothing took them on that decision (Explore ran); the next one starts clean.
        self._see(m, w, [(4, 0)], move=("", "Explore", ""))
        self.assertEqual(oscillation.take_escape(m, w), set())

    def test_pacing_with_any_other_state_forces_no_escape(self):
        m, w = Memory(), world()
        fired = self._see(m, w, [A, B, A, B, A, B], move=("", "Fight", ""))
        self.assertNotIn("escape", fired[5])
        self.assertEqual(oscillation.take_escape(m, w), set())

    def test_pacing_no_state_moved_forces_no_escape(self):
        m, w = Memory(), world()
        fired = self._see(m, w, [A, B, A, B, A, B])
        self.assertEqual(fired[5]["states"], [])
        self.assertNotIn("escape", fired[5])

    def test_a_map_change_starts_over(self):
        m, w = Memory(), world()
        self._see(m, w, [A, B, A, B, A])
        w.map_id = 2
        self.assertEqual(self._see(m, w, [B]), [None])
        self.assertEqual(m.nav_stuck.recent_cells, [B])



class EveryWalkGivenUpTest(unittest.TestCase):
    """Two walks pace: each is given up, whichever state sent it and whichever attempt is active (A90).

    The Travel walk is stuck at step 2, so Break walks to the block it
    nominated; between Break's moves the safe default's Explore walk steps
    back toward its frontier, and its ``track`` makes its own attempt the
    active one each time.
    """

    BLOCK = (9, 0)
    FRONTIER = (0, 0)

    def setUp(self):
        self.m, self.w = Memory(), world()
        self.w.view.tiles[self.BLOCK] = "bush"
        self.travel = nav_stuck.track(self.m, self.w, WALK, GOTO)
        self.travel.level = nav_stuck.BREAK
        nav_stuck.set_break_target(self.travel, self.BLOCK, "cut")

    def decide(self, mover):
        """One decision as dispatch runs it: the guard, then a state's move, then ``note_move``."""
        m, w = self.m, self.w
        event = oscillation.check(m, w)
        if mover == "Break":
            m.path, m.goal = [B, (4, 0), self.BLOCK] if w.pos == A else [A, B, self.BLOCK], BREAK_GOAL
            step = m.path[0]
        else:
            nav_stuck.track(m, w, "explore", self.FRONTIER)  # Explore's walk is the active attempt now
            m.path, m.goal = [A, (1, 0), self.FRONTIER] if w.pos == B else [B, A, self.FRONTIER], "explore"
            step = m.path[0]
        oscillation.note_move(m, w, [set_position(step)], mover)
        w.pos, w.tick = step, w.tick + 10
        return event

    def alternate(self, rounds=3):
        """Break toward the block, Explore back toward the frontier, until the guard fires."""
        self.w.pos = A
        for i in range(rounds * 2 + 2):
            event = self.decide("Break" if self.w.pos == A else "Explore")
            if event is not None:
                return event
        self.fail("the guard never fired")

    def test_the_break_walk_is_known_by_its_block_whatever_attempt_is_active(self):
        m, w = self.m, self.w
        nav_stuck.track(m, w, "explore", self.FRONTIER)
        m.path, m.goal = [B, (4, 0), self.BLOCK], BREAK_GOAL
        oscillation.note_move(m, w, [set_position(B)], "Break")
        self.assertEqual(m.nav_stuck.last_move, (BREAK_GOAL, "Break", nav_stuck.goal_key(BREAK_GOAL, 1, self.BLOCK)))

    def test_alternating_break_and_explore_walks_both_give_up(self):
        event = self.alternate()
        self.assertEqual(sorted(event["goals"]), sorted([BREAK_GOAL, "explore"]))
        self.assertEqual(event["states"], ["Break", "Explore"])
        m, w = self.m, self.w
        self.assertTrue(nav_stuck.backed_off(m, BREAK_GOAL, 1, self.BLOCK, w.tick), "the block walk is given up")
        self.assertTrue(nav_stuck.backed_off(m, "explore", 1, self.FRONTIER, w.tick), "and so is the frontier")
        self.assertEqual(given_up_blocks(m.nav_stuck, 1, w.tick), {self.BLOCK})
        # The Travel walk whose step 2 nominated the block leaves step 2.
        self.assertEqual(self.travel.level, nav_stuck.REVEAL)
        self.assertEqual(self.travel.reasons, ["pacing"])
        self.assertIsNone(nav_stuck.break_target(self.travel))
        self.assertEqual(event["escalated"], [{"goal": WALK, "target": list(GOTO), "block": list(self.BLOCK)}])
        self.assertFalse(nav_stuck.backed_off(m, WALK, 1, GOTO, w.tick), "the travel itself walks on")
        # Each give-up is a stuck trigger for the planner, with when it lapses.
        sig = {s["goal"]: s for s in m.nav_stuck.stuck_signals}
        self.assertEqual(sig[BREAK_GOAL]["retry_at_tick"], event["tick"] + nav_stuck.BACKOFF_BASE_TICKS)

    def test_a_given_up_block_is_not_nominated_until_its_backoff_ends(self):
        self.alternate()
        m, w = self.m, self.w
        w.pos = (5, 0)
        skip = given_up_blocks(m.nav_stuck, 1, w.tick)
        self.assertIsNone(nominate_on_path(w, None, w.pos, GOTO, skip))
        w.tick = m.nav_stuck.given_up[nav_stuck.goal_key(BREAK_GOAL, 1, self.BLOCK)].until
        self.assertEqual(given_up_blocks(m.nav_stuck, 1, w.tick), frozenset(), "aged by time")
        self.assertEqual(given_up_blocks(m.nav_stuck, 2, 0), frozenset(), "only on its own map")

    def test_the_same_holds_with_the_travel_attempt_active(self):
        # Whichever attempt is active when Break moves, its walk is filed by its block.
        m, w = self.m, self.w
        w.pos = A
        for i in range(8):
            if w.pos == A:
                nav_stuck.track(m, w, WALK, GOTO)
            event = self.decide("Break" if w.pos == A else "Explore")
            if event is not None:
                break
        self.assertIn(BREAK_GOAL, event["goals"])
        self.assertTrue(nav_stuck.backed_off(m, BREAK_GOAL, 1, self.BLOCK, w.tick))


class OpStepBack(State):
    """The top op's executor stepping from B back to A with a step that is no walk."""

    name = "Break"

    def guard(self, world, ctx):
        return my_op(ctx, self.name) is not None and world.pos == B

    def done(self, world, ctx):
        return not self.guard(world, ctx)

    def act(self, world, ctx):
        return StateOutcome([set_position(A)], "step → A", state=self.name)


class ExploreAhead(State):
    """Like the safe default stepping from A toward a frontier at B."""

    name = "Explore"

    def guard(self, world, ctx):
        return world.pos == A

    def done(self, world, ctx):
        return not self.guard(world, ctx)

    def act(self, world, ctx):
        ctx.memory.path, ctx.memory.goal = [B], "explore"
        return StateOutcome([set_position(B)], "explore → B", state=self.name)


class OtherStepBack(OpStepBack):
    """A state that is not the top op's executor, stepping back from B to A."""

    name = "Detour"

    def guard(self, world, ctx):
        return world.pos == B


class PacedOpTest(unittest.TestCase):
    """The top op's executor paced (A103, A104, A90): a walk of its own is given
    up and its executor keeps off it; a step that was no walk stalls the op."""

    BRK = {"op": "break_block", "x": 9, "y": 0, "capability": "cut"}

    def play(self, ops, back):
        w, c = world(), ctx()
        c.plan = Plan(ops, dict(PARAM_DEFAULTS))
        cells = []
        with mock.patch.object(dispatch_module, "STATES", (back, ExploreAhead())):
            for _ in range(12):
                land(w, c, dispatch_module.dispatch(w, c))
                cells.append(w.pos)
        return c, cells

    def drop(self, op, paced, ops_after=()):
        plan = Plan([op, *ops_after], dict(PARAM_DEFAULTS))
        m = Memory()
        dispatch_module._stall_paced_op(plan, plan.current(), paced, world(), m, None)
        return plan, m

    def test_the_op_whose_executor_paced_with_no_walk_is_dropped_and_the_planner_told(self):
        c, cells = self.play([self.BRK], OpStepBack())
        self.assertIsNone(c.plan.current(), "with nothing below it, the op that cannot progress is dropped")
        failed = [s for s in c.memory.strategist_signals if s.get("trigger") == "goal_failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["op"], self.BRK)
        self.assertEqual(failed[0]["reason"], "Break paced between 2,0 and 3,0 with Explore: no way forward")
        self.assertEqual(c.memory.nav_stuck.oscillations[0]["op"], self.BRK)
        self.assertEqual([r.target for r in c.memory.stalled_ops], [self.BRK], "its failure is remembered (A104)")

    def test_the_op_that_paced_with_no_walk_moves_below_work_that_can_progress(self):
        gather = {"op": "gather_gems", "count": 20}
        paced = {"states": ["Break", "Explore"], "unwalked": ["Break"], "cells": [[2, 0], [3, 0]]}
        plan, m = self.drop(self.BRK, paced, ops_after=[gather])
        self.assertEqual(plan.current(), gather)
        self.assertEqual(plan.goals[plan.index + 1], self.BRK)
        stalled = [s for s in m.strategist_signals if s.get("trigger") == "goal_stalled"]
        self.assertEqual(len(stalled), 1)
        self.assertEqual(stalled[0]["reason"], "Break paced between 2,0 and 3,0 with Explore: no way forward")
        self.assertEqual(stalled[0]["now_below"], gather)

    def test_an_op_whose_executor_only_walked_is_left_to_it(self):
        # Every walk that paced was given up; the executor keeps off it by its own rule.
        op = {"op": "explore_area", "x": 9, "y": 0, "radius": 5}
        paced = {"states": ["Detour", "Explore"], "unwalked": ["Detour"], "cells": [[2, 0], [3, 0]]}
        plan, m = self.drop(op, paced)
        self.assertIs(plan.current(), plan.goals[0])
        self.assertFalse(m.strategist_signals)
        self.assertFalse(m.stalled_ops)

    def test_pacing_the_op_executor_took_no_part_in_keeps_the_op(self):
        c, _ = self.play([self.BRK], OtherStepBack())
        self.assertTrue(c.memory.nav_stuck.oscillations, "the guard fired")
        self.assertEqual(c.plan.current(), self.BRK, "Break never moved: its op is not the guard's to drop")

    def test_a_break_op_on_a_given_up_block_is_dropped_with_why(self):
        w, c = world(), ctx()
        c.plan = Plan([self.BRK], dict(PARAM_DEFAULTS))
        nav_stuck.remember_give_up(c.memory.nav_stuck, nav_stuck.goal_key(BREAK_GOAL, 1, (9, 0)), BREAK_GOAL, w.tick)
        out = break_outcome(w, c)
        self.assertIsNone(c.plan.current())
        self.assertIn("the walk to it was given up", out.reason)

    def test_escape_only_pacing_keeps_the_op(self):
        paced = {"states": ["Break", "Flee"], "unwalked": ["Break", "Flee"], "cells": [[2, 0], [3, 0]], "escape": True}
        plan, m = self.drop(self.BRK, paced)
        self.assertEqual(plan.current(), self.BRK)
        self.assertFalse(m.strategist_signals)

    def test_an_op_settled_off_the_top_spares_the_next_one(self):
        # The op that paced was popped before the drop: the next op, with the
        # same executor, took no part in the pacing and stays.
        second = {"op": "break_block", "x": 8, "y": 0, "capability": "cut"}
        plan = Plan([self.BRK, second], dict(PARAM_DEFAULTS))
        m = Memory()
        paced_op = plan.current()
        plan.finish_current("done")
        paced = {"states": ["Break", "Explore"], "unwalked": ["Break"], "cells": [[2, 0], [3, 0]]}
        dispatch_module._stall_paced_op(plan, paced_op, paced, world(), m, None)
        self.assertEqual(plan.current(), second)
        self.assertFalse(m.strategist_signals)
        self.assertFalse(m.stalled_ops)


if __name__ == "__main__":
    unittest.main()
