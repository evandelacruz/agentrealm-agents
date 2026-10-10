"""The Manual's Supplies reference (A54)."""

import http.client
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentrealm_agent import supplies
from agentrealm_agent.knowledge_base import KnowledgeBase
from tests import REAL_FETCH

SERVED = [
    {"code": "cleaver", "class": "weapon", "slot": "armed", "use_effects": ["attack", "cut", "chop"],
     "attack_range": 2, "damage": 9, "used_up_on_break": False, "stacks": False},
    {"code": "flare", "class": "tool", "slot": "armed", "use_effects": ["burn", "light"], "used_up_on_break": True},
    {"code": "plum", "class": "consumable", "use_effects": [], "heal": 2, "eaten_on_pickup": True},
    {"code": "tonic", "class": "consumable", "slot": "armed", "use_effects": ["heal"], "heal": 10},
    {"code": "gem", "class": "gem", "use_effects": [], "stacks": True, "eaten_on_pickup": True},
    {"name": "no code"},
    "not a row",
]


class ParseTest(unittest.TestCase):
    def test_rows_by_code_skip_malformed(self):
        rows = supplies.parse(SERVED)
        self.assertEqual(sorted(rows), ["cleaver", "flare", "gem", "plum", "tonic"])
        self.assertEqual(rows["cleaver"].attack_range, 2)
        self.assertFalse(rows["cleaver"].used_up_on_break)
        self.assertIsNone(rows["flare"].attack_range)
        self.assertEqual(supplies.parse({"rows": []}), {})

    def test_bundled_copy_lists_the_starting_kit(self):
        rows = supplies.parse(json.loads(supplies.BUNDLED_PATH.read_text()))
        self.assertEqual(rows["pocket_knife"].use_effects, frozenset({"attack", "cut"}))
        self.assertEqual(rows["pocket_knife"].attack_range, 1)


class QuestionsTest(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(supplies, "_table", supplies.parse(SERVED))
        patch.start()
        self.addCleanup(patch.stop)

    def test_food_heals_on_pickup_and_a_potion_on_use(self):
        self.assertTrue(supplies.is_food("plum"))
        self.assertFalse(supplies.is_food("gem"), "eaten on pickup but heals nothing")
        self.assertTrue(supplies.is_potion("tonic"))
        self.assertFalse(supplies.is_potion("plum"))
        self.assertTrue(supplies.heals("tonic") and supplies.heals("plum"))
        self.assertFalse(supplies.heals("unlisted") or supplies.heals(None))

    def test_weapons_and_breaks(self):
        self.assertTrue(supplies.is_weapon("cleaver"))
        self.assertFalse(supplies.is_weapon("flare"))
        self.assertTrue(supplies.kept_on_break("cleaver"))
        self.assertFalse(supplies.kept_on_break("flare"))
        self.assertFalse(supplies.kept_on_break("unlisted"))
        self.assertEqual(supplies.break_capabilities("cleaver"), frozenset({"cut", "chop"}))
        self.assertEqual(supplies.break_capabilities("flare"), frozenset({"burn"}), "light opens no block")
        self.assertEqual(supplies.break_capabilities("unlisted"), frozenset())

    def test_block_reach_defaults_to_the_next_block(self):
        self.assertEqual(supplies.block_reach("cleaver"), 2)
        self.assertEqual(supplies.block_reach("flare"), 1)
        self.assertEqual(supplies.block_reach(None), 1)

    def test_capabilities_filed_on_the_item_table_grow_only(self):
        items = {"flare": {"capabilities": ["smash"], "gem_price": 4}}
        supplies.file_capabilities(items)
        self.assertEqual(items["cleaver"], {"capabilities": ["chop", "cut"]})
        self.assertEqual(items["flare"], {"capabilities": ["burn", "smash"], "gem_price": 4})
        self.assertNotIn("plum", items, "a supply with no break capability makes no row")

    def test_load_for_run_files_the_world_items(self):
        kb = KnowledgeBase.empty("sandbox")
        lines: list[str] = []
        with mock.patch.object(supplies, "load", return_value="bundled"):
            supplies.load_for_run(kb, lines.append)
        self.assertEqual(kb.items["cleaver"]["capabilities"], ["chop", "cut"])
        self.assertEqual(lines, ["supplies reference: 5 subtypes (bundled)"])


class FetchTest(unittest.TestCase):
    def test_a_truncated_or_failed_answer_is_none(self):
        for error in (http.client.IncompleteRead(b"[{"), http.client.BadStatusLine("x"), OSError("reset")):
            with mock.patch.object(supplies.urllib.request, "urlopen", side_effect=error):
                self.assertIsNone(REAL_FETCH("https://example.invalid/supplies.json"), type(error).__name__)


class LoadTest(unittest.TestCase):
    """Fresh cache, then the site, then any cache, then the bundled copy."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache = Path(tmp.name) / "state" / "supplies.json"
        patch = mock.patch.object(supplies, "_table", {})
        patch.start()
        self.addCleanup(patch.stop)
        self.fetches = 0

    def fetcher(self, raw):
        def fetch():
            self.fetches += 1
            return raw
        return fetch

    def write_cache(self, rows, age_seconds):
        self.cache.parent.mkdir(parents=True, exist_ok=True)
        self.cache.write_text(json.dumps(rows))
        t = 1_000_000.0 - age_seconds
        os.utime(self.cache, (t, t))

    def load(self, raw):
        return supplies.load(fetcher=self.fetcher(raw), cache_path=self.cache, now=lambda: 1_000_000.0)

    def test_served_table_is_used_and_cached(self):
        self.assertEqual(self.load(SERVED), "served")
        self.assertTrue(supplies.is_weapon("cleaver"))
        self.assertEqual(json.loads(self.cache.read_text()), SERVED)

    def test_fresh_cache_skips_the_fetch(self):
        self.write_cache(SERVED, age_seconds=60)
        self.assertEqual(self.load(None), "cache")
        self.assertEqual(self.fetches, 0)
        self.assertTrue(supplies.is_food("plum"))

    def test_stale_cache_is_refetched(self):
        self.write_cache([SERVED[2]], age_seconds=supplies.CACHE_MAX_AGE_SECONDS + 1)
        self.assertEqual(self.load(SERVED), "served")
        self.assertEqual(self.fetches, 1)
        self.assertTrue(supplies.is_weapon("cleaver"))

    def test_unreachable_site_falls_back_to_a_stale_cache(self):
        self.write_cache(SERVED, age_seconds=supplies.CACHE_MAX_AGE_SECONDS * 10)
        self.assertEqual(self.load(None), "stale cache")
        self.assertTrue(supplies.is_weapon("cleaver"))

    def test_a_failed_cache_save_leaves_no_temp_file(self):
        with mock.patch.object(supplies.os, "replace", side_effect=OSError("disk full")):
            self.assertEqual(self.load(SERVED), "served")
        self.assertEqual(list(self.cache.parent.iterdir()), [])

    def test_defaults_are_read_on_each_call(self):
        # tests/__init__.py turns fetching off by patching these module names.
        with mock.patch.object(supplies, "fetch", self.fetcher(SERVED)), \
                mock.patch.object(supplies, "CACHE_PATH", self.cache):
            self.assertEqual(supplies.load(), "served")
        self.assertTrue(self.cache.exists())

    def test_unreachable_site_and_no_cache_fall_back_to_the_bundled_copy(self):
        self.assertEqual(self.load([]), "bundled")
        self.assertTrue(supplies.is_weapon("pocket_knife"))
        self.assertFalse(self.cache.exists(), "an empty answer is not cached")


if __name__ == "__main__":
    unittest.main()
