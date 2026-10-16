"""A151, A162: a hostile's ground that blocks every way is passed or cleared, seen or remembered.

On a dark level that is mostly water, a bogmaw stands on land beside the
one water channel to a door, and the ground it holds covers the channel. It
chases (Manual §11 Hostiles): it steps toward the character and stands still
only where no step closes in. Where a wall keeps it from the channel
(``PASS``), a walk on an op's behalf, the internal ``leave_level`` walk
included, passes its ground when health and the heals carried cover the hits
expected while it is near and stay above Retreat's floor, whether the bogmaw
is in view or only remembered; otherwise it asks the planner, naming it.
Where the land runs round the wall's end to the channel (``LAND``), it comes
round to meet the walk, so no pass is covered: the walk lands where it first
comes within range, on land, and the engagement fights it there. A chaser
that would meet the walk on the water is never landed for.
"""

from __future__ import annotations

import random
import unittest

from agentrealm_adk.directives import PARAM_DEFAULTS
from agentrealm_adk.knowledge_base import KnowledgeBase
from agentrealm_adk.knowledge_maps import record_warp
from agentrealm_adk.memory import Memory
from agentrealm_agent.plan import Plan
from agentrealm_agent.states import PlayContext, dispatch
from agentrealm_adk.world import Entity, Sighting, WorldModel
from tests.test_fording import LiveLevelMapTest
from tests.test_hostile_reach import POLICY, step_to
from tests.test_level_order import kb_with

HOSTILE = ("npc", 200)
BOGMAW = (5, 5)  # on the land west of the channel
START = (8, 10)  # on the channel, the raft armed
DOOR = (8, 0)  # the level's door, at the channel's north end
# Land west of a wall (x 6–7) beside the water channel (x 8), its door at
# (8, 0). On ``LAND`` the land runs round the wall's north end (rows 1–2) to
# the channel; on ``PASS`` walls close it, so the bogmaw cannot reach it.
LAND = [
    "wwwwwwwwfwwwwwww",
    "wtttttttWwwwwwww",
    "wtttttttWwwwwwww",
    *["wtttttwwWwwwwwww"] * 8,
    "wwwwwwwwwwwwwwww",
]
PASS = [LAND[0], *["wwwwwwwwWwwwwwww"] * 2, *LAND[3:]]


def live(
    rows: list[str] = PASS, health: int = 10, seen: bool = True, held=("bronze_sword",), at=START, bogmaw=BOGMAW
) -> WorldModel:
    """The live level on ``rows``, the raft armed, the bogmaw on ``bogmaw``,
    still (blocked), in view or remembered. Remembered, the level is dark:
    sight 4, so its cell is out of sight from the start."""
    from agentrealm_adk.item_table import InventorySupply

    w = LiveLevelMapTest().live(at, 4, rows)
    w.worn_codes = {}
    w.held_supplies = [InventorySupply(8 + i, code) for i, code in enumerate(held)]
    w.health, w.max_health = health, 10
    w.threat.record(("npc", "bogmaw"), 2)
    e = Entity(*HOSTILE, bogmaw, code="bogmaw")
    w.sightings[HOSTILE] = Sighting(e, 44, w.tick, still=True, in_view=seen)
    w.entities = [e] if seen else []
    if not seen:
        w.perception = 4
    return w


def ctx(op: dict, kb: KnowledgeBase | None = None) -> PlayContext:
    plan = Plan([dict(op)], dict(PARAM_DEFAULTS))
    return PlayContext(Memory(), POLICY, random.Random(0), knowledge=kb or KnowledgeBase.empty("sandbox"), plan=plan)


def to_door() -> PlayContext:
    return ctx({"op": "travel", "to": "point", "x": DOOR[0], "y": DOOR[1]})


def walk(w: WorldModel, c: PlayContext, to: tuple[int, int], limit: int = 40) -> list[str]:
    """Dispatch and take each step until ``to`` is reached or nothing is sent."""
    states = []
    while w.pos != to and len(states) < limit:
        out = dispatch(w, c)
        states.append(out.state)
        steps = [i for i in out.intents or [] if i["verb"] == "SetPosition"]
        if not steps:
            break
        step_to(w, c.memory, (steps[0]["x"], steps[0]["y"]))
    return states


def asks(c: PlayContext) -> list[dict]:
    return [s for s in c.memory.strategist_signals if s["trigger"] == "no_way_on"]


