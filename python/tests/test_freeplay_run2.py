"""Free-play run 2 offline: 10 minutes, no deaths, gems 6 → 17 and nothing bought.

Rebuilt here (A9, A10, A66, A22, A63): Park and Retreat aimed at safe
tiles walled off by bush and wall, with no fallback. They now head for the
nearest safe cell a path reaches, else the town cell.

Gather's region rules (a ``gather_gems`` x, y, yield pricing) are tested
as rules in ``test_gather.py`` (``TargetRegionTest``, ``YieldPricingTest``).
"""

from __future__ import annotations

import unittest

from agentrealm_agent.config import Policy
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.memory import Memory
from agentrealm_agent.pathing import SAFE_GOAL_CHECKS, SAFE_THREATENED_TICKS, grid_params, reachable_safe_goal
from agentrealm_agent.states import dispatch
from agentrealm_agent.states.fight import retreat_tail
from agentrealm_agent.states.retreat import retreat_step
from agentrealm_agent.travel import sync_town
from agentrealm_agent.world import Entity, WorldModel
from agentrealm_agent.zone_discovery import apply_zone
from tests.test_survival_runs import MAP, ctx, hit, world


def wall_in(w: WorldModel, cell) -> None:
    """Wall every neighbour of ``cell`` (bush on one side, as in the run)."""
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            if (dx, dy) != (0, 0):
                w.view.tiles[(cell[0] + dx, cell[1] + dy)] = "bush" if dx == 1 else "wall"


WALLED = (6, 10)  # nearer than OPEN to the agent at (10, 10)
OPEN = (-5, 10)


def safe(w: WorldModel, *cells) -> None:
    for cell in cells:
        apply_zone(w, MAP, cell[0], cell[1], {"safe": True})


class ReachableSafeGoalTest(unittest.TestCase):
    def test_skips_a_walled_off_safe_tile_and_remembers_it(self):
        w, m = world(), Memory()
        wall_in(w, WALLED)
        params = grid_params(Policy(kind="scripted"), set(), set())
        self.assertEqual(reachable_safe_goal(m, w, [WALLED, OPEN], params, None), OPEN)
        self.assertIn((MAP, WALLED), m.safe_unreachable)

    def test_a_boxed_in_start_marks_no_safe_cell_unreachable(self):
        # Review on #140: a search that runs out from our side proves only
        # that we stand shut in (here behind a bush Break could open).
        w, m = world(), Memory()
        wall_in(w, (10, 10))
        params = grid_params(Policy(kind="scripted"), set(), set())
        reachable_safe_goal(m, w, [OPEN, (14, 10)], params, (-20, 10))
        self.assertEqual(m.safe_unreachable, {})
        w.pos = (20, 10)
        self.assertEqual(reachable_safe_goal(m, w, [OPEN, (14, 10)], params, (-20, 10)), OPEN)

    def test_falls_back_to_town(self):
        w, m = world(), Memory()
        wall_in(w, WALLED)
        params = grid_params(Policy(kind="scripted"), set(), set())
        self.assertEqual(reachable_safe_goal(m, w, [WALLED], params, (-20, 10)), (-20, 10))
        self.assertIsNone(reachable_safe_goal(m, w, [WALLED], params, None))

    def test_past_the_checks_the_nearest_candidate_is_still_tried(self):
        w, m = world(), Memory()
        far = [(-20, y) for y in range(0, 2 * SAFE_GOAL_CHECKS, 2)]
        for cell in far:
            wall_in(w, cell)
        params = grid_params(Policy(kind="scripted"), set(), set())
        self.assertEqual(reachable_safe_goal(m, w, far + [(25, 10), (12, 10)], params, None), (12, 10))

class SafeWalksPickAReachableTileTest(unittest.TestCase):
    def test_retreat_heads_for_the_nearest_reachable_safe_tile(self):
        w, c = world(health=4), ctx(on_hostile="fight")
        w.entities = [Entity("npc", 7, (11, 10), code="chaser")]
        hit(w)
        safe(w, WALLED, OPEN)
        wall_in(w, WALLED)
        out = dispatch(w, c)
        self.assertEqual((out.state, out.reason), ("Retreat", f"retreat → safe {OPEN}"))

    def test_retreat_falls_back_to_town_when_no_safe_tile_is_reachable(self):
        kb = KnowledgeBase("sandbox")
        sync_town(kb, {"map_id": MAP, "x": -20, "y": 10})
        w, c = world(health=4), ctx(on_hostile="fight", kb=kb)
        w.entities = [Entity("npc", 7, (11, 10), code="chaser")]
        hit(w)
        safe(w, WALLED)
        wall_in(w, WALLED)
        out = dispatch(w, c)
        self.assertEqual((out.state, out.reason), ("Retreat", "retreat → safe (-20, 10)"))

    def test_an_escape_cell_never_marks_its_safe_tile_unreachable(self):
        # Review on #140: a one-shot oscillation escape on the pocket's only
        # entrance, or on the goal itself, is no proof the tile is walled in.
        for paced in ({(7, 10)}, {WALLED}):
            w, c = world(health=4), ctx(on_hostile="fight")
            w.entities = [Entity("npc", 7, (11, 10), code="chaser")]
            hit(w)
            safe(w, WALLED)
            wall_in(w, WALLED)
            w.view.tiles[(7, 10)] = "dirt"  # the pocket's only way in
            retreat_step(w, c, "Retreat", paced=paced)
            self.assertNotIn((MAP, WALLED), c.memory.safe_unreachable, paced)
            self.assertEqual(dispatch(w, c).reason, f"retreat → safe {WALLED}", "next decision walks in")

    def test_fight_retreat_tail_heads_where_retreat_does(self):
        w, c = world(health=4), ctx(on_hostile="fight")
        safe(w, WALLED, OPEN)
        wall_in(w, WALLED)
        self.assertTrue(retreat_tail(w, c.memory, c.policy, c, limit=3))
        self.assertEqual(c.memory.path[-1], OPEN)

    def test_park_walks_to_a_reachable_safe_tile(self):
        w, c = world(), ctx()
        c.memory.parking = True
        safe(w, WALLED, OPEN)
        wall_in(w, WALLED)
        out = dispatch(w, c)
        self.assertEqual((out.state, out.reason), ("Park", f"retreat → safe {OPEN}"))


