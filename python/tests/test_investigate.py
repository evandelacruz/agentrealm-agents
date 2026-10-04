"""Interest list and Investigate state (A30)."""

import random
import unittest

from agentrealm_agent.brain import decide
from agentrealm_agent.config import Policy
from agentrealm_agent.curiosity import curiosity_ticks_used, detour_allowed, record_curiosity_queue
from agentrealm_agent.directives import Directives
from agentrealm_agent.interest_list import list_interest, sight_range
from agentrealm_agent.investigation import cell_was_read, mark_cell_read, mark_npc_spoken, spoken_npc_ids
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.memory import Memory
from agentrealm_agent.states import dispatch
from agentrealm_agent.states.base import PlayContext
from agentrealm_agent.world import Entity, WorldModel, ZoneFact


def world(rows: list[str], at=(1, 1), perception=3) -> WorldModel:
    glyph = {".": "dirt", "#": "wall", "S": "wall"}
    w = WorldModel(character_id=1, map_id=7, pos=at, perception=perception)
    for y, row in enumerate(rows):
        for x, g in enumerate(row):
            w.view.tiles[(x, y)] = glyph[g]
            if g == "S":
                w.view.readable[(x, y)] = True
    w.terrain_center, w.terrain_map = at, 7
    return w


class InterestListTest(unittest.TestCase):
    def test_readable_in_sight_is_nominated(self):
        w = world(["...", ".S.", "..."], at=(1, 1))
        kb = KnowledgeBase.empty("sandbox")
        items = list_interest(w, kb, Policy(kind="scripted"), Memory(), Directives())
        kinds = {it.kind for it in items}
        self.assertIn("read_block", kinds)

    def test_read_cells_are_not_repeated(self):
        w = world(["...", ".S.", "..."], at=(1, 1))
        kb = KnowledgeBase.empty("sandbox")
        mark_cell_read(kb, 7, (1, 1))
        self.assertTrue(cell_was_read(kb, 7, (1, 1)))
        items = list_interest(w, kb, Policy(kind="scripted"), Memory(), Directives())
        self.assertFalse(any(it.kind == "read_block" for it in items))

    def test_npc_say_within_25_blocks(self):
        w = world(["...", "...", "..."], at=(1, 1))
        w.entities = [Entity("npc", 9, (1, 4), "guard")]
        kb = KnowledgeBase.empty("sandbox")
        pol = Policy(kind="scripted", hostile=[])
        items = list_interest(w, kb, pol, Memory(), Directives())
        self.assertTrue(any(it.kind == "say" for it in items))
        mark_npc_spoken(kb, 9)
        self.assertIn(9, spoken_npc_ids(kb))
        items = list_interest(w, kb, pol, Memory(), Directives())
        self.assertFalse(any(it.kind == "say" for it in items))

    def test_hostiles_pause_curiosity(self):
        w = world(["...", ".S.", "..."], at=(1, 1))
        w.entities = [Entity("npc", 3, (2, 1), "wolf")]
        pol = Policy(kind="scripted", hostile=["npc"], hostile_range=2)
        items = list_interest(w, kb := KnowledgeBase.empty("sandbox"), pol, Memory(), Directives())
        self.assertEqual(items, [])


class InvestigateStateTest(unittest.TestCase):
    def test_investigate_beats_explore_for_unread_sign(self):
        w = world(["...", ".S.", "..."], at=(0, 1))
        ctx = PlayContext(Memory(), Policy(kind="scripted", goals=["explore"]), random.Random(0), knowledge=KnowledgeBase.empty("sandbox"))
        out = dispatch(w, ctx)
        self.assertEqual(out.state, "Investigate")
        self.assertEqual(out.intents[0]["verb"], "Read")

    def test_decide_returns_read_intent(self):
        w = world(["...", ".S.", "..."], at=(0, 1))
        d = decide(w, Memory(), Policy(kind="scripted"), random.Random(0), knowledge=KnowledgeBase.empty("sandbox"))
        self.assertEqual(d.intent["verb"], "Read")


class CuriosityTrackingTest(unittest.TestCase):
    def test_segments_sum_in_window(self):
        m = Memory()
        record_curiosity_queue(m, 100, 50)
        record_curiosity_queue(m, 200, 40)
        self.assertEqual(curiosity_ticks_used(m, 250), 90)
        self.assertEqual(curiosity_ticks_used(m, 800), 40)

    def test_detour_allowed_tracks_param(self):
        m = Memory()
        record_curiosity_queue(m, 0, 150)
        self.assertFalse(detour_allowed(m, 100, 0.2))
        self.assertTrue(detour_allowed(m, 100, 1.0))


class SightRangeTest(unittest.TestCase):
    def test_brightness_caps_sight(self):
        w = WorldModel(1, map_id=1, pos=(0, 0), perception=5)
        w.zones[1] = {(0, 0): ZoneFact(safe=False, brightness=0.5)}
        self.assertLess(sight_range(w, 1, (0, 0)), 5)


if __name__ == "__main__":
    unittest.main()
