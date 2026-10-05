"""A58: while the policy ``goto`` is still owed, the walk comes first.

Each test sets up something a state would act on, checks the state takes it
with no ``goto`` in the goals, then checks it yields while the goto is owed.
Also covers the honest regen probe (A10) and stuck detection on a kept goto
path (A15).
"""

import random
import unittest

from agentrealm_agent.config import Policy
from agentrealm_agent.directives import PARAM_DEFAULTS
from agentrealm_agent.healing import (
    REGEN_MEASURE_TICKS,
    note_regen_sample,
    raise_buy_potion,
)
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.memory import Memory
from agentrealm_agent.navigation import stuck as nav_stuck
from agentrealm_agent.pathing import goto_navigation_pending
from agentrealm_agent.plan import Plan
from agentrealm_agent.states import dispatch
from agentrealm_agent.states.base import PlayContext
from agentrealm_agent.states.break_state import BreakState
from agentrealm_agent.states.investigate import InvestigateState
from agentrealm_agent.states.shop import ShopState
from agentrealm_agent.states.travel import TravelState
from agentrealm_agent.travel import refresh_travel_stack
from agentrealm_agent.world import Entity, WorldModel, ZoneFact

GOTO = (11, 0)


def world(at=(1, 0), width=12) -> WorldModel:
    """Map 1, one row of open dirt ``width`` wide, full health."""
    w = WorldModel(character_id=1, map_id=1, pos=at, perception=12, gems=10, health=10, max_health=10)
    for x in range(width):
        w.view.tiles[(x, 0)] = "dirt"
    w.terrain_center, w.terrain_map = at, 1
    return w


def ctx(*, goto: bool, m: Memory | None = None, plan: Plan | None = None) -> PlayContext:
    goals = ["goto", "explore"] if goto else ["explore"]
    policy = Policy(kind="scripted", goals=goals, goto=GOTO if goto else None)
    return PlayContext(
        m or Memory(),
        policy,
        random.Random(0),
        params=dict(PARAM_DEFAULTS),
        knowledge=KnowledgeBase.empty("sandbox"),
        plan=plan,
    )


class GotoPendingTest(unittest.TestCase):
    def test_pending_until_reached_or_given_up(self):
        w = world()
        c = ctx(goto=True)
        self.assertTrue(goto_navigation_pending(w, c.memory, c.policy))
        self.assertFalse(goto_navigation_pending(w, c.memory, ctx(goto=False).policy))
        att = nav_stuck.track(c.memory, w, "goto", GOTO)
        nav_stuck.give_up(c.memory, w, att, "no_path")
        self.assertFalse(goto_navigation_pending(w, c.memory, c.policy), "backed off")
        w.pos = GOTO
        self.assertFalse(goto_navigation_pending(w, Memory(), c.policy), "reached")


class GotoDefersStatesTest(unittest.TestCase):
    def test_shop(self):
        w = world()
        w.entities = [Entity("supply", 5, (2, 0), "torch", gem_price=3)]
        for goto, want in ((False, True), (True, False)):
            m = Memory()
            raise_buy_potion(m, why="test", code="torch")
            self.assertEqual(ShopState().guard(w, ctx(goto=goto, m=m)), want)

    def test_investigate(self):
        w = world()
        w.view.tiles[(1, 1)] = "wall"
        w.view.readable[(1, 1)] = True
        self.assertTrue(InvestigateState().guard(w, ctx(goto=False)))
        self.assertFalse(InvestigateState().guard(w, ctx(goto=True)))

    def test_travel(self):
        w = world()
        for goto, want in ((False, True), (True, False)):
            m = Memory()
            refresh_travel_stack(m, ["travel:point:5:0"])
            self.assertEqual(TravelState().guard(w, ctx(goto=goto, m=m)), want)

    def test_break_plan_op_waits_but_the_gotos_own_break_runs(self):
        w = world()
        op = {"op": "break_block", "x": 3, "y": 0, "capability": "magic"}
        self.assertTrue(BreakState().guard(w, ctx(goto=False, plan=Plan([op], dict(PARAM_DEFAULTS)))))
        self.assertFalse(BreakState().guard(w, ctx(goto=True, plan=Plan([op], dict(PARAM_DEFAULTS)))))

        m = Memory()
        other = nav_stuck.track(m, w, "explore", (5, 0))
        other.level = nav_stuck.BREAK
        self.assertFalse(BreakState().guard(w, ctx(goto=True, m=m)), "another goal's break waits")
        att = nav_stuck.track(m, w, "goto", GOTO)
        att.level = nav_stuck.BREAK
        self.assertTrue(BreakState().guard(w, ctx(goto=True, m=m)), "the goto's own break")

    def test_heal_does_not_walk_to_a_safe_tile(self):
        w = world(at=(3, 0))
        w.health = 5
        w.zones[1] = {(0, 0): ZoneFact(safe=True), (3, 0): ZoneFact(safe=False)}
        out = dispatch(w, ctx(goto=False))
        self.assertEqual((out.state, out.intents), ("Heal", [{"verb": "SetPosition", "x": 2, "y": 0}]))
        out = dispatch(w, ctx(goto=True))
        self.assertEqual(out.state, "Explore")
        self.assertEqual(out.intents, [{"verb": "SetPosition", "x": 4, "y": 0}])

    def test_heal_still_rests_in_the_safe_zone_it_stands_in(self):
        w = world(at=(3, 0))
        w.health = 5
        w.zones[1] = {(3, 0): ZoneFact(safe=True)}
        out = dispatch(w, ctx(goto=True))
        self.assertEqual(out.state, "Heal")
        self.assertIsNone(out.intents)

    def test_plan_wait_does_not_hold_the_goto_walk(self):
        w = world()
        plan = Plan([{"op": "wait", "seconds": 60}], dict(PARAM_DEFAULTS))
        self.assertIn("plan wait", dispatch(w, ctx(goto=False, plan=plan)).reason)
        plan = Plan([{"op": "wait", "seconds": 60}], dict(PARAM_DEFAULTS))
        out = dispatch(w, ctx(goto=True, plan=plan))
        self.assertEqual(out.intents, [{"verb": "SetPosition", "x": 2, "y": 0}])


