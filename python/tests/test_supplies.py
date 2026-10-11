"""The Manual's Supplies reference (A54)."""

import dataclasses
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
        self.assertEqual(rows["cleaver"].damage, 9)
        self.assertFalse(rows["cleaver"].used_up_on_break)
        self.assertIsNone(rows["flare"].damage)
        self.assertEqual(supplies.parse({"rows": []}), {})

    def test_bundled_copy_lists_the_starting_kit(self):
        rows = supplies.parse(json.loads(supplies.BUNDLED_PATH.read_text()))
        self.assertEqual(rows["pocket_knife"].use_effects, frozenset({"attack", "cut"}))
        self.assertEqual(rows["pocket_knife"].damage, 2)
        self.assertEqual(rows["pocket_knife"].attack_cooldown_ms, 800)


# The same supplies as ``SERVED``, in the shape the site serves since saims
# B138: a ``capabilities`` list in place of ``use_effects`` and the stat fields.
SERVED_CAPABILITIES = [
    {"code": "cleaver", "class": "weapon", "slot": "armed", "capabilities": [
        {"capability": "attacks", "trigger": "used", "amount": 9, "reach": 2, "cooldown_ms": 1300},
        {"capability": "chops", "trigger": "used"}, {"capability": "cuts", "trigger": "used"}],
     "used_up_on_break": False, "stacks": False},
    {"code": "flare", "class": "tool", "slot": "armed", "capabilities": [
        {"capability": "burns", "trigger": "used"},
        {"capability": "lights", "trigger": "used", "reach": 8, "duration_seconds": 120}], "used_up_on_break": True},
    {"code": "plum", "class": "consumable", "capabilities": [
        {"capability": "heals", "trigger": "picked_up", "amount": 2}], "eaten_on_pickup": True},
    {"code": "tonic", "class": "consumable", "slot": "armed", "capabilities": [
        {"capability": "heals", "trigger": "used", "amount": 10}]},
    {"code": "gem", "class": "gem", "capabilities": [], "stacks": True, "eaten_on_pickup": True},
    {"name": "no code"},
    "not a row",
]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "supplies"


