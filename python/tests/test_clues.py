"""A32: clue capture and no-LLM clue rules."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentrealm_agent import config
from agentrealm_agent.clues import (
    direction_preference,
    nearest_explore_target,
    note_read_clue,
    note_spoken_clue,
    order_explore_targets,
    record_clue,
)
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.memory import Memory
from agentrealm_agent.navigation.planner import CostGridParams
from agentrealm_agent.runner import Runner
from agentrealm_agent.states.intents import read_block
from agentrealm_agent.world import WorldModel
from tests.test_investigate import world


class ClueCaptureTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.object(config, "STATE_DIR", Path(tmp.name))
        patch.start()
        self.addCleanup(patch.stop)
        self.kb = KnowledgeBase.empty("sandbox")
        self.m = Memory()

    def test_record_clue_and_signal(self):
        self.assertTrue(
            record_clue(self.kb, self.m, kind="sign", text="Go north.", map_id=7, x=1, y=2, tick=50)
        )
        self.assertEqual(len(self.kb.clues), 1)
        self.assertEqual(self.kb.clues[0]["tick"], 50)
        self.assertEqual(self.m.clue_signals[0]["trigger"], "clue")
        self.assertFalse(record_clue(self.kb, self.m, kind="sign", text="Go north.", map_id=7, x=1, y=2, tick=60))

    def test_note_read_clue_from_result(self):
        note_read_clue(
            self.kb,
            self.m,
            {"outcome": "applied", "text": "The cave lies east."},
            7,
            (3, 4),
            10,
        )
        self.assertEqual(self.kb.clues[0]["text"], "The cave lies east.")
        self.assertEqual(self.kb.clues[0]["kind"], "sign")

    def test_runner_read_stores_clue(self):
        from agentrealm_agent.config import CharacterConfig, Policy

        cfg = CharacterConfig("T", "default", "test", "sandbox", Policy(kind="scripted"), Path("t.toml"))
        r = Runner(cfg, mock.Mock(), 1, mock.Mock(), out=lambda _: None, knowledge=self.kb)
        r.world = world(["....."], at=(0, 0))
        r.mem = Memory()
        r.mem.pending = read_block((1, 1))
        r.on_result({"outcome": "applied", "tick": 5, "text": "Turn left."}, 0)
        self.assertEqual(r.knowledge.clues[0]["text"], "Turn left.")
        self.assertEqual(len(r.mem.clue_signals), 1)

    def test_spoken_clue_from_event(self):
        w = world(["....."], at=(0, 0))
        from agentrealm_agent.world import Entity

        w.entities = [Entity(id=4, kind="npc", pos=(2, 0), code="helper")]
        note_spoken_clue(
            self.kb,
            self.m,
            w,
            {"kind": "SpokenTo", "speaker_kind": "npc", "speaker_id": 4, "text": "Head west."},
            20,
        )
        self.assertEqual(self.kb.clues[0]["kind"], "npc")
        self.assertEqual(self.kb.clues[0]["x"], 2)


class ClueRulesTest(unittest.TestCase):
    def test_direction_preference(self):
        kb = KnowledgeBase.empty("sandbox")
        kb.clues.append({"text": "Walk north from the well.", "map_id": 1, "x": 0, "y": 0, "tick": 1})
        self.assertEqual(direction_preference(kb), (0.0, -1.0))

    def test_order_explore_targets_prefers_north(self):
        kb = KnowledgeBase.empty("sandbox")
        kb.clues.append({"text": "north", "map_id": 1, "x": 0, "y": 0, "tick": 1})
        w = world([".......", ".......", "......."], at=(3, 1))
        w.view.tiles.update({(3, 0): "grass", (3, 2): "grass", (4, 1): "grass"})
        targets = {(3, 0), (3, 2), (4, 1)}
        ordered = order_explore_targets(w, targets, kb)
        self.assertEqual(ordered[0], (3, 0), "north frontier before south or east")

    def test_nearest_explore_target_tries_east_first_at_equal_distance(self):
        kb = KnowledgeBase.empty("sandbox")
        kb.clues.append({"text": "east", "map_id": 1, "x": 0, "y": 0, "tick": 1})
        rows = ["......."] * 5
        w = world(rows, at=(2, 2))
        for x in range(5):
            w.view.tiles[(x, 2)] = "grass"
        targets = {(0, 2), (4, 2)}
        ordered = order_explore_targets(w, targets, kb)
        self.assertEqual(ordered[0], (4, 2))
        found = nearest_explore_target(w, targets, CostGridParams(), kb)
        self.assertIsNotNone(found)
        self.assertEqual(found[0], (4, 2))


if __name__ == "__main__":
    unittest.main()
