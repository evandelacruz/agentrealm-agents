"""A fight decided on holds, and two states never take turns for nothing (A136).

Rebuilt offline: a weak hostile stands still behind a corner, so the one
step that only looks closer ends beside a wall. Fight closes on it round the
corner and kills it, with no Flee in between; the engagement keeps a fight
it decided on until health reaches the floor or a new hostile joins; and the
oscillation guard benches the losing side of a flip that has no walk to give
up, and asks the planner when that does not settle it.
"""

from __future__ import annotations

import random
import unittest
from unittest import mock

from agentrealm_agent import engagement
from agentrealm_agent.config import Policy
from agentrealm_agent.directives import PARAM_DEFAULTS
from agentrealm_agent.engagement import Engagement, sync_engagement
from agentrealm_agent.memory import Memory
from agentrealm_agent.navigation import oscillation
from agentrealm_agent.states import dispatch
from agentrealm_agent.states.base import PlayContext
from agentrealm_agent.states.fight import close_step, held_target
from agentrealm_agent.world import Entity, WorldModel, chebyshev

MAP = 1
CODE = "fake_biter"
POST = (13, 10)  # where the biter stands, and never leaves
CORNER = (12, 11)  # the one cell beside it a greedy step from (11, 12) would take


def world(at=(11, 12), health=10) -> WorldModel:
    """Open dirt with a stone at ``CORNER``: from ``at`` a step closer exists
    (to nowhere nearer), and the way in is round the stone."""
    w = WorldModel(character_id=1, map_id=MAP, pos=at, perception=8, health=health, max_health=10, lives=9)
    for x in range(30):
        for y in range(30):
            w.view.tiles[(x, y)] = "dirt"
    w.view.tiles[CORNER] = "stone"
    w.terrain_center, w.terrain_map = at, MAP
    w.hostile_types.add(("npc", CODE))
    w.attack_range = 1
    w.armed_code = "sword"
    return w


def ctx() -> PlayContext:
    policy = Policy(kind="scripted", goals=["explore"], on_hostile="fight", hostile=["npc"])
    return PlayContext(Memory(), policy, random.Random(0), params=dict(PARAM_DEFAULTS))


def biter(npc_id=7, at=POST) -> Entity:
    return Entity("npc", npc_id, at, code=CODE)


class StandingHostileTest(unittest.TestCase):
    """A weak hostile that never moves is closed on and fought to the end."""

    def test_close_step_walks_round_the_corner(self):
        w = world(at=(12, 12))
        w.view.tiles[(13, 11)] = "stone"
        w.view.tiles[(12, 11)] = "stone"
        # Every cell beside (12, 12) nearer the biter is shut; the walk goes round.
        step = close_step(w, biter(), set())
        self.assertIsNotNone(step)
        self.assertNotIn(step, {(12, 11), (13, 11)})

    def test_a_walk_that_loops_out_can_close_from_every_cell_of_it(self):
        """The only way in leaves the straight line by more than the gap it
        started at: one step in, then out to 9 cells off and back. Every cell
        of the walk can still close (the ground searched is fixed round the
        target, never measured from where Fight stands)."""
        w = world(at=(15, 20))
        for x in range(30):
            for y in range(30):
                w.view.tiles[(x, y)] = "stone"
        corridor = [(15, 20), (16, 20)] + [(16, y) for y in range(19, 10, -1)]
        corridor += [(x, 11) for x in range(17, 21)] + [(20, y) for y in range(12, 20)]
        for p in corridor:
            w.view.tiles[p] = "dirt"
        npc = biter(at=(20, 20))
        w.entities = [npc]
        for _ in range(40):
            if chebyshev(w.pos, npc.pos) <= 1:
                break
            step = close_step(w, npc, set())
            self.assertIsNotNone(step, f"no walk in from {w.pos}")
            w.pos = step
        self.assertLessEqual(chebyshev(w.pos, npc.pos), 1)

    def test_a_walk_in_crosses_water_only_with_a_fording_supply_worn(self):
        """The walk in is over ground Fight can swing from (A146): a raft held
        or armed leaves no weapon on water, so only one worn opens it (A133)."""
        from agentrealm_agent.item_table import InventorySupply

        w = world(at=(5, 10))
        for y in range(30):
            for x in (7, 8, 9):
                w.view.tiles[(x, y)] = "water"
        npc = biter(at=(11, 10))
        w.entities = [npc]
        self.assertIsNone(close_step(w, npc, set()), "no way over water without one")
        w.held_supplies = [InventorySupply(7, "raft")]
        self.assertIsNone(close_step(w, npc, set()), "held, it would be armed in the hand: no swing there")
        w.held_supplies, w.armed_code = [], "raft"
        self.assertIsNone(close_step(w, npc, set()), "armed, the same")
        w.armed_code, w.worn_codes = "bronze_sword", {"accessory": "raft"}
        step = close_step(w, npc, set())
        self.assertIsNotNone(step, "worn, the hand is free for the sword")
        self.assertEqual(step[0], 6, "toward the water")

    def test_it_is_closed_on_and_killed_with_no_flee_between(self):
        w, c = world(), ctx()
        npc = biter()
        w.entities = [npc]
        states, swung = [], 0
        for _ in range(40):
            w.tick += 5
            out = dispatch(w, c)
            states.append(out.state)
            uses = [i for i in out.intents or [] if i["verb"] == "Use"]
            if uses:
                swung += 1
                c.memory.last_use_tick = w.tick
                if swung == 3:
                    w.entities = []  # it went down
                    break
            elif out.intents and out.intents[0]["verb"] == "SetPosition":
                w.pos = (out.intents[0]["x"], out.intents[0]["y"])
        self.assertEqual(swung, 3, states)
        self.assertNotIn("Flee", states)
        self.assertEqual(set(states), {"Fight"}, states)
        self.assertNotEqual(dispatch(w, c).state, "Fight")