class KeptGotoPathStuckTest(unittest.TestCase):
    """The goto path is kept between decisions, and stuck detection still runs on it."""

    def _walking(self):
        w, c = world(), ctx(goto=True)
        out = dispatch(w, c)
        self.assertEqual(out.intents, [{"verb": "SetPosition", "x": 2, "y": 0}])
        self.assertEqual(c.memory.goal, "goto")
        return w, c

    def test_path_is_kept(self):
        w, c = self._walking()
        path = list(c.memory.path)
        dispatch(w, c)
        self.assertEqual(c.memory.path, path, "not replanned while it still yields a step")

    def test_stalled_kept_path_escalates(self):
        w, c = self._walking()
        att = nav_stuck.active(c.memory, w)
        att.moves = nav_stuck.PROGRESS_MOVE_LIMIT
        dispatch(w, c)
        self.assertEqual(att.level, nav_stuck.CAUTIOUS)

    def test_stalled_kept_path_at_the_last_level_gives_up(self):
        w, c = self._walking()
        att = nav_stuck.active(c.memory, w)
        att.level = nav_stuck.ALT_ROUTE
        att.moves = nav_stuck.PROGRESS_MOVE_LIMIT
        dispatch(w, c)
        signals = c.memory.nav_stuck.stuck_signals
        self.assertEqual([s["goal"] for s in signals], ["goto"])
        self.assertFalse(goto_navigation_pending(w, c.memory, c.policy))


class RegenProbeTest(unittest.TestCase):
    """A10: only a hurt window in a safe zone gives a regen verdict."""

    def _sample(self, w, m, ticks, health=None):
        verdict = None
        for _ in range(ticks):
            w.tick += 10
            if health is not None:
                w.health = health
            verdict = note_regen_sample(m, w) or verdict
        return verdict

    def test_full_health_gives_no_verdict(self):
        w, m = world(), Memory()
        self.assertIsNone(self._sample(w, m, REGEN_MEASURE_TICKS))
        self.assertIsNone(m.heal_regen_sample)

    def test_unknown_max_health_gives_no_verdict(self):
        w, m = world(), Memory()
        w.health, w.max_health = 1, None
        self.assertIsNone(self._sample(w, m, REGEN_MEASURE_TICKS))

    def test_hurt_with_no_health_back_is_no(self):
        w, m = world(), Memory()
        w.health = 5
        self.assertEqual(self._sample(w, m, REGEN_MEASURE_TICKS // 10 + 2), "no")

    def test_health_back_is_yes_even_when_it_reaches_full(self):
        w, m = world(), Memory()
        w.health = 9
        self.assertIsNone(self._sample(w, m, 1))
        self.assertEqual(self._sample(w, m, 1, health=10), "yes")

    def test_full_health_in_town_never_raises_a_buy(self):
        w = world(at=(1, 0))
        w.zones[1] = {(1, 0): ZoneFact(safe=True)}
        c = ctx(goto=False)
        for _ in range(REGEN_MEASURE_TICKS // 10 + 2):
            w.tick += 10
            self.assertNotEqual(dispatch(w, c).state, "Heal")
        self.assertFalse(c.memory.heal_regen_absent)
        self.assertEqual(c.memory.buy_signals, [])


if __name__ == "__main__":
    unittest.main()
