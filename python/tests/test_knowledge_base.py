"""Per-world knowledge base load/save and sections."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

from agentrealm_agent import __main__ as cli
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
        base.items["bronze_sword"] = {"weapon_damage": {"wolf": 2}, "attack_range": 1}
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

    def _write(self, world: str, raw) -> None:
        path = kb.world_path(world)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(raw if isinstance(raw, str) else json.dumps(raw))

    def test_invalid_json(self) -> None:
        self._write("sandbox", "{not json")
        with self.assertRaisesRegex(kb.KnowledgeBaseError, "invalid JSON"):
            kb.load("sandbox")

    def test_root_not_object(self) -> None:
        self._write("sandbox", [])
        with self.assertRaisesRegex(kb.KnowledgeBaseError, "JSON object"):
            kb.load("sandbox")

    def test_wrong_typed_sections(self) -> None:
        for key, bad in (("maps", []), ("clues", {}), ("compose", "x"), ("levels", 3)):
            with self.subTest(key=key):
                self._write("sandbox", {"schema_version": 1, key: bad})
                with self.assertRaisesRegex(kb.KnowledgeBaseError, key):
                    kb.load("sandbox")

    def test_schema_version_too_new(self) -> None:
        self._write("sandbox", {"schema_version": kb.SCHEMA_VERSION + 1})
        with self.assertRaisesRegex(kb.KnowledgeBaseError, "unsupported"):
            kb.load("sandbox")

    def test_schema_version_not_int(self) -> None:
        for bad in ("1", 1.0, True, None):
            with self.subTest(bad=bad):
                self._write("sandbox", {"schema_version": bad})
                with self.assertRaisesRegex(kb.KnowledgeBaseError, "integer"):
                    kb.load("sandbox")

    def test_save_leaves_no_temp_files(self) -> None:
        kb.save(kb.KnowledgeBase.empty("sandbox"))
        kb.save(kb.KnowledgeBase.empty("sandbox"))
        self.assertEqual([p.name for p in self.worlds.iterdir()], ["sandbox.json"])


class RunWiringTest(unittest.TestCase):
    """`run` loads one knowledge base per world, shares it, and saves it at exit."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.worlds = Path(self.tmp.name) / "worlds"
        patch = mock.patch.object(kb, "WORLDS_DIR", self.worlds)
        patch.start()
        self.addCleanup(patch.stop)
        self.seen: list[tuple[str, kb.KnowledgeBase]] = []
        seen = self.seen

        class FakeRunner:
            def __init__(self, cfg, client, cid, stop, out, knowledge=None):
                self.cfg, self.knowledge = cfg, knowledge

            def run(self) -> None:
                seen.append((self.cfg.name, self.knowledge))
                with self.knowledge.lock:
                    self.knowledge.clues.append({"kind": "sign", "text": self.cfg.name})

        patch = mock.patch.object(cli, "Runner", FakeRunner)
        patch.start()
        self.addCleanup(patch.stop)

    def _run(self, worlds: list[str]) -> tuple[int, str, str]:
        ids = [(SimpleNamespace(name=f"c{i}", world=w), i) for i, w in enumerate(worlds)]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "_ids", return_value=ids), redirect_stdout(out), redirect_stderr(err):
            code = cli.run(mock.Mock(), [])
        return code, out.getvalue(), err.getvalue()

    def test_one_base_per_world_saved_at_exit(self) -> None:
        with mock.patch.object(cli, "load_knowledge", wraps=kb.load) as load:
            code, _, _ = self._run(["sandbox", "sandbox", "olympuff"])
        self.assertEqual(code, 0)
        self.assertEqual(sorted(c.args[0] for c in load.call_args_list), ["olympuff", "sandbox"])
        by_name = dict(self.seen)
        self.assertIs(by_name["c0"], by_name["c1"])
        self.assertIsNot(by_name["c0"], by_name["c2"])
        self.assertEqual(sorted(c["text"] for c in kb.load("sandbox").clues), ["c0", "c1"])
        self.assertEqual([c["text"] for c in kb.load("olympuff").clues], ["c2"])

    def test_bad_file_stops_before_any_character_runs(self) -> None:
        path = kb.world_path("sandbox")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")
        code, _, err = self._run(["sandbox"])
        self.assertEqual(code, 2)
        self.assertIn("invalid JSON", err)
        self.assertEqual(self.seen, [])
        self.assertEqual(path.read_text(), "{not json")

    def test_bad_world_code_stops_before_any_character_runs(self) -> None:
        code, _, err = self._run(["../escape"])
        self.assertEqual(code, 2)
        self.assertIn("invalid world code", err)
        self.assertEqual(self.seen, [])

    def test_save_error_is_reported_per_world(self) -> None:
        real_save = kb.save

        def save(base: kb.KnowledgeBase) -> None:
            if base.world_code == "sandbox":
                raise OSError("disk full")
            real_save(base)

        with mock.patch.object(cli, "save_knowledge", side_effect=save):
            code, out, _ = self._run(["sandbox", "olympuff"])
        self.assertEqual(code, 0)
        self.assertIn("knowledge base sandbox: not saved: disk full", out)
        self.assertEqual(len(kb.load("olympuff").clues), 1)


if __name__ == "__main__":
    unittest.main()