class FightHoldsTest(unittest.TestCase):
    """Once it says fight, the decision holds on the same facts (A136)."""

    def test_a_drop_in_the_odds_alone_does_not_turn_a_fight(self):
        w, c = world(at=(12, 10)), ctx()
        w.entities = [biter()]
        self.assertTrue(sync_engagement(w, c.memory, c.policy, c.params).fight)
        with mock.patch.object(engagement, "win_ratio", return_value=0.1):
            e = sync_engagement(w, c.memory, c.policy, c.params)
        self.assertTrue(e.fight, "the same hostile, health above the floor: the fight holds")
        self.assertEqual(e.ratio, 0.1)

    def test_health_at_the_floor_turns_it(self):
        w, c = world(at=(12, 10)), ctx()
        w.entities = [biter()]
        self.assertTrue(sync_engagement(w, c.memory, c.policy, c.params).fight)
        w.health = 1
        with mock.patch.object(engagement, "win_ratio", return_value=0.1):
            self.assertFalse(sync_engagement(w, c.memory, c.policy, c.params).fight)

    def test_a_fight_with_no_weighed_hostile_in_view_ends(self):
        w, c = world(at=(12, 10)), ctx()
        w.entities = [biter()]
        self.assertTrue(sync_engagement(w, c.memory, c.policy, c.params).fight)
        w.attacked_tick = w.tick  # still hitting us, so the engagement goes on
        w.entities = []
        e = sync_engagement(w, c.memory, c.policy, c.params)
        self.assertIsNotNone(e)
        self.assertFalse(e.fight, "nothing it weighs is left to fight")

    def test_running_found_not_to_work_keeps_what_was_weighed(self):
        w, c = world(at=(12, 10)), ctx()
        w.entities = [biter(), biter(8, (16, 16))]
        sync_engagement(w, c.memory, c.policy, c.params)
        weighed = c.memory.engagement.weighed
        w.entities = [biter()]  # one weighed member out of view now
        e = engagement.cannot_outrun(w, c.memory, c.policy, c.params)
        self.assertTrue(e.cannot_outrun)
        self.assertLessEqual(weighed, e.weighed)

    def test_a_second_hostile_can_still_send_it_running(self):
        w, c = world(at=(12, 10)), ctx()
        w.entities = [biter()]
        self.assertTrue(sync_engagement(w, c.memory, c.policy, c.params).fight)
        w.entities.append(biter(8, (11, 10)))
        e = sync_engagement(w, c.memory, c.policy, c.params)
        self.assertFalse(e.fight, "a pair is more than the sword takes at the margin")
        self.assertEqual(dispatch(w, c).state, "Flee")


def pace(m: Memory, w: WorldModel, moves: list[str], cells=((5, 5), (6, 5))) -> dict | None:
    """Run the guard over decisions whose moves came from ``moves`` in turn,
    stepping between ``cells``: the last event, if any."""
    event = None
    for i, state in enumerate(moves):
        w.tick += 5
        w.pos = cells[i % 2]
        event = oscillation.check(m, w) or event
        m.nav_stuck.last_move = ("", state, "")  # no walk's
    return event


