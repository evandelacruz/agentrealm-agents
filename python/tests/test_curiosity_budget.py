"""Curiosity budget cap for Investigate detours (A30)."""

import unittest

from agentrealm_agent.curiosity_budget import (
    WINDOW_TICKS,
    cap_ticks,
    charged_ticks_in_queue,
    curiosity_room,
    record_curiosity_queue,
    ticks_in_window,
)
from agentrealm_agent.directives import PARAM_DEFAULTS
from agentrealm_agent.interest_list import pick_interest_tick
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.memory import Memory
from agentrealm_agent.config import Policy
from agentrealm_agent.states.intents import read_block, set_position
from agentrealm_agent.world import WorldModel


class CuriosityBudgetTest(unittest.TestCase):
    def test_default_cap_is_twenty_percent_of_window(self):
        self.assertEqual(cap_ticks(0.2), 120)

    def test_read_and_say_queues_do_not_charge(self):
        m = Memory()
        record_curiosity_queue(m, 50, [read_block((1, 1))], "Investigate")
        record_curiosity_queue(
            m,
            51,
            [{"verb": "Say", "text": "hi", "target": {"kind": "npc", "npc_id": 1}}],
            "Investigate",
        )
        self.assertEqual(m.curiosity_spans, [])

    def test_investigate_walk_counts(self):
        m = Memory()
        record_curiosity_queue(m, 100, [set_position((2, 2))], "Investigate")
        self.assertEqual(m.curiosity_spans, [(100, 1)])
        self.assertEqual(ticks_in_window(m.curiosity_spans, 100), 1)

    def test_window_sums_overlapping_spans(self):
        spans = [(590, 20), (600, 10)]
        # Window at tick 600 is ticks 1–600; only the tail of the first span overlaps.
        self.assertEqual(ticks_in_window(spans, 600), 12)

    def test_curiosity_zero_blocks_charged_items_only(self):
        w = WorldModel(1, map_id=7, pos=(0, 0), perception=8)
        for y in range(6):
            for x in range(6):
                w.view.tiles[(x, y)] = "dirt"
        w.view.readable[(1, 0)] = True
        w.view.tiles[(5, 5)] = "framed_door"
        kb = KnowledgeBase.empty("sandbox")
        kb.entrances["7:5,5"] = {"map_id": 7, "x": 5, "y": 5}
        m = Memory(curiosity_spans=[(1, WINDOW_TICKS)])
        w.tick = WINDOW_TICKS
        params = dict(PARAM_DEFAULTS)
        params["curiosity"] = 0.0
        self.assertFalse(curiosity_room(params, m, w.tick))
        picked = pick_interest_tick(w, kb, Policy(kind="scripted"), m, params=params, tick=w.tick)
        self.assertEqual(picked.kind, "read_block")
        self.assertEqual(picked.pos, (1, 0))

    def test_charged_ticks_in_queue(self):
        self.assertEqual(charged_ticks_in_queue([read_block((1, 1))]), 0)
        self.assertEqual(charged_ticks_in_queue([set_position((1, 1)), read_block((1, 1))]), 1)


if __name__ == "__main__":
    unittest.main()
