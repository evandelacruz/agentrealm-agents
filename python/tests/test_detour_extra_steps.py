"""Detour prices every find in view by the steps it adds, each kind with its own allowance (A73).

Free-play run 4 walked past a gem triple 5 cells off an Explore walk: it lay
past the fixed 3-cell reach and cost about 7 extra steps against an
allowance of 4. Gather went back for it 255 s later.
"""

from __future__ import annotations

import unittest

from agentrealm_agent.directives import PARAM_DEFAULTS
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.memory import Memory
from agentrealm_agent.plan import Plan
from agentrealm_agent.states import dispatch
from agentrealm_agent.states.detour import GEM_PILE_STEPS, detour_find, extra_steps
from agentrealm_agent.world import Entity, WorldModel

from tests.test_freeplay_run4 import ctx, open_field

HERE, TARGET = (10, 10), (35, 10)


def walking() -> tuple[WorldModel, Memory, Plan]:
    """Travel under way east along y = 10 from ``HERE``."""
    w, m = open_field(at=HERE), Memory()
    plan = Plan([{"op": "travel", "to": "point", "x": TARGET[0], "y": TARGET[1]}], dict(PARAM_DEFAULTS))
    out = dispatch(w, ctx(m, plan, pickup=True))
    assert out.state == "Travel", out.reason
    return w, m, plan


def gems(*cells) -> list[Entity]:
    return [Entity("supply", 70 + i, c, "gem") for i, c in enumerate(cells)]


class PricedByStepsTest(unittest.TestCase):
    def test_a_triple_five_off_the_route_is_taken(self):
        w, m, plan = walking()
        w.entities = gems((9, 15), (10, 15), (11, 15))
        self.assertIsNotNone(detour_find(w, ctx(m, plan, pickup=True)), "past the old 3-cell reach")
        self.assertEqual(dispatch(w, ctx(m, plan, pickup=True)).state, "Detour")

    def test_a_lone_pile_twelve_off_is_not(self):
        w, m, plan = walking()
        w.entities = gems((10, 22))
        self.assertIsNone(detour_find(w, ctx(m, plan, pickup=True)))
        self.assertEqual(dispatch(w, ctx(m, plan, pickup=True)).state, "Travel")

    def test_a_cluster_is_worth_more_than_a_lone_pile(self):
        w, m, plan = walking()
        w.entities = gems((6, 16))  # behind and 6 off: 10 extra steps
        self.assertIsNone(detour_find(w, ctx(m, plan, pickup=True)))
        w.entities = gems((5, 16), (6, 16), (7, 16))
        self.assertIsNotNone(detour_find(w, ctx(m, plan, pickup=True)))

    def test_a_life_is_worth_more_than_a_gem(self):
        w, m, plan = walking()
        kb = KnowledgeBase.empty("sandbox")
        kb.items["heart"] = {"life_on_pickup": True}
        w.entities = [Entity("supply", 80, (6, 16), "heart")]
        find = detour_find(w, ctx(m, plan, kb, pickup=True))
        self.assertEqual(find.id if find else None, 80)

    def test_a_wall_between_the_route_and_the_find_counts(self):
        w, m, plan = walking()
        for x in range(40):
            if x != 30:
                w.view.tiles[(x, 11)] = "wall"
        self.assertIsNone(extra_steps(w, HERE, (14, 12), list(m.path), GEM_PILE_STEPS), "2 off, but round the wall")
        w.entities = gems((14, 12))
        self.assertIsNone(detour_find(w, ctx(m, plan, pickup=True)))

    def test_a_find_off_danger_free_ground_is_left(self):
        w, m, plan = walking()
        w.hostile_types.add(("npc", "gnawer"))
        w.entities = gems((9, 15), (10, 15), (11, 15)) + [Entity("npc", 90, (10, 17), "gnawer")]
        self.assertIsNone(detour_find(w, ctx(m, plan, pickup=True, hostile=["npc"], hostile_range=1)))


class ShortInsertTest(unittest.TestCase):
    """The detour takes the triple and the walk resumes to its target, without pacing."""

    def test_triple_taken_then_the_walk_goes_on(self):
        w, m, plan = walking()
        w.gems = 0
        w.entities = gems((9, 15), (10, 15), (11, 15))
        states = []
        for _ in range(80):
            o = dispatch(w, ctx(m, plan, pickup=True))
            states.append(o.state)
            for i in o.intents or []:
                if i["verb"] == "SetPosition":
                    w.pos = (i["x"], i["y"])
                    if m.path and m.path[0] == w.pos:
                        m.path = m.path[1:]
                elif i["verb"] == "Take":
                    w.entities = [e for e in w.entities if e.id != i["supply_id"]]
                    w.gems += 1
            w.tick += 4
            if w.pos == TARGET:
                break
        self.assertEqual(w.gems, 3, states)
        self.assertEqual(w.pos, TARGET, states)
        self.assertEqual(m.nav_stuck.oscillations, [], "no pacing")
        last_detour = max(i for i, s in enumerate(states) if s == "Detour")
        self.assertNotIn("Detour", states[last_detour + 1 :])
        self.assertIsNone(m.detour)


if __name__ == "__main__":
    unittest.main()