class FlipGuardTest(unittest.TestCase):
    """Two states pacing with no walk are settled by the guard (A136)."""

    def setUp(self):
        self.w = WorldModel(character_id=1, map_id=MAP, pos=(5, 5), health=10, max_health=10)
        self.m = Memory()
        self.m.engagement = Engagement(frozenset({("npc", 7)}), fight=True)

    def test_an_unwalked_fight_flee_flip_benches_flee_within_the_window(self):
        event = pace(self.m, self.w, ["Fight", "Flee"] * 4)
        self.assertIsNotNone(event)
        self.assertEqual(event["unwalked"], ["Fight", "Flee"])
        self.assertEqual(event["benched"], "Flee")
        self.assertTrue(oscillation.benched(self.m, self.w, "Flee"))
        self.assertFalse(oscillation.benched(self.m, self.w, "Fight"))
        self.w.tick = event["benched_until"]
        self.assertFalse(oscillation.benched(self.m, self.w, "Flee"), "the cool-down ends")

    def test_while_it_says_flee_fight_is_benched(self):
        self.m.engagement = Engagement(frozenset({("npc", 7)}), fight=False)
        self.assertEqual(pace(self.m, self.w, ["Fight", "Flee"] * 4)["benched"], "Fight")

    def test_a_turn_in_the_decision_lifts_the_bench(self):
        pace(self.m, self.w, ["Fight", "Flee"] * 4)
        self.assertTrue(oscillation.benched(self.m, self.w, "Flee"))
        self.m.engagement = Engagement(frozenset({("npc", 7), ("npc", 8)}), fight=False)
        self.assertFalse(oscillation.benched(self.m, self.w, "Flee"), "a new danger may still flee")

    def test_retreat_is_never_benched(self):
        """Retreat runs at the survival floor: with no engagement, a flip with
        it benches the other side."""
        self.m.engagement = None
        event = pace(self.m, self.w, ["Fight", "Retreat"] * 4)
        self.assertEqual(event["benched"], "Fight")
        self.assertFalse(oscillation.benched(self.m, self.w, "Retreat"))
        self.m.nav_stuck.benched["Retreat"] = oscillation.nav_stuck.Bench(self.w.tick + 100, None)
        self.assertFalse(oscillation.benched(self.m, self.w, "Retreat"))

    def test_fight_is_never_benched_while_it_says_fight(self):
        """Fight carries out the decision: a flip with another state benches that state."""
        event = pace(self.m, self.w, ["Fight", "Gather"] * 4)
        self.assertEqual(event["benched"], "Gather")
        self.assertFalse(oscillation.benched(self.m, self.w, "Fight"))

    def test_only_the_deciding_sides_pacing_asks_the_planner_at_once(self):
        """Fight (the decision) and Retreat (the floor) may neither be benched:
        the planner is asked on the first flip."""
        event = pace(self.m, self.w, ["Fight", "Retreat"] * 4)
        self.assertNotIn("benched", event)
        self.assertTrue(event.get("asked"))
        self.assertFalse(oscillation.benched(self.m, self.w, "Fight"))
        self.assertEqual([s["trigger"] for s in self.m.strategist_signals], ["pacing"])

    def test_with_no_side_to_bench_the_planner_is_asked_once_per_episode(self):
        for _ in range(4):
            pace(self.m, self.w, ["Fight", "Retreat"] * 4)
        asks = [s for s in self.m.strategist_signals if s["trigger"] == "pacing"]
        self.assertEqual(len(asks), 1, asks)
        self.assertIsNone(asks[0]["benched"])
        self.w.tick += 3 * oscillation.FLIP_COOLDOWN_TICKS  # the episode is over
        pace(self.m, self.w, ["Fight", "Retreat"] * 4)
        self.assertEqual(len([s for s in self.m.strategist_signals if s["trigger"] == "pacing"]), 2)

    def test_a_swing_in_the_window_is_progress(self):
        self.m.last_use_tick = self.w.tick + 12
        event = pace(self.m, self.w, ["Fight", "Flee"] * 4)
        self.assertIsNotNone(event)
        self.assertNotIn("benched", event)

    def test_a_flip_the_bench_does_not_settle_asks_the_planner(self):
        first = pace(self.m, self.w, ["Fight", "Flee"] * 4)
        self.assertNotIn("asked", first)
        self.assertFalse([s for s in self.m.strategist_signals if s["trigger"] == "pacing"])
        again = pace(self.m, self.w, ["Fight", "Flee"] * 4)
        self.assertTrue(again.get("asked"))
        asks = [s for s in self.m.strategist_signals if s["trigger"] == "pacing"]
        self.assertEqual(len(asks), 1)
        self.assertEqual(asks[0]["states"], ["Fight", "Flee"])

    def test_dispatch_skips_a_benched_state(self):
        """Walled off from the biter, a fight decided on cannot close: Flee
        would run, but it lost the flip and stays off the pick."""
        w, c = world(at=(12, 10)), ctx()
        w.entities = [biter()]
        sync_engagement(w, c.memory, c.policy, c.params, lambda e: held_target(w, c, e))
        w.pos = (11, 10)
        for p in [(10, 9), (10, 10), (10, 11), (11, 9), (11, 11), (12, 9), (12, 10), (12, 11)]:
            w.view.tiles[p] = "stone"
        self.assertEqual(dispatch(w, c).state, "Flee")
        c.memory.nav_stuck.benched["Flee"] = oscillation.nav_stuck.Bench(w.tick + 100, True)
        self.assertNotEqual(dispatch(w, c).state, "Flee")
        self.assertLessEqual(chebyshev(w.pos, POST), 2)


if __name__ == "__main__":
    unittest.main()