class SoleBlockerIsPassedTest(unittest.TestCase):
    def test_seen_or_remembered_the_walk_gets_past_a_blocked_chaser(self):
        """Chased up the channel, the bogmaw keeps to its side of the wall,
        3 cells off: never in range, so the pass is made at full health."""
        for seen in (True, False):
            with self.subTest(seen=seen):
                w = live(seen=seen)
                c = to_door()
                states = walk(w, c, DOOR)
                self.assertEqual(w.pos, DOOR, (states, asks(c)))
                self.assertEqual(asks(c), [])

    def test_leave_level_reaches_the_door(self):
        for seen in (True, False):
            with self.subTest(seen=seen):
                w = live(seen=seen)
                w.levels_cleared = [1, 2]  # level 3 is the target: level 4 is left
                kb = kb_with(1, (5, 5))
                record_warp(kb, 1, (5, 5), "framed_door", 44, (8, 1))  # came in by (8, 0)
                c = ctx({"op": "explore_area", "x": 0, "y": 0, "radius": 3}, kb)
                states = walk(w, c, DOOR)
                self.assertEqual(w.pos, DOOR, (states, asks(c)))
                self.assertIn("Level", states)
                self.assertEqual(asks(c), [])


class UncoveredPassAsksTest(unittest.TestCase):
    def test_at_low_health_it_asks_naming_the_hostile(self):
        for seen, named in ((True, "hostile bogmaw 200 at 5,5"), (False, "remembered hostile bogmaw 200, last seen at 5,5")):
            with self.subTest(seen=seen):
                w = live(health=7, seen=seen)
                c = to_door()
                out = dispatch(w, c)
                self.assertFalse(out.intents, out.reason)
                (ask,) = asks(c)
                self.assertIn(named, ask["blocked_by"])
                self.assertNotIn("no known way", ask["blocked_by"])

    def test_heals_carried_on_water_with_the_raft_armed_do_not_count(self):
        """The pass is all water with the raft only armed: no potion can be
        drunk there (``would_strand``, A133), so one carried does not cover
        it, and it asks."""
        w = live(health=9, held=("bronze_sword", "large_potion"))
        c = to_door()
        self.assertFalse(dispatch(w, c).intents)
        self.assertEqual(len(asks(c)), 1)

    def test_a_chaser_that_comes_round_is_no_pass_at_any_health(self):
        """On ``LAND`` the bogmaw comes round the wall's end, within range of
        the channel's top: there the engagement says flee, so no pass."""
        from agentrealm_adk.pathing import op_walk_sets

        path = [(8, y) for y in range(9, -1, -1)]
        reach = op_walk_sets(live(PASS), Memory(), POLICY, None, dict(PARAM_DEFAULTS), [])[2]
        self.assertTrue(reach.passes(path, DOOR), "blocked behind the wall")
        reach = op_walk_sets(live(LAND), Memory(), POLICY, None, dict(PARAM_DEFAULTS), [])[2]
        self.assertTrue(reach.only_way(DOOR))
        self.assertFalse(reach.passes(path, DOOR), "it comes round to (6, 2)")


class LandsToFightTest(unittest.TestCase):
    def test_a_pass_not_covered_heads_for_land_where_it_comes_in_range(self):
        """The pass is not covered, but the engagement would fight the bogmaw
        from land: the walk heads for land, on a way where the chase first
        brings it into range ashore (round the wall's end, at (7, 2)), never
        on water, and never asks."""
        from agentrealm_adk.fording import on_water
        from agentrealm_agent.states.way import _meeting

        w = live(LAND)
        c = to_door()
        out = dispatch(w, c)
        self.assertIn("land at", out.reason)
        self.assertEqual(out.intents, [{"verb": "SetPosition", "x": 8, "y": 9}])
        met = _meeting(w, POLICY, w.entities[0], c.memory.path)
        self.assertEqual(met, (7, 2))
        self.assertFalse(on_water(w, c.memory, met))
        self.assertEqual(asks(c), [])

    def test_landed_where_it_comes_in_range_fight_arms_the_sword(self):
        """Ashore at the wall's end with the bogmaw come round beside it,
        Fight arms the sword (A133) and closes."""
        from agentrealm_adk.fording import on_water

        w = live(LAND, at=(7, 2), bogmaw=(6, 3))
        self.assertFalse(on_water(w, Memory(), w.pos))
        out = dispatch(w, to_door())
        self.assertEqual(out.state, "Fight", out.reason)
        self.assertEqual(out.intents[0], {"verb": "Arm", "supply_id": 8}, "the sword armed for the fight")

    def test_a_chaser_that_meets_the_walk_on_water_is_not_landed_for(self):
        """A146's island: the bogmaw holds a small island, and whichever
        land cell in its range a walk makes for, it steps to the shore and
        comes within range while the walk is still on the water, where the
        engagement never says fight: no landing there will do."""
        from agentrealm_adk.fording import on_water
        from agentrealm_adk.navigation import cost_path
        from agentrealm_adk.pathing import grid_params
        from agentrealm_agent.states.way import _meeting

        w = live(LiveLevelMapTest.ROWS, at=(11, 10), bogmaw=(6, 5))
        e = w.entities[0]
        island = [(x, y) for x in range(4, 7) for y in range(3, 6) if (x, y) != e.pos]
        for cell in island:
            with self.subTest(cell=cell):
                met = _meeting(w, POLICY, e, cost_path(w, cell, grid_params(POLICY, set(), set())))
                self.assertIsNotNone(met)
                self.assertTrue(on_water(w, Memory(), met), met)

    def test_with_neither_it_asks(self):
        from unittest import mock

        w = live(LAND)
        c = to_door()
        with mock.patch("agentrealm_agent.states.way.would_fight", return_value=False):
            self.assertFalse(dispatch(w, c).intents)
        self.assertIn("hostile bogmaw 200 at 5,5", asks(c)[0]["blocked_by"])


