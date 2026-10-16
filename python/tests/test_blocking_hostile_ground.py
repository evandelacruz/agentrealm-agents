"""A151: a hostile's ground that blocks every way is passed or cleared, seen or remembered.

On a dark level that is mostly water, a bogmaw keeps its post on an island,
and the ground it holds (its reach grown by the hits it dealt) covers every
way to a door. A walk on an op's behalf, the internal ``leave_level`` walk
included, passes that ground when health and the heals carried cover the
hits expected on the pass and stay above Retreat's floor, whether the bogmaw
is in view or only remembered; otherwise it asks the planner, naming it.
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
POST = (6, 5)  # the bogmaw's island post
START = (11, 10)  # on open water, the raft armed
EXIT, SOUTH = (7, 0), (7, 11)  # the level's two doors


def live(health: int = 10, seen: bool = True, held=("bronze_sword",)) -> WorldModel:
    """The live level, all 192 cells known: water with tile islands at
    (4–6, 3–5), (11–12, 4–6) and (6–8, 8–10), tile at (7, 1), doors at (7, 0)
    and (7, 11); the raft armed, a bogmaw at its post (6, 5) that has come
    out 4 cells for us, so the ground it holds covers every way to (7, 0)."""
    from agentrealm_adk.item_table import InventorySupply

    w = LiveLevelMapTest().live(START, 4, LiveLevelMapTest.ROWS)
    w.worn_codes = {}
    w.held_supplies = [InventorySupply(8 + i, code) for i, code in enumerate(held)]
    w.health, w.max_health = health, 10
    w.threat.record(("npc", "bogmaw"), 2)
    bogmaw = Entity(*HOSTILE, POST, code="bogmaw")
    w.sightings[HOSTILE] = Sighting(bogmaw, 44, w.tick, POST, post=True, reach=4, in_view=seen)
    w.entities = [bogmaw] if seen else []
    return w


def ctx(op: dict, kb: KnowledgeBase | None = None) -> PlayContext:
    plan = Plan([dict(op)], dict(PARAM_DEFAULTS))
    return PlayContext(Memory(), POLICY, random.Random(0), knowledge=kb or KnowledgeBase.empty("sandbox"), plan=plan)


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
    def test_seen_or_remembered_the_walk_to_either_door_gets_there(self):
        for seen in (True, False):
            for door in (EXIT, SOUTH):
                with self.subTest(seen=seen, door=door):
                    w = live(seen=seen)
                    c = ctx({"op": "travel", "to": "point", "x": door[0], "y": door[1]})
                    states = walk(w, c, door)
                    self.assertEqual(w.pos, door, (states, asks(c)))
                    self.assertEqual(asks(c), [])

    def test_leave_level_reaches_the_door(self):
        for seen in (True, False):
            with self.subTest(seen=seen):
                w = live(seen=seen)
                w.levels_cleared = [1, 2]  # level 3 is the target: level 4 is left
                kb = kb_with(1, (5, 5))
                record_warp(kb, 1, (5, 5), "framed_door", 44, (7, 1))  # came in by (7, 0)
                c = ctx({"op": "explore_area", "x": 0, "y": 0, "radius": 3}, kb)
                states = walk(w, c, EXIT)
                self.assertEqual(w.pos, EXIT, (states, asks(c)))
                self.assertIn("Level", states)
                self.assertEqual(asks(c), [])


class UncoveredPassAsksTest(unittest.TestCase):
    def test_at_low_health_it_asks_naming_the_hostile(self):
        for seen, named in ((True, "hostile bogmaw 200 at 6,5"), (False, "remembered hostile bogmaw 200, last seen at 6,5")):
            with self.subTest(seen=seen):
                w = live(health=7, seen=seen)
                c = ctx({"op": "travel", "to": "point", "x": EXIT[0], "y": EXIT[1]})
                out = dispatch(w, c)
                self.assertFalse(out.intents, out.reason)
                (ask,) = asks(c)
                self.assertIn(named, ask["blocked_by"])
                self.assertNotIn("no known way", ask["blocked_by"])

    def test_heals_carried_on_water_with_the_raft_armed_do_not_count(self):
        """The pass to (7, 0) crosses water with the raft only armed: no
        potion can be drunk there (``would_strand``, A133), so one carried
        does not cover it, and it asks."""
        w = live(health=7, held=("bronze_sword", "large_potion"))
        c = ctx({"op": "travel", "to": "point", "x": EXIT[0], "y": EXIT[1]})
        self.assertFalse(dispatch(w, c).intents)
        self.assertEqual(len(asks(c)), 1)


class LandsToFightTest(unittest.TestCase):
    def test_a_pass_not_covered_heads_for_land_beside_the_hostile(self):
        """The pass is not covered, but the engagement would fight the bogmaw
        from its island: the walk heads for land beside it, never coming
        within its range on water, and never asks."""
        from unittest import mock

        from agentrealm_adk.fording import on_water

        w = live()
        c = ctx({"op": "travel", "to": "point", "x": EXIT[0], "y": EXIT[1]})
        with mock.patch("agentrealm_adk.pathing.covers_pass", return_value=False):
            out = dispatch(w, c)
            self.assertIn("land at", out.reason)
            for _ in range(30):
                (step,) = [(i["x"], i["y"]) for i in out.intents or [] if i["verb"] == "SetPosition"][:1] or [None]
                if step is None:
                    break
                self.assertFalse(
                    on_water(w, c.memory, step) and max(abs(step[0] - POST[0]), abs(step[1] - POST[1])) <= POLICY.hostile_range,
                    f"{step}: on water within the bogmaw's range",
                )
                step_to(w, c.memory, step)
                if w.pos == EXIT:
                    break
                out = dispatch(w, c)
        self.assertEqual(asks(c), [])

    def test_landed_beside_it_fight_arms_the_sword(self):
        """One step off its island the walk lands, and ashore Fight arms
        the sword (A133) and closes."""
        from unittest import mock

        from agentrealm_adk.fording import on_water
        from agentrealm_agent.states.base import op_walk
        from agentrealm_agent.states.way import land_to_fight

        w = live()
        w.pos = (3, 6)  # on water, 3 from the bogmaw: out of its range
        c = ctx({"op": "travel", "to": "point", "x": EXIT[0], "y": EXIT[1]})
        with mock.patch("agentrealm_adk.pathing.covers_pass", return_value=False):
            out = land_to_fight(w, c, EXIT, "Travel", op_walk(w, c)[2])
        self.assertEqual(out.intents, [{"verb": "SetPosition", "x": 4, "y": 5}], out.reason)
        step_to(w, c.memory, (4, 5))
        self.assertFalse(on_water(w, c.memory, w.pos))
        out = dispatch(w, c)
        self.assertEqual(out.state, "Fight", out.reason)
        self.assertEqual(out.intents[0], {"verb": "Arm", "supply_id": 8}, "the sword armed for the fight")

    def test_with_neither_it_asks(self):
        from unittest import mock

        w = live()
        c = ctx({"op": "travel", "to": "point", "x": EXIT[0], "y": EXIT[1]})
        with mock.patch("agentrealm_adk.pathing.covers_pass", return_value=False), mock.patch(
            "agentrealm_agent.states.way.would_fight", return_value=False
        ):
            self.assertFalse(dispatch(w, c).intents)
        self.assertIn("hostile bogmaw 200 at 6,5", asks(c)[0]["blocked_by"])


class LandToFightExitsTest(unittest.TestCase):
    """``way.land_to_fight`` lands only for one hostile in view, not
    forbidden, with land in its range to fight from; else it gives None and
    the executor asks."""

    def land(self, w: WorldModel, c: PlayContext | None = None):
        from unittest import mock

        from agentrealm_agent.states.base import op_walk
        from agentrealm_agent.states.way import land_to_fight

        c = c or ctx({"op": "travel", "to": "point", "x": EXIT[0], "y": EXIT[1]})
        with mock.patch("agentrealm_adk.pathing.covers_pass", return_value=False):
            return land_to_fight(w, c, EXIT, "Travel", op_walk(w, c)[2])

    def test_it_lands_for_the_one_hostile_in_view(self):
        self.assertIsNotNone(self.land(live()))

    def test_not_for_a_forbidden_hostile(self):
        c = ctx({"op": "travel", "to": "point", "x": EXIT[0], "y": EXIT[1]})
        c.never_attack = ["bogmaw"]
        self.assertIsNone(self.land(live(), c))

    def test_not_for_a_remembered_hostile(self):
        self.assertIsNone(self.land(live(seen=False)), "out of view the engagement cannot weigh it")

    def test_not_when_two_hostiles_shut_the_way(self):
        w = live()
        w.entities.append(Entity("npc", 201, (12, 1), code="bogmaw"))  # its ground on the way along row 1
        self.assertIsNone(self.land(w))

    def test_not_with_no_land_in_its_range(self):
        w = live()
        for x in range(4, 7):
            for y in range(3, 6):
                if (x, y) != POST:
                    w.view.tiles[(x, y)] = "water"  # its island shrunk to the cell it stands on
        self.assertIsNone(self.land(w))


class WhatBlocksTest(unittest.TestCase):
    def test_a_hostile_the_engagement_would_fight_is_named_when_its_range_reaches_the_way(self):
        """A hostile the engagement would fight is left out of the walk's
        ground (A128), but an ask that goes out for any reason still names
        it when its range reaches the way."""
        from unittest import mock

        from agentrealm_agent.states.ask import what_blocks

        w = live()
        c = ctx({"op": "travel", "to": "point", "x": 4, "y": 3})
        with mock.patch("agentrealm_adk.pathing.would_fight", return_value=True):
            self.assertIn("hostile bogmaw 200 at 6,5", what_blocks(w, c, (4, 3)))

    def test_a_remembered_hostile_is_named_with_where_it_was_last_seen(self):
        from agentrealm_agent.states.ask import what_blocks

        w = live(seen=False)
        c = ctx({"op": "travel", "to": "point", "x": EXIT[0], "y": EXIT[1]})
        blocked = what_blocks(w, c, EXIT)
        self.assertIn("remembered hostile bogmaw 200, last seen at 6,5, out of view, holding ground round 6,5", blocked)


class RememberedGroundStillBarsTest(unittest.TestCase):
    def test_with_a_way_round_the_walk_keeps_out_of_it(self):
        """A remembered hostile's ground is passed only when it is the only
        way: a walk with a way round keeps to it, as before."""
        from agentrealm_adk.pathing import op_walk_sets

        w = live(seen=False)
        w.sightings[HOSTILE].reach = 0  # its post's ground alone: (3..9, 2..8)
        c = ctx({"op": "travel", "to": "point", "x": EXIT[0], "y": EXIT[1]})
        reach = op_walk_sets(w, c.memory, POLICY, None, dict(PARAM_DEFAULTS), [])[2]
        self.assertIn((7, 4), reach)
        self.assertFalse(reach.only_way(SOUTH))
        c = ctx({"op": "travel", "to": "point", "x": SOUTH[0], "y": SOUTH[1]})
        while w.pos != SOUTH:
            out = dispatch(w, c)
            (step,) = [(i["x"], i["y"]) for i in out.intents if i["verb"] == "SetPosition"][:1]
            self.assertNotIn(step, reach, "kept out of the ground with a way round")
            step_to(w, c.memory, step)


if __name__ == "__main__":
    unittest.main()