class CapabilitiesShapeTest(unittest.TestCase):
    """A123: the ``capabilities`` shape reads into the same ``Supply`` as the shape before it."""

    def test_both_shapes_parse_to_the_same_supply(self):
        old, new = supplies.parse(SERVED), supplies.parse(SERVED_CAPABILITIES)
        self.assertEqual(sorted(old), sorted(new))
        for code in old:
            with self.subTest(code=code):
                self.assertEqual(dataclasses.replace(new[code], attack_cooldown_ms=None), old[code])

    def test_the_whole_table_parses_alike_in_both_shapes(self):
        # The table as served before B138 and the bundled copy served after it.
        old = supplies.parse(json.loads((FIXTURES / "before_b138.json").read_text()))
        new = supplies.parse(json.loads(supplies.BUNDLED_PATH.read_text()))
        self.assertEqual(sorted(old), sorted(new))
        for code in old:
            with self.subTest(code=code):
                self.assertEqual(dataclasses.replace(new[code], attack_cooldown_ms=None), old[code])

    def test_the_attack_cooldown_is_read(self):
        rows = supplies.parse(SERVED_CAPABILITIES)
        self.assertEqual(rows["cleaver"].attack_cooldown_ms, 1300)
        self.assertEqual(rows["cleaver"].attack_range, 2)
        self.assertIsNone(rows["flare"].attack_cooldown_ms)
        self.assertIsNone(supplies.parse(SERVED)["cleaver"].attack_cooldown_ms, "the shape before B138 has none")

    def test_every_question_answers_alike_on_both_tables(self):
        # What loot, healing, break memory, equip and State read, per subtype.
        questions = (
            supplies.is_food, supplies.is_potion, supplies.heals, supplies.eaten_on_pickup, supplies.is_weapon,
            supplies.kept_on_break, supplies.break_capabilities, supplies.weapon_damage, supplies.attack_reach,
            supplies.armor_defense, supplies.used_on_self, supplies.chest_capacity, supplies.worn, supplies.worn_slot,
        )
        old = supplies.parse(json.loads((FIXTURES / "before_b138.json").read_text()))
        new = supplies.parse(json.loads(supplies.BUNDLED_PATH.read_text()))
        answers = {}
        for name, table in (("old", old), ("new", new)):
            with mock.patch.object(supplies, "_table", table):
                answers[name] = {
                    (q.__name__, code): q(code) for q in questions for code in [*old, "unlisted", None]
                } | {"sold": supplies.sold_codes()}
        self.assertEqual(answers["old"], answers["new"])

    def test_a_weapon_with_no_cooldown_listed_swings_at_the_default(self):
        with mock.patch.object(supplies, "_table", supplies.parse(SERVED)):
            self.assertEqual(supplies.weapon_cooldown_ms("cleaver"), supplies.DEFAULT_ATTACK_COOLDOWN_MS)
            self.assertIsNone(supplies.weapon_cooldown_ms("unlisted"))

    def test_armor_without_protects_adds_no_defense(self):
        rows = supplies.parse([
            {"code": "boots", "class": "armor", "slot": "feet", "capabilities": [
                {"capability": "defend-the-feet", "trigger": "worn"},
                {"capability": "speeds", "trigger": "equipped", "amount": 500}]},
            {"code": "mail", "class": "armor", "slot": "body", "capabilities": [
                {"capability": "defend-the-body", "trigger": "worn"},
                {"capability": "protects", "trigger": "worn", "amount": 2}]},
            {"code": "lens", "class": "accessory", "slot": "accessory", "capabilities": [
                {"capability": "unmasks", "trigger": "equipped"}]},
        ])
        self.assertEqual((rows["boots"].defense, rows["mail"].defense, rows["lens"].defense), (0, 2, None))
        self.assertEqual(rows["boots"].use_effects, frozenset(), "a worn capability no rule reads names no use effect")


class QuestionsTest(unittest.TestCase):
    served = SERVED

    def setUp(self):
        patch = mock.patch.object(supplies, "_table", supplies.parse(self.served))
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

    def test_weapon_damage_is_for_weapons_only(self):
        self.assertEqual(supplies.weapon_damage("cleaver"), 9)
        self.assertIsNone(supplies.weapon_damage("flare"))
        self.assertIsNone(supplies.weapon_damage(None))

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


class CapabilitiesQuestionsTest(QuestionsTest):
    """A123: every question answers the same on the ``capabilities`` shape."""

    served = SERVED_CAPABILITIES

    def test_weapon_cooldown_and_reach(self):
        self.assertEqual(supplies.weapon_cooldown_ms("cleaver"), 1300)
        self.assertEqual(supplies.attack_reach("cleaver"), 2)
        self.assertIsNone(supplies.weapon_cooldown_ms("flare"))
        self.assertIsNone(supplies.attack_reach("tonic"))
        self.assertEqual(supplies.what_it_does("cleaver")["cooldown_ms"], 1300)


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


class WornTest(unittest.TestCase):
    """A95: what the reference says about wearing a subtype."""

    def test_worn_classes_and_their_slots(self):
        for code, worn, slot in (
            ("iron_mail", True, "body"),
            ("goggles", True, "accessory"),
            ("matches", False, ""),
            ("torch", False, ""),
            ("apple", False, ""),
            ("bronze_sword", False, ""),
            ("gem", False, ""),
            ("looted_helm", None, ""),
            (None, None, ""),
        ):
            with self.subTest(code=code):
                self.assertIs(supplies.worn(code), worn)
                self.assertEqual(supplies.worn_slot(code), slot)


if __name__ == "__main__":
    unittest.main()
