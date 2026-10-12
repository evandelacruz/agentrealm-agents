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

from agentrealm_agent.directives import PARAM_DEFAULTS
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.knowledge_maps import record_warp
from agentrealm_agent.memory import Memory
from agentrealm_agent.plan import Plan
from agentrealm_agent.states import PlayContext, dispatch
from agentrealm_agent.world import Entity, Sighting, WorldModel
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
    from agentrealm_agent.item_table import InventorySupply

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

    def test_heals_carried_count_toward_the_pass(self):
        w = live(health=7, held=("bronze_sword", "small_potion"))
        c = ctx({"op": "travel", "to": "point", "x": EXIT[0], "y": EXIT[1]})
        self.assertTrue(dispatch(w, c).intents, asks(c))


class LandsToFightTest(unittest.TestCase):
    def test_a_pass_not_covered_lands_beside_the_hostile_and_fights_it(self):
        """The pass is not covered, but the engagement would fight the bogmaw
        from its island: the walk lands there without coming within its
        range on water, and ashore Fight arms the sword and swings."""
        from unittest import mock

        from agentrealm_agent.fording import on_water

        w = live()
        c = ctx({"op": "travel", "to": "point", "x": EXIT[0], "y": EXIT[1]})
        with mock.patch("agentrealm_agent.pathing.covers_pass", return_value=False):
            for _ in range(30):
                out = dispatch(w, c)
                if any(i["verb"] in ("Use", "Arm") for i in out.intents or []):
                    break
                (step,) = [(i["x"], i["y"]) for i in out.intents or [] if i["verb"] == "SetPosition"][:1] or [None]
                self.assertIsNotNone(step, (out.reason, asks(c)))
                self.assertFalse(
                    on_water(w, c.memory, step) and max(abs(step[0] - POST[0]), abs(step[1] - POST[1])) <= POLICY.hostile_range,
                    f"{step}: on water within the bogmaw's range",
                )
                step_to(w, c.memory, step)
        self.assertEqual(out.state, "Fight", out.reason)
        self.assertFalse(on_water(w, c.memory, w.pos), "ashore")
        self.assertIn({"verb": "Arm", "supply_id": 8}, out.intents, "the sword armed for the fight")
        self.assertEqual(asks(c), [])

    def test_with_neither_it_asks(self):
        from unittest import mock

        w = live()
        c = ctx({"op": "travel", "to": "point", "x": EXIT[0], "y": EXIT[1]})
        with mock.patch("agentrealm_agent.pathing.covers_pass", return_value=False), mock.patch(
            "agentrealm_agent.states.way.would_fight", return_value=False
        ):
            self.assertFalse(dispatch(w, c).intents)
        self.assertIn("hostile bogmaw 200 at 6,5", asks(c)[0]["blocked_by"])


class RememberedGroundStillBarsTest(unittest.TestCase):
    def test_with_a_way_round_the_walk_keeps_out_of_it(self):
        """A remembered hostile's ground is passed only when it is the only
        way: a walk with a way round keeps to it, as before."""
        from agentrealm_agent.pathing import op_walk_sets

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
