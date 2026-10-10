"""Free-play run 3 offline: Park spent 60 s two cells from a safe tile (A9, A66).

A search cut short by its budget still counts as a way to a safe tile, so the
pick kept a tile the walk never got nearer to, logging "safe tile
unreachable" every decision. A Park, Retreat or Heal walk to a safe tile
that goes a stuck window (A15) without its remaining path shortening now
rules that tile out for a bounded time and moves on to the next candidate,
else the town cell.
"""

from __future__ import annotations

import unittest

from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.navigation import stuck as nav_stuck
from agentrealm_agent.pathing import SAFE_UNREACHABLE_TICKS
from agentrealm_agent.states import dispatch
from agentrealm_agent.travel import sync_town
from agentrealm_agent.world import Entity, WorldModel
from agentrealm_agent.zone_discovery import apply_zone
from tests.test_survival_runs import MAP, ctx, hit, world

STUCK = (6, 10)  # nearer than OPEN to the agent at (10, 10)
OPEN = (-5, 10)
TOWN = (-20, 10)


def safe(w: WorldModel, *cells) -> None:
    for cell in cells:
        apply_zone(w, MAP, cell[0], cell[1], {"safe": True})


def parking(kb: KnowledgeBase | None = None):
    c = ctx(kb=kb)
    c.memory.parking = True
    return c


def stand_still(w: WorldModel, c, ticks: int, every: int = 10) -> list:
    """Decide every ``every`` ticks for ``ticks`` while no step ever lands:
    the walk is reachable by budget but never gets closer."""
    outs = []
    for _ in range(ticks // every):
        outs.append(dispatch(w, c))
        w.tick += every
    return outs


class SafeWalkWithNoProgressTest(unittest.TestCase):
    def test_park_abandons_the_tile_within_the_window_and_moves_on(self):
        w, c = world(), parking()
        safe(w, STUCK, OPEN)
        first = dispatch(w, c)
        self.assertEqual((first.state, first.reason), ("Park", f"retreat → safe {STUCK}"))
        outs = stand_still(w, c, nav_stuck.PROGRESS_TICK_LIMIT + 10)
        self.assertEqual(outs[-1].reason, f"retreat → safe {OPEN}")
        self.assertIn((MAP, STUCK), c.memory.safe_unreachable)
        moved_on = next(i for i, o in enumerate(outs) if o.reason.endswith(f"{OPEN}"))
        self.assertLessEqual(moved_on * 10, nav_stuck.PROGRESS_TICK_LIMIT + 10, "within the stuck window")
        self.assertEqual(c.memory.path[-1], OPEN)

    def test_a_walk_that_keeps_shortening_is_never_abandoned(self):
        w, c = world(), parking()
        safe(w, (-30, 10))
        for _ in range(nav_stuck.PROGRESS_TICK_LIMIT // 20 + 5):  # a slow step: 20 ticks each
            out = dispatch(w, c)
            self.assertEqual(out.reason, "retreat → safe (-30, 10)")
            w.pos = c.memory.path[0]  # the step lands
            c.memory.path = c.memory.path[1:]
            w.tick += 20
        self.assertEqual(c.memory.safe_unreachable, {})

    def test_retreat_abandons_it_too(self):
        w, c = world(health=4), ctx(on_hostile="fight")
        w.entities = [Entity("npc", 7, (11, 10), code="chaser")]
        safe(w, STUCK, OPEN)
        outs = []
        for _ in range(nav_stuck.PROGRESS_TICK_LIMIT // 10 + 1):
            hit(w)
            outs.append(dispatch(w, c))
            w.tick += 10
        self.assertEqual(outs[0].reason, f"retreat → safe {STUCK}")
        self.assertEqual(outs[-1].state, "Retreat")
        self.assertTrue(outs[-1].reason.startswith(f"retreat → safe {OPEN}"), outs[-1].reason)

    def test_heal_abandons_it_too(self):
        w, c = world(health=4), ctx()
        safe(w, STUCK, OPEN)
        outs = stand_still(w, c, nav_stuck.PROGRESS_TICK_LIMIT + 20)  # the give-up decision itself sends nothing
        self.assertEqual(outs[0].reason, f"heal_measure → {STUCK}")
        self.assertEqual(outs[-1].reason, f"heal_measure → {OPEN}")

    def test_the_mark_expires_after_the_bounded_time(self):
        w, c = world(), parking()
        safe(w, STUCK, OPEN)
        stand_still(w, c, nav_stuck.PROGRESS_TICK_LIMIT + 10)
        marked = c.memory.safe_unreachable[(MAP, STUCK)]
        w.tick = marked + SAFE_UNREACHABLE_TICKS - 1
        self.assertEqual(dispatch(w, c).reason, f"retreat → safe {OPEN}")
        w.tick = marked + SAFE_UNREACHABLE_TICKS
        self.assertEqual(dispatch(w, c).reason, f"retreat → safe {STUCK}", "the nearer tile is tried again")

    def test_with_no_candidate_left_it_walks_to_town(self):
        kb = KnowledgeBase("sandbox")
        sync_town(kb, {"map_id": MAP, "x": TOWN[0], "y": TOWN[1]})
        w, c = world(), parking(kb)
        safe(w, STUCK)
        outs = stand_still(w, c, nav_stuck.PROGRESS_TICK_LIMIT + 10)
        self.assertEqual(outs[0].reason, f"retreat → safe {STUCK}")
        self.assertEqual(outs[-1].reason, f"retreat → safe {TOWN}")


if __name__ == "__main__":
    unittest.main()