class LandToFightExitsTest(unittest.TestCase):
    """``way.land_to_fight`` lands only for one hostile in view, not
    forbidden, with land in its range to meet it on; else it gives None and
    the executor asks."""

    def land(self, w: WorldModel, c: PlayContext | None = None):
        from agentrealm_agent.states.base import op_walk
        from agentrealm_agent.states.way import land_to_fight

        c = c or to_door()
        return land_to_fight(w, c, DOOR, "Travel", op_walk(w, c)[2])

    def test_it_lands_for_the_one_hostile_in_view(self):
        self.assertIsNotNone(self.land(live(LAND)))

    def test_not_for_a_forbidden_hostile(self):
        c = to_door()
        c.never_attack = ["bogmaw"]
        self.assertIsNone(self.land(live(LAND), c))

    def test_not_for_a_remembered_hostile(self):
        self.assertIsNone(self.land(live(LAND, seen=False, at=(8, 4))), "out of view the engagement cannot weigh it")

    def test_not_when_two_hostiles_shut_the_way(self):
        w = live(LAND)
        w.entities.append(Entity("npc", 201, (5, 9), code="bogmaw"))  # its ground on the channel's south end
        self.assertIsNone(self.land(w))

    def test_not_with_no_land_in_its_range(self):
        w = live(LAND)
        for x in range(1, 8):
            for y in range(1, 11):
                if w.view.tiles.get((x, y)) == "tile" and (x, y) != BOGMAW:
                    w.view.tiles[(x, y)] = "water"  # its land shrunk to the cell it stands on
        self.assertIsNone(self.land(w))


class WhatBlocksTest(unittest.TestCase):
    def test_a_hostile_the_engagement_would_fight_is_named_when_its_range_reaches_the_way(self):
        """A hostile the engagement would fight is left out of the walk's
        ground (A128), but an ask that goes out for any reason still names
        it when its range reaches the way."""
        from unittest import mock

        from agentrealm_agent.states.ask import what_blocks

        w = live(LAND)
        c = ctx({"op": "travel", "to": "point", "x": 5, "y": 3})
        with mock.patch("agentrealm_adk.pathing.would_fight", return_value=True):
            self.assertIn("hostile bogmaw 200 at 5,5", what_blocks(w, c, (5, 3)))

    def test_a_remembered_hostile_is_named_with_where_it_was_last_seen(self):
        from agentrealm_agent.states.ask import what_blocks

        w = live(seen=False)
        blocked = what_blocks(w, to_door(), DOOR)
        self.assertIn("remembered hostile bogmaw 200, last seen at 5,5, out of view", blocked)


class RememberedGroundStillBarsTest(unittest.TestCase):
    def test_with_a_way_round_the_walk_keeps_out_of_it(self):
        """A remembered hostile's ground is passed only when it is the only
        way: a walk with a way round keeps to it, as before. A146's open
        water, the bogmaw last seen still on its island, out of sight."""
        from agentrealm_adk.pathing import op_walk_sets

        south = (7, 11)
        w = live(LiveLevelMapTest.ROWS, seen=False, at=(11, 10), bogmaw=(6, 5))
        c = ctx({"op": "travel", "to": "point", "x": south[0], "y": south[1]})
        reach = op_walk_sets(w, c.memory, POLICY, None, dict(PARAM_DEFAULTS), [])[2]
        self.assertIn((7, 4), reach)
        self.assertFalse(reach.only_way(south))
        while w.pos != south:
            out = dispatch(w, c)
            (step,) = [(i["x"], i["y"]) for i in out.intents if i["verb"] == "SetPosition"][:1]
            self.assertNotIn(step, reach, "kept out of the ground with a way round")
            step_to(w, c.memory, step)


if __name__ == "__main__":
    unittest.main()
