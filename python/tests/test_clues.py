"""A32: clue capture and no-LLM clue rules."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentrealm_agent import config
from agentrealm_agent.clues import (
    DIRECTION_TICKS,
    direction_hint,
    nearest_explore_target,
    note_read_clue,
    note_spoken_clue,
    record_clue,
)
from agentrealm_agent.config import CharacterConfig, Policy
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.memory import Memory
from agentrealm_agent.navigation import planner
from agentrealm_agent.navigation.planner import CostGridParams
from agentrealm_agent.runner import Runner
from agentrealm_agent.states.intents import read_block
from agentrealm_agent.world import Entity
from tests.test_investigate import world


def spoken(speaker_id: int, text: str) -> dict:
    return {"kind": "SpokenTo", "speaker_kind": "npc", "speaker_id": speaker_id, "text": text}


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
        self.assertEqual(len(self.m.clue_signals), 1, "a duplicate fires no second signal")

    def test_note_read_clue_from_result(self):
        note_read_clue(self.kb, self.m, {"outcome": "applied", "text": "The cave lies east."}, 7, (3, 4), 10)
        self.assertEqual(self.kb.clues[0]["text"], "The cave lies east.")
        self.assertEqual(self.kb.clues[0]["kind"], "sign")

    def test_runner_read_stores_clue(self):
        cfg = CharacterConfig("T", "sandbox", Policy(kind="scripted"), Path("t.toml"))
        r = Runner(cfg, mock.Mock(), 1, mock.Mock(), out=lambda _: None, knowledge=self.kb)
        r.world = world(["....."], at=(0, 0))
        r.mem = Memory()
        r.mem.pending = read_block((1, 1))
        r.on_result({"outcome": "applied", "tick": 5, "text": "Turn left."}, 0)
        self.assertEqual(r.knowledge.clues[0]["text"], "Turn left.")
        self.assertEqual(len(r.mem.clue_signals), 1)

    def test_spoken_clue_uses_the_helpers_cell_when_in_sight(self):
        w = world(["....."], at=(0, 0))
        w.entities = [Entity(id=4, kind="npc", pos=(2, 0), code="helper")]
        note_spoken_clue(self.kb, self.m, w, spoken(4, "Head west."), 20)
        self.assertEqual(self.kb.clues[0]["kind"], "npc")
        self.assertEqual(self.kb.clues[0]["speaker_id"], 4)
        self.assertEqual((self.kb.clues[0]["x"], self.kb.clues[0]["y"]), (2, 0))

    def test_helpers_out_of_sight_heard_from_one_stand_are_all_kept(self):
        # Say reaches 25 blocks, further than sight: both fall back to where we stand.
        w = world(["....."], at=(0, 0))
        note_spoken_clue(self.kb, self.m, w, spoken(4, "Head west."), 20)
        note_spoken_clue(self.kb, self.m, w, spoken(5, "Take matches."), 20)
        self.assertEqual([c["speaker_id"] for c in self.kb.clues], [4, 5])

    def test_same_helper_same_line_is_stored_once(self):
        w = world(["....."], at=(0, 0))
        note_spoken_clue(self.kb, self.m, w, spoken(4, "Head west."), 20)
        w.pos = (3, 0)
        note_spoken_clue(self.kb, self.m, w, spoken(4, "Head west."), 40)
        self.assertEqual(len(self.kb.clues), 1)


def clue(text: str, *, map_id: int = 7, at=(0, 0), tick: int = 0) -> dict:
    return {"kind": "sign", "text": text, "map_id": map_id, "x": at[0], "y": at[1], "tick": tick}


class DirectionHintTest(unittest.TestCase):
    def test_hint_is_scoped_to_the_map_it_was_found_on(self):
        kb = KnowledgeBase.empty("sandbox")
        kb.clues.append(clue("Walk north from the well.", map_id=8))
        self.assertIsNone(direction_hint(world(["..."], at=(1, 0)), kb))

    def test_hint_expires(self):
        kb = KnowledgeBase.empty("sandbox")
        kb.clues.append(clue("Walk north from the well.", at=(1, 0), tick=100))
        w = world(["..."], at=(1, 0))
        w.tick = 100 + DIRECTION_TICKS
        self.assertEqual(direction_hint(w, kb), ((1, 0), (0, -1)))
        w.tick += 1
        self.assertIsNone(direction_hint(w, kb))

    def test_newest_clue_wins_and_hints_do_not_cancel(self):
        kb = KnowledgeBase.empty("sandbox")
        kb.clues.append(clue("Go east.", tick=10))
        kb.clues.append(clue("Go north-west.", at=(2, 2), tick=20))
        w = world(["..."], at=(1, 0))
        w.tick = 30
        self.assertEqual(direction_hint(w, kb), ((2, 2), (-1, -1)))


class NearestExploreTargetTest(unittest.TestCase):
    def _row_world(self, at=(5, 0)):
        return world(["..........."], at=at)

    def test_no_hint_is_plain_nearest(self):
        w = self._row_world()
        found = nearest_explore_target(w, {(3, 0), (10, 0)}, CostGridParams(), KnowledgeBase.empty("sandbox"))
        self.assertEqual(found[0], (3, 0))

    def test_hinted_side_beats_a_closer_frontier_on_the_other_side(self):
        kb = KnowledgeBase.empty("sandbox")
        kb.clues.append(clue("east", at=(5, 0)))
        w = self._row_world()
        found = nearest_explore_target(w, {(4, 0), (9, 0), (10, 0)}, CostGridParams(), kb)
        self.assertEqual(found[0], (9, 0), "nearest on the east side, not the closer west cell")

    def test_side_is_measured_from_where_the_clue_was_found(self):
        kb = KnowledgeBase.empty("sandbox")
        kb.clues.append(clue("east", at=(7, 0)))
        w = self._row_world()
        # (6, 0) is east of us but west of the sign; (8, 0) is past the sign.
        found = nearest_explore_target(w, {(6, 0), (8, 0)}, CostGridParams(), kb)
        self.assertEqual(found[0], (8, 0))

    def test_falls_back_to_nearest_when_hinted_side_has_no_frontier(self):
        kb = KnowledgeBase.empty("sandbox")
        kb.clues.append(clue("east", at=(5, 0)))
        w = self._row_world()
        found = nearest_explore_target(w, {(1, 0), (3, 0)}, CostGridParams(), kb)
        self.assertEqual(found[0], (3, 0))

    def test_search_count_stays_bounded(self):
        kb = KnowledgeBase.empty("sandbox")
        kb.clues.append(clue("east", at=(0, 0)))
        w = world(["." * 40], at=(0, 0))
        targets = {(x, 0) for x in range(1, 40)}
        with mock.patch.object(planner, "_search", wraps=planner._search) as search:
            found = nearest_explore_target(w, targets, CostGridParams(), kb)
        self.assertEqual(found[0], (1, 0))
        self.assertLessEqual(search.call_count, 2, "early stop holds: no A* per frontier cell")


if __name__ == "__main__":
    unittest.main()
