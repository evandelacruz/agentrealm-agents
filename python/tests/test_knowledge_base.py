"""Per-world knowledge base load/save and sections."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentrealm_agent import knowledge_base as kb


class KnowledgeBaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.worlds = Path(self.tmp.name) / "worlds"
        patch = mock.patch.object(kb, "WORLDS_DIR", self.worlds)
        patch.start()
        self.addCleanup(patch.stop)

    def test_missing_file_is_empty(self) -> None:
        base = kb.load("sandbox")
        self.assertEqual(base.world_code, "sandbox")
        self.assertEqual(base.maps, {})
        self.assertEqual(base.clues, [])

    def test_roundtrip_sections(self) -> None:
        base = kb.KnowledgeBase.empty("olympuff")
        base.maps["12"] = {
            "terrain": {"10,20": "grass"},
            "doors": [{"x": 5, "y": 6, "to_map_id": 99, "to_x": 1, "to_y": 2}],
        }
        base.clues.append(
            {
                "kind": "sign",
                "text": "Turn left at the hollow tree.",
                "map_id": 12,
                "x": 10,
                "y": 20,
                "tick": 100,
            }
        )
        base.breaks["12,10,20,cut"] = {"result": "opened", "block_after": "dirt"}
        base.npc_types["wolf"] = {"damage_per_hit": 2}
        base.entrances["120,40"] = {"locked": True, "needs": "iron_key"}
        base.levels["3"] = {"cleared": True}
        base.compose.append({"whole": "master_key", "tick": 500})
        kb.save(base)

        again = kb.load("olympuff")
        self.assertEqual(again.to_dict(), base.to_dict())
        self.assertEqual(again.clues[0]["text"], "Turn left at the hollow tree.")

    def test_shared_by_characters_in_world(self) -> None:
        first = kb.KnowledgeBase.empty("sandbox")
        first.clues.append({"kind": "npc", "text": "hello", "map_id": 1, "x": 0, "y": 0})
        kb.save(first)
        second = kb.load("sandbox")
        self.assertEqual(len(second.clues), 1)
        second.clues.append({"kind": "scroll", "text": "fragment", "map_id": 1, "x": 1, "y": 1})
        kb.save(second)
        self.assertEqual(len(kb.load("sandbox").clues), 2)

    def test_invalid_world_code(self) -> None:
        with self.assertRaises(kb.KnowledgeBaseError):
            kb.load("../escape")
        with self.assertRaises(kb.KnowledgeBaseError):
            kb.KnowledgeBase.empty("")

    def test_world_code_mismatch(self) -> None:
        path = kb.world_path("sandbox")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"world_code": "other", "schema_version": 1}) + "\n")
        with self.assertRaisesRegex(kb.KnowledgeBaseError, "mismatch"):
            kb.load("sandbox")

    def test_unknown_top_level_keys_preserved(self) -> None:
        path = kb.world_path("sandbox")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "world_code": "sandbox",
                    "maps": {},
                    "future_section": {"note": "keep"},
                }
            )
            + "\n"
        )
        loaded = kb.load("sandbox")
        self.assertEqual(loaded.extra["future_section"], {"note": "keep"})
        kb.save(loaded)
        roundtrip = json.loads(path.read_text())
        self.assertEqual(roundtrip["future_section"], {"note": "keep"})


if __name__ == "__main__":
    unittest.main()
