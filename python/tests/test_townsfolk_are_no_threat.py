"""A9, A23: every place that keeps clear of a threat uses the one hostility
test (``survival.is_hostile``), so townsfolk never block a cut, repel a path,
close a frontier, hold off a heal, raise the poll rate or count as hostile in
State. Each test sets a known hostile beside a townsperson of a type never
seen attacking or dying."""

import random
import unittest

from agentrealm_agent.config import Policy
from agentrealm_agent.navigation.planner import hostile_cost
from agentrealm_agent.pathing import grid_params
from agentrealm_agent.memory import Memory
from agentrealm_agent.poll_cadence import hostile_within
from agentrealm_agent.states import PlayContext
from agentrealm_agent.states.explore import _look_around
from agentrealm_agent.states.gather import _barred_by_hostile
from agentrealm_agent.states.gather_safe import gather_ground, hostiles_near
from agentrealm_agent.states.greet import threat_near
from agentrealm_agent.states.heal import _wants_heal
from agentrealm_agent.strategist import npc_lines
from agentrealm_agent.world import Entity, WorldModel

POLICY = Policy(kind="scripted", hostile=["npc"], hostile_range=2)


def world(rows: list[str], at=(2, 1)) -> WorldModel:
    glyph = {".": "dirt", "g": "grass"}
    w = WorldModel(character_id=1, map_id=7, pos=at, perception=5)
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            w.view.tiles[(x, y)] = glyph[ch]
    w.terrain_center, w.terrain_map = at, 7
    w.attack_range = 1
    w.hostile_types.add(("npc", "gnawer"))  # a type seen attacking (survival.is_hostile)
    return w


def townsperson(pos) -> Entity:
    return Entity("npc", 5, pos, "villager")


def monster(pos) -> Entity:
    return Entity("npc", 6, pos, "gnawer")


class TownsfolkAreNoThreatTest(unittest.TestCase):
    def test_gather_cuts_beside_a_townsperson(self):
        w = world(["ggggg", "ggggg", "ggggg"])
        w.entities = [townsperson((3, 1))]
        self.assertTrue(gather_ground(w, (4, 1), POLICY))
        self.assertFalse(_barred_by_hostile(w, POLICY, set(), set()))
        w.entities = [monster((3, 1))]
        self.assertFalse(gather_ground(w, (4, 1), POLICY))
        self.assertTrue(_barred_by_hostile(w, POLICY, set(), set()))

    def test_a_townsperson_adds_no_path_danger(self):
        w = world([".......", ".......", "......."], at=(0, 1))
        path = [(x, 1) for x in range(1, 7)]
        params = grid_params(POLICY, set(), set())
        w.entities = [townsperson((3, 0))]
        self.assertEqual(hostile_cost(w, path, params), 0)
        w.entities = [monster((3, 0))]
        self.assertGreater(hostile_cost(w, path, params), 0)

    def test_explore_does_not_avoid_a_townsperson(self):
        w = world(["...", "...", "..."], at=(1, 1))
        w.entities = [townsperson((2, 1))]
        self.assertFalse(hostiles_near(w, (1, 0), POLICY))
        self.assertIn("look around", _look_around(w, POLICY, random.Random(1), set(), "Explore").reason)
        w.entities = [monster((2, 1))]
        self.assertTrue(hostiles_near(w, (1, 0), POLICY))
        self.assertIn("boxed in", _look_around(w, POLICY, random.Random(1), set(), "Explore").reason)

    def test_heal_starts_beside_a_townsperson(self):
        w = world(["...", "...", "..."], at=(1, 1))
        w.health, w.max_health = 5, 10
        ctx = PlayContext(Memory(), POLICY, random.Random(1))
        w.entities = [townsperson((2, 1))]
        self.assertTrue(_wants_heal(w, ctx))
        w.entities = [monster((2, 1))]
        self.assertFalse(_wants_heal(w, ctx))

    def test_a_townsperson_does_not_raise_the_poll_rate(self):
        w = world(["..."], at=(0, 0))
        w.entities = [townsperson((1, 0))]
        self.assertFalse(hostile_within(w, POLICY))
        w.entities = [monster((1, 0))]
        self.assertTrue(hostile_within(w, POLICY))

    def test_greet_sees_no_threat_in_a_townsperson(self):
        w = world(["...", "...", "..."], at=(1, 1))
        w.entities = [townsperson((2, 1))]
        self.assertFalse(threat_near(w, POLICY))
        w.entities = [monster((2, 1))]
        self.assertTrue(threat_near(w, POLICY))

    def test_state_marks_only_the_monster_hostile(self):
        w = world(["....."], at=(0, 0))
        w.entities = [townsperson((1, 0)), monster((2, 0))]
        rows = next(line for line in npc_lines(w, None) if line.startswith("nearby_npcs="))
        self.assertIn('"hostile": false, "id": 5', rows)
        self.assertIn('"hostile": true, "id": 6', rows)


if __name__ == "__main__":
    unittest.main()
