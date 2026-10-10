"""Free-play run 8 offline: Gather stood off a far hostile, and Detour walked a hurt character to a pack (A82).

1. Gather stalled for 177 s with no cut: every known cell to cut was barred
   by a hostile 10–13 cells away, so it sent nothing, and the safe default,
   hurt, stood off the hostile. Gather now walks on to the nearest frontier
   clear of every known hostile (``CLEAR``) and keeps working; the safe
   default's keep-away holds only while a hostile is closing in
   (``test_freeplay_run6``).
2. Detour walked a character at 4/10 to a pile beside a pack, and it
   dropped to 1/10. A find near a known hostile is taken only while health
   covers one hit from each of them above Retreat's floor
   (``detour.risk_allowed``).
3. A planner reply that was not JSON cleared the stack: see
   ``test_strategist.UnreadableReplyTest``.
"""

from __future__ import annotations

import random
import unittest
from unittest import mock

from agentrealm_agent.config import Policy
from agentrealm_agent.directives import PARAM_DEFAULTS
from agentrealm_agent.memory import Memory
from agentrealm_agent.plan import Plan
from agentrealm_agent.states import PlayContext
from agentrealm_agent.states.detour import RISK_RADIUS, DetourState, detour_find, risk_allowed
from agentrealm_agent.states import gather as gather_module
from agentrealm_agent.states.gather import BLOCKED, CLEAR, WALKING, WALK_TARGETS, gather_outcome
from agentrealm_agent.states.gather_safe import route_clear
from agentrealm_agent.hostile_ground import GATHER_HOSTILE_RADIUS, danger
from agentrealm_agent.world import Entity, WorldModel, chebyshev

MAP = 1
PACK = ("npc", "fake_pack_beast")
POLICY = Policy(kind="scripted", goals=[], on_hostile="ignore", hostile_range=2, pickup=True)
GATHER = {"op": "gather_gems", "count": 5}


def beast(eid: int, at) -> Entity:
    return Entity("npc", eid, at, PACK[1])


def field(at=(12, 10), health=4) -> WorldModel:
    """Known dirt over x 0..39, y 0..20, unknown beyond: its edge is the frontier."""
    w = WorldModel(character_id=1, map_id=MAP, pos=at, perception=8)
    for x in range(40):
        for y in range(21):
            w.view.tiles[(x, y)] = "dirt"
    w.terrain_center, w.terrain_map = at, MAP
    w.alive, w.tick = True, 1000
    w.health, w.max_health, w.lives = health, 10, 5
    w.hostile_types.add(PACK)
    w.threat.record(PACK, 2)
    return w


class GatherKeepsWorkingTest(unittest.TestCase):
    """Item 1: the only grass known lies beside a hostile 13 cells off."""

    def blocked(self) -> tuple[WorldModel, Memory]:
        w, m = field(), Memory()
        for x in (24, 25, 26):
            w.view.tiles[(x, 10)] = "grass"
        w.entities = [beast(9, (25, 11))]
        return w, m

    def test_it_walks_on_to_ground_clear_of_the_hostile(self):
        w, m = self.blocked()
        out = gather_outcome(w, m, POLICY, op=GATHER)
        self.assertIsNotNone(out.intents, "not handed to the safe default")
        self.assertEqual(out.intents[0]["verb"], "SetPosition")
        kind, cell = m.gather_target
        self.assertEqual(kind, CLEAR)
        self.assertIn(cell, w.view.frontier())
        self.assertGreater(chebyshev(cell, (25, 11)), GATHER_HOSTILE_RADIUS)
        self.assertTrue(route_clear(w, POLICY, m.path, danger(w, POLICY)))
        self.assertEqual(m.gather_status, WALKING.format(WALK_TARGETS[CLEAR]))

    def test_the_walk_is_kept_until_grass_comes_into_sight(self):
        w, m = self.blocked()
        gather_outcome(w, m, POLICY, op=GATHER)
        kept = m.gather_target
        w.pos = m.path[0]
        gather_outcome(w, m, POLICY, op=GATHER)
        self.assertEqual(m.gather_target, kept)
        grass = (w.pos[0] + 2, w.pos[1])
        w.view.tiles[grass] = "grass"
        gather_outcome(w, m, POLICY, op=GATHER)
        self.assertEqual(m.gather_target, ("grass", grass))

    def test_a_gem_pile_in_sight_ends_the_walk(self):
        w, m = self.blocked()
        gather_outcome(w, m, POLICY, op=GATHER)
        w.pos = m.path[0]
        pile = (w.pos[0] + 3, w.pos[1])
        w.entities.append(Entity("supply", 70, pile, "gem"))
        gather_outcome(w, m, POLICY, op=GATHER)
        self.assertEqual(m.gather_target, ("pile", pile))

    def test_grass_in_sight_it_cannot_reach_keeps_the_walk(self):
        """Walled-in grass in sight is tried once, then the walk goes on to
        the same cell, decision after decision (A71)."""
        w, m = self.blocked()
        gather_outcome(w, m, POLICY, op=GATHER)
        kept = m.gather_target
        w.pos = m.path[0]
        grass = (w.pos[0] + 3, w.pos[1] + (3 if w.pos[1] < 10 else -3))
        w.view.tiles[grass] = "grass"
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if (dx, dy) != (0, 0):
                    w.view.tiles[(grass[0] + dx, grass[1] + dy)] = "wall"
        out = gather_outcome(w, m, POLICY, op=GATHER)  # tried once
        self.assertEqual(m.gather_target, kept)
        searches = mock.Mock(wraps=gather_module.nearest_target)
        with mock.patch.object(gather_module, "nearest_target", searches):
            for _ in range(3):
                w.pos = (out.intents[0]["x"], out.intents[0]["y"])
                out = gather_outcome(w, m, POLICY, op=GATHER)
                self.assertEqual(m.gather_target, kept)
        self.assertEqual(searches.call_count, 0, "not searched again every decision")

    def test_a_hostile_that_comes_to_hold_its_cell_ends_the_walk(self):
        w, m = self.blocked()
        gather_outcome(w, m, POLICY, op=GATHER)
        first = m.gather_target[1]
        w.pos = m.path[0]
        w.entities.append(beast(10, first))
        gather_outcome(w, m, POLICY, op=GATHER)
        self.assertNotEqual(m.gather_target, (CLEAR, first))
        if m.gather_target is not None:
            self.assertGreater(chebyshev(m.gather_target[1], first), GATHER_HOSTILE_RADIUS)

    def test_with_no_frontier_clear_of_it_it_is_still_blocked(self):
        w, m = self.blocked()
        for x in range(-1, 41):
            w.view.tiles[(x, -1)] = w.view.tiles[(x, 21)] = "wall"
        for y in range(-1, 22):
            w.view.tiles[(-1, y)] = w.view.tiles[(40, y)] = "wall"
        self.assertEqual(w.view.frontier(), set())
        out = gather_outcome(w, m, POLICY, op=GATHER)
        self.assertIsNone(out.intents)
        self.assertEqual(m.gather_status, BLOCKED)