class SafePickSkipsHostileReachTest(unittest.TestCase):
    """A63 run 4: Heal walked to the nearest safe tile with a known-hostile
    pack beside it and died. The pick skips safe tiles in a known hostile's
    reach (``survival.hostile_reach``), then falls back to the next reachable
    safe tile, then the town cell."""

    PACKED = (6, 10)  # nearer than OPEN to the agent at (10, 10)
    PACK = [Entity("npc", 30 + i, p, code="wartlurch") for i, p in enumerate([(6, 12), (5, 12), (7, 13)])]

    def packed(self, w: WorldModel) -> None:
        """The pack in view, of a type seen attacking: known hostile."""
        w.hostile_types.add(("npc", "wartlurch"))
        w.entities.extend(self.PACK)

    def test_skips_a_safe_tile_in_a_hostiles_reach(self):
        w, m = world(), Memory()
        params = grid_params(Policy(kind="scripted"), set(), set())
        pack = {("npc", 30): {self.PACKED}}
        self.assertEqual(reachable_safe_goal(m, w, [self.PACKED, OPEN], params, None, pack), OPEN)
        self.assertNotIn((MAP, self.PACKED), m.safe_unreachable, "a threat is not a wall")
        self.assertEqual(m.safe_threatened, {(MAP, self.PACKED): {("npc", 30): w.tick}})
        self.assertEqual(reachable_safe_goal(m, w, [self.PACKED, OPEN], params, None), OPEN, "remembered out of view")
        w.tick += SAFE_THREATENED_TICKS
        self.assertEqual(reachable_safe_goal(m, w, [self.PACKED, OPEN], params, None), self.PACKED)
        self.assertEqual(m.safe_threatened, {}, "a lapsed mark is dropped")

    def test_with_every_safe_tile_threatened_it_falls_back_to_town(self):
        w, m = world(), Memory()
        params = grid_params(Policy(kind="scripted"), set(), set())
        pack = {("npc", 30): {self.PACKED}, ("npc", 31): {OPEN}}
        self.assertEqual(reachable_safe_goal(m, w, [self.PACKED, OPEN], params, (-20, 10), pack), (-20, 10))
        self.assertIsNone(reachable_safe_goal(m, w, [self.PACKED, OPEN], params, None, pack))

    def test_retreat_keeps_its_pick_when_the_pack_leaves_view(self):
        w, c = world(health=4), ctx(on_hostile="fight")
        w.entities = [Entity("npc", 7, (11, 10), code="chaser")]
        hit(w)
        self.packed(w)
        safe(w, self.PACKED, OPEN)
        self.assertEqual(dispatch(w, c).reason, f"retreat → safe {OPEN}")
        w.entities = [Entity("npc", 7, (11, 10), code="chaser")]
        w.tick += 10
        c.memory.held_queue = None
        self.assertEqual(dispatch(w, c).reason, f"retreat → safe {OPEN}")

    def test_retreat_skips_a_tile_beside_its_chaser(self):
        # Free-play run 7: the chaser's reach rules a tile out too.
        w, c = world(health=4), ctx(on_hostile="fight")
        safe(w, (12, 10), OPEN)
        w.hostile_types.add(("npc", "chaser"))
        w.entities = [Entity("npc", 7, (11, 10), code="chaser")]
        hit(w)
        out = dispatch(w, c)
        self.assertEqual((out.state, out.reason), ("Retreat", f"retreat → safe {OPEN}"))

    def test_retreat_skips_a_safe_tile_beside_a_pack_it_is_not_running_from(self):
        w, c = world(health=4), ctx(on_hostile="fight")
        w.entities = [Entity("npc", 7, (11, 10), code="chaser")]
        hit(w)
        self.packed(w)
        safe(w, self.PACKED, OPEN)
        out = dispatch(w, c)
        self.assertEqual((out.state, out.reason), ("Retreat", f"retreat → safe {OPEN}"))

    def test_retreat_skips_a_safe_tile_beside_its_chaser(self):
        # Free-play run 7: a safe tile 2 cells from the hostile Retreat ran
        # from drew it back into 6 hits. The chaser's reach rules it out too.
        w, c = world(health=4), ctx(on_hostile="fight")
        w.entities = [Entity("npc", 7, (11, 10), code="chaser")]
        hit(w)
        safe(w, (12, 10), OPEN)
        out = dispatch(w, c)
        self.assertEqual((out.state, out.reason), ("Retreat", f"retreat → safe {OPEN}"))

    def test_fight_retreat_tail_skips_it_too(self):
        w, c = world(health=4), ctx(on_hostile="fight")
        self.packed(w)
        safe(w, self.PACKED, OPEN)
        self.assertTrue(retreat_tail(w, c.memory, c.policy, c, limit=3))
        self.assertEqual(c.memory.path[-1], OPEN)


if __name__ == "__main__":
    unittest.main()
