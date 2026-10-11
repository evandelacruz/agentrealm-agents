"""Free-play run 9 offline: Heal's regen measure walk held off the plan (A84, A10).

At 9/10 health Heal walked to measure regen and circled for 217 s between
safe tiles it could not reach, giving each up and moving on to the next,
while the planner's ``travel`` to town never ran. Now safe ground (walk,
rest, regen sample) outranks a plan op in progress only when health is low,
and the walk commits to one reachable safe tile: when it gives that tile up,
Heal rules it out as Retreat and Park do, gives safe ground up until the next
full heal, asks the planner for supplies, and yields.
"""

from __future__ import annotations

import random
import unittest

from agentrealm_agent.config import Policy
from agentrealm_agent.directives import PARAM_DEFAULTS
from agentrealm_agent.memory import Memory
from agentrealm_agent.navigation import stuck as nav_stuck
from agentrealm_agent.pathing import SAFE_UNREACHABLE_TICKS
from agentrealm_agent.plan import Plan
from agentrealm_agent.states import dispatch
from agentrealm_agent.states.base import PlayContext
from agentrealm_agent.world import WorldModel
from tests.test_freeplay_run3 import safe
from tests.test_survival_runs import MAP, world

# A cluster of safe tiles west of the agent at (10, 10), as run 9's at (389–392, 393–394).
CLUSTER = [(4, 10), (4, 11), (3, 10), (3, 11)]
TRAVEL = {"op": "travel", "to": "point", "x": 30, "y": 10}


def ctx(plan: Plan | None = None) -> PlayContext:
    policy = Policy(kind="scripted", goals=[], hostile=["npc"])
    return PlayContext(Memory(), policy, random.Random(0), params=dict(PARAM_DEFAULTS), plan=plan)


def travel_plan() -> Plan:
    return Plan([dict(TRAVEL)], dict(PARAM_DEFAULTS))


def wall_in(w: WorldModel, cells: list[tuple[int, int]]) -> None:
    """A wall ring round ``cells``: no way in from any side."""
    xs, ys = [p[0] for p in cells], [p[1] for p in cells]
    for x in range(min(xs) - 1, max(xs) + 2):
        for y in range(min(ys) - 1, max(ys) + 2):
            if (x, y) not in cells:
                w.view.tiles[(x, y)] = "wall"