class DetourRiskTest(unittest.TestCase):
    """Item 2: a pile 4 cells from a pack of two, off a travel walk: outside
    their gather bar, inside ``RISK_RADIUS``."""

    PILE = (20, 14)

    def ctx(self, m: Memory, ops: list[dict] | None = None) -> PlayContext:
        ops = ops or [{"op": "travel", "to": "point", "x": 35, "y": 10}]
        return PlayContext(m, POLICY, random.Random(0), plan=Plan(ops, dict(PARAM_DEFAULTS)))

    def walking_past(self, health: int, pack=((19, 18), (21, 18))) -> tuple[WorldModel, Memory]:
        w, m = field(at=(14, 10), health=health), Memory()
        m.goal, m.path = "travel", [(x, 10) for x in range(15, 36)]
        w.entities = [Entity("supply", 70, self.PILE, "gem")] + [beast(9 + i, p) for i, p in enumerate(pack)]
        for p in pack:
            self.assertLessEqual(chebyshev(p, self.PILE), RISK_RADIUS)
        return w, m

    def test_hurt_it_skips_a_find_beside_a_pack(self):
        w, m = self.walking_past(health=4)
        self.assertIsNone(detour_find(w, self.ctx(m)))

    def test_at_full_health_it_takes_it(self):
        w, m = self.walking_past(health=10)
        find = detour_find(w, self.ctx(m))
        self.assertIsNotNone(find)
        self.assertEqual(find.id, 70)

    def test_the_price_counts_every_hostile_near_the_find(self):
        """7/10 covers one hit from one (7 − 2 > 4) but not from two (7 − 4)."""
        w, m = self.walking_past(health=7)
        self.assertIsNone(detour_find(w, self.ctx(m)))
        w, m = self.walking_past(health=7, pack=((19, 18),))
        self.assertIsNotNone(detour_find(w, self.ctx(m)))

    def test_a_remembered_pack_counts_too(self):
        w, m = self.walking_past(health=4)
        w._set_entities(list(w.entities), w.tick)  # seen: remembered
        w.tick += 1
        w._set_entities([e for e in w.entities if e.kind == "supply"], w.tick)
        self.assertFalse(risk_allowed(w, self.ctx(m), self.PILE, danger(w, POLICY)))

    def test_an_op_that_fights_for_its_ground_takes_the_risk(self):
        w, m = self.walking_past(health=4)
        find = detour_find(w, self.ctx(m, [{"op": "gather_gems", "count": 5, "fight": True}]))
        self.assertIsNotNone(find)

    def test_a_detour_under_way_ends_when_health_no_longer_covers_it(self):
        w, m = self.walking_past(health=10)
        c = self.ctx(m)
        DetourState().act(w, c)
        self.assertIsNotNone(m.detour)
        w.health = 4
        DetourState().act(w, c)
        self.assertIsNone(m.detour)
        self.assertNotIn(70, m.detour_skipped, "not given up for the run: healed, it may be taken")


if __name__ == "__main__":
    unittest.main()
