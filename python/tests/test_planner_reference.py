"""A62: the game reference in the planner's prompt, and the script that refreshes it."""

from __future__ import annotations

import importlib.util
import re
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from agentrealm_agent.directives import Directives, PARAM_DEFAULTS
from agentrealm_agent.plan import Plan
from agentrealm_agent.planner_reference import (
    CORE_SECTIONS,
    REFERENCE_PATH,
    missing_sections,
    is_core,
    reference_text,
    select_sections,
    split_sections,
)
from agentrealm_agent.strategist import PROGRESSION, SYSTEM_PROMPT, build_prompt, estimate_tokens
from agentrealm_agent.world import WorldModel

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "refresh_planner_reference.py"


def prompt() -> list[dict]:
    return build_prompt(
        triggers=[{"trigger": "clue", "text": "a lantern"}],
        w=WorldModel(character_id=1, map_id=1, pos=(2, 3), tick=5),
        plan=Plan([{"op": "wait", "seconds": 0, "why": "test"}], dict(PARAM_DEFAULTS)),
        directives=Directives(params=dict(PARAM_DEFAULTS)),
        knowledge=SimpleNamespace(lock=threading.Lock(), clues=[], extra={}, entrances={}, items={}),
    )


class PromptTest(unittest.TestCase):
    def test_reference_comes_first_then_measured_facts_then_the_contract(self):
        system = prompt()[0]["content"]
        self.assertTrue(system.startswith("# Agent Realm reference"))
        notes = system.index("# Measured facts (docs/GAME_NOTES.md)")
        contract = system.index("# Planner contract")
        self.assertLess(notes, contract)
        self.assertIn("## Answers in one screen", system[notes:contract])  # GAME_NOTES.md itself
        self.assertIn(SYSTEM_PROMPT.strip(), system[contract:])
        self.assertIn("trust the measured facts", system[contract:])

    def test_progression_sits_between_the_facts_and_the_contract(self):
        system = prompt()[0]["content"]
        stages = system.index(PROGRESSION)
        self.assertLess(system.index("# Measured facts"), stages)
        self.assertLess(stages, system.index("# Planner contract"))
        for stage in ("1. Survive and learn", "2. Build up loot", "3. Beat levels", "4. Beat the world"):
            self.assertIn(stage, PROGRESSION)
        for op in ("explore_area", "gather_gems", "equip", "enter_level", "fight_boss"):
            self.assertIn(op, PROGRESSION)

    def test_system_prefix_carries_the_cache_marker(self):
        system = prompt()[0]
        self.assertEqual((system["role"], system["cache"]), ("system", True))

    def test_every_field_the_progression_names_is_in_state(self):
        named = re.search(r"Judge the stage from State \(([^)]*)\)", PROGRESSION).group(1)
        fields = [f.strip() for f in named.split(",")]
        self.assertTrue({"health", "armed", "worn", "held", "levels_cleared", "level_count"} <= set(fields))
        state = prompt()[1]["content"].split("State:\n", 1)[1].split("\n\n", 1)[0]
        for field in fields:
            self.assertRegex(state, rf"\b{field}=", field)

    def test_world_reads_levels_cleared_from_a_snapshot(self):
        w = WorldModel(character_id=1)
        w._apply_body_scalars({"levels_cleared": [2, 1, 2]})
        self.assertEqual(w.levels_cleared, [1, 2])

    def test_dynamic_part_comes_after_the_cached_prefix(self):
        messages = prompt()
        self.assertEqual([m["role"] for m in messages], ["system", "user"])
        self.assertNotIn("cache", messages[1])
        self.assertIn("a lantern", messages[1]["content"])
        self.assertNotIn("a lantern", messages[0]["content"])
        self.assertIn("tick=5", messages[1]["content"])
        # Only the dynamic part is charged against tokens_per_min.
        self.assertLess(estimate_tokens(messages), 2_000)

    def test_prefix_is_identical_across_calls(self):
        self.assertEqual(prompt()[0], prompt()[0])


class SectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sections = split_sections(REFERENCE_PATH.read_text(encoding="utf-8"))

    def test_every_core_entry_matches_a_section(self):
        self.assertEqual(missing_sections(self.sections), [])
        keys = [s.key for s in self.sections]
        for core in CORE_SECTIONS:
            self.assertTrue(any(k.startswith(core) for k in keys), core)

    def test_missing_sections_names_a_renamed_heading(self):
        text = REFERENCE_PATH.read_text(encoding="utf-8").replace("### 11. Game rules", "### 11. Rules of play")
        self.assertEqual(missing_sections(split_sections(text)), ["/docs/manual / 11. game rules"])

    def test_default_keeps_core_and_stays_under_the_budget(self):
        picked = select_sections(self.sections, "", budget=60_000)
        self.assertTrue(all(s in picked for s in self.sections if is_core(s)))
        self.assertLessEqual(sum(s.tokens for s in picked), 60_000)

    def test_setup_sections_match_by_key_not_substring(self):
        picked = {s.key for s in select_sections(self.sections, "", budget=10**9)}
        self.assertIn("/docs/guides/create-a-character-agent / identity", picked)
        for setup in ("/docs/manual / 4. accounts and keys", "/docs/api / accounts and access"):
            self.assertNotIn(setup, picked)
        self.assertFalse(any(k.startswith("/docs/changelog") for k in picked))

    def test_default_reference_has_the_round_trip(self):
        self.assertIn("### 7. The round trip", reference_text(""))

    def test_core_only_and_a_named_section(self):
        core = select_sections(self.sections, "core")
        self.assertTrue(all(is_core(s) for s in core))
        named = select_sections(self.sections, "changelog")
        self.assertTrue(any("changelog" in s.key for s in named))
        self.assertEqual(len(select_sections(self.sections, "all")), len(self.sections))

    def test_left_out_sections_are_named_with_their_reason(self):
        default = reference_text("")
        self.assertRegex(default, r"Sections left out as setup and history: [^)]*/docs/changelog")
        self.assertNotRegex(default, r"left out for size: [^)]*changelog")
        core = reference_text("core")
        self.assertIn("Sections left out as not asked for:", core)
        self.assertNotIn("left out for size", core)

    def test_code_fence_hash_is_not_a_section(self):
        text = "# page: /docs/x — X\n\n### A\n\n```\n### not a heading\n```\n### B\nb\n"
        self.assertEqual([s.key for s in split_sections(text)], ["/docs/x / ", "/docs/x / a", "/docs/x / b"])


class RefreshScriptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        spec = importlib.util.spec_from_file_location("refresh_planner_reference", SCRIPT)
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)

    def test_fixture_page_becomes_markdown_without_boilerplate(self):
        html = (FIXTURES / "reference_page.html").read_text(encoding="utf-8")
        title, md, links = self.mod.page_markdown(html)
        self.assertEqual(title, "Test Manual")
        self.assertIn("## 11. Game rules", md)
        self.assertIn("- A `move` intent takes **one tick** per tile.", md)
        self.assertIn("| Item | Cost |", md)
        self.assertIn("| small_potion | 5 |", md)
        self.assertIn('```\n{"intents": [{"type": "move"}]}\n```', md)
        self.assertNotIn("Sign in", md)  # nav
        self.assertNotIn("Footer text", md)
        self.assertNotIn("console.log", md)
        self.assertEqual(links, {"/docs", "/docs/api", "/guides/state-machine"})

    def test_refresh_fails_when_a_core_section_is_missing(self):
        pages = [("/docs/manual", "Manual", "## Manual\n\n### 1. Contract on one screen\n\nx\n")]
        with self.assertRaises(SystemExit) as e:
            self.mod.check_core(self.mod.render(pages, __import__("datetime").date(2026, 1, 2)))
        self.assertIn("11. game rules", str(e.exception))

    def test_render_puts_source_and_date_on_top(self):
        import datetime as dt

        out = self.mod.render([("/docs/x", "X", "## X\n\n### Y\n\n```\n# shell comment\n```\n")], dt.date(2026, 1, 2))
        self.assertIn("Source: https://agentrealm.gg/docs and https://agentrealm.gg/guides", out.splitlines()[2])
        self.assertIn("Fetched 2026-01-02", out)
        self.assertIn("# page: /docs/x — X\n\n### X\n\n#### Y", out)
        self.assertIn("```\n# shell comment\n```", out)  # code is not demoted


if __name__ == "__main__":
    unittest.main()