def stand_still(w: WorldModel, c: PlayContext, ticks: int, every: int = 10) -> list:
    """Decide every ``every`` ticks while no step lands: each walk goes nowhere."""
    outs = []
    for _ in range(ticks // every):
        outs.append(dispatch(w, c))
        w.tick += every
    return outs


def measure_targets(outs: list) -> list[str]:
    return [o.reason for o in outs if o.state == "Heal" and o.reason.startswith("heal_measure")]


class MeasureYieldsToThePlanTest(unittest.TestCase):
    def test_at_9_of_10_the_plans_travel_goes_first(self):
        w, c = world(health=9), ctx(travel_plan())
        safe(w, *CLUSTER)
        out = dispatch(w, c)
        self.assertEqual(out.state, "Travel")
        self.assertIn("Heal: health not low: the plan's travel goes first", out.yielded)

    def test_run_9_unreachable_safe_tiles_at_9_of_10_yield_to_the_plan(self):
        w, c = world(health=9), ctx(travel_plan())
        safe(w, *CLUSTER)
        wall_in(w, CLUSTER)
        outs = stand_still(w, c, 6 * nav_stuck.PROGRESS_TICK_LIMIT)
        self.assertEqual(measure_targets(outs), [])
        self.assertEqual(outs[0].state, "Travel")
        # Standing still, the travel op stalls out and the safe default takes
        # over; Heal never walks for the measure meanwhile.
        self.assertNotIn("Heal", {o.state for o in outs})

    def test_low_health_still_rests_before_the_plan(self):
        w, c = world(health=4), ctx(travel_plan())
        safe(w, *CLUSTER)
        out = dispatch(w, c)
        self.assertEqual((out.state, out.reason), ("Heal", f"heal_measure → {CLUSTER[0]}"))

    def test_in_a_safe_zone_at_9_of_10_the_plan_goes_first(self):
        w, c = world(health=9), ctx(travel_plan())
        safe(w, (10, 10), *CLUSTER)
        out = dispatch(w, c)
        self.assertEqual(out.state, "Travel")
        self.assertIsNone(c.memory.heal_regen_sample, "no sample while the plan's op runs")

    def test_with_no_plan_op_it_still_measures(self):
        w, c = world(health=9), ctx()
        safe(w, *CLUSTER)
        out = dispatch(w, c)
        self.assertEqual((out.state, out.reason), ("Heal", f"heal_measure → {CLUSTER[0]}"))


class OneSafeTileTest(unittest.TestCase):
    def test_walled_in_safe_tiles_are_never_walked_to(self):
        w, c = world(health=4), ctx()
        safe(w, *CLUSTER)
        wall_in(w, CLUSTER)
        out = dispatch(w, c)
        self.assertNotEqual(out.state, "Heal")
        self.assertIn("Heal: no reachable safe tile", out.yielded)
        for cell in CLUSTER[:4]:
            self.assertIn((MAP, cell), c.memory.safe_unreachable)
        # Review on #178: the planner hears of it too, not only after a give-up.
        asks = [s for s in c.memory.strategist_signals if s["trigger"] == "heal_supplies"]
        self.assertEqual([a.get("safe_ground") for a in asks], ["unreachable"])

    def test_a_retreat_or_park_mark_on_the_committed_tile_ends_the_walk(self):
        # One reachability for every safe walk: a cell Retreat or Park ruled
        # out is out for Heal too, and counts as its one give-up.
        w, c = world(health=7), ctx()
        safe(w, *CLUSTER)
        self.assertEqual(dispatch(w, c).reason, f"heal_measure → {CLUSTER[0]}")
        c.memory.safe_unreachable[(MAP, CLUSTER[0])] = w.tick  # as retreat.no_progress marks it
        w.tick += 10
        out = dispatch(w, c)
        self.assertNotEqual(out.state, "Heal")
        self.assertEqual(c.memory.heal_safe_given_up, (MAP, w.tick))
        self.assertIn("Heal: safe tile given up: safe ground given up for now", out.yielded)

    def test_a_run_of_unreachable_tiles_ends_after_the_first(self):
        # Reachable by budget, never nearer: the old walk gave each tile up
        # in turn and started on the next, then came round again. Health not
        # low: given up until a full heal.
        w, c = world(health=7), ctx()
        safe(w, *CLUSTER)
        outs = stand_still(w, c, 6 * nav_stuck.PROGRESS_TICK_LIMIT)
        self.assertEqual(set(measure_targets(outs)), {f"heal_measure → {CLUSTER[0]}"})
        self.assertIn((MAP, CLUSTER[0]), c.memory.safe_unreachable)
        self.assertEqual(c.memory.heal_safe_given_up[0], MAP)
        self.assertNotEqual(outs[-1].state, "Heal")
        self.assertIn("Heal: safe ground given up for now", outs[-1].yielded)
        asks = [s for s in c.memory.strategist_signals if s["trigger"] == "heal_supplies"]
        self.assertEqual(len(asks), 1)
        self.assertEqual(asks[0]["safe_ground"], "unreachable")
        self.assertNotIn("regen", asks[0], "regen was never measured")

    def test_a_full_heal_lets_the_next_hurt_spell_walk_again(self):
        w, c = world(health=4), ctx()
        safe(w, *CLUSTER)
        stand_still(w, c, nav_stuck.PROGRESS_TICK_LIMIT + 20)
        self.assertEqual(c.memory.heal_safe_given_up[0], MAP)
        w.health = 10
        dispatch(w, c)
        self.assertIsNone(c.memory.heal_safe_given_up)
        w.health = 4
        out = dispatch(w, c)
        self.assertEqual(out.state, "Heal")
        self.assertTrue(out.reason.startswith("heal_measure → "), out.reason)
        self.assertNotEqual(out.reason, f"heal_measure → {CLUSTER[0]}", "the given-up tile is still ruled out")

    def test_at_low_health_one_tile_is_tried_again_after_the_give_up_lapses(self):
        # Review on #178: a give-up that never lapsed shut every safe tile out
        # for the spell, even reachable ones, with no other way to heal.
        w, c = world(health=4), ctx()
        safe(w, *CLUSTER)
        outs = stand_still(w, c, nav_stuck.PROGRESS_TICK_LIMIT + 20)
        given_up = c.memory.heal_safe_given_up[1]
        self.assertEqual(set(measure_targets(outs)), {f"heal_measure → {CLUSTER[0]}"})
        w.tick = given_up + SAFE_UNREACHABLE_TICKS - 1
        self.assertNotEqual(dispatch(w, c).state, "Heal", "still given up")
        w.tick = given_up + SAFE_UNREACHABLE_TICKS
        out = dispatch(w, c)
        self.assertEqual(out.state, "Heal")
        self.assertTrue(out.reason.startswith("heal_measure → "), out.reason)

    def test_not_low_the_give_up_holds_past_the_lapse(self):
        w, c = world(health=7), ctx()
        safe(w, *CLUSTER)
        stand_still(w, c, nav_stuck.PROGRESS_TICK_LIMIT + 20)
        w.tick = c.memory.heal_safe_given_up[1] + 3 * SAFE_UNREACHABLE_TICKS
        self.assertNotEqual(dispatch(w, c).state, "Heal")
        w.health = 4  # now low: one tile again
        self.assertEqual(dispatch(w, c).state, "Heal")


if __name__ == "__main__":
    unittest.main()
