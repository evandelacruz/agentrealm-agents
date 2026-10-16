"""A134: oddities, blocks out of place, are shown to the planner and remembered."""

import json
import unittest

from agentrealm_adk.break_memory import record_attempt
from agentrealm_adk.directives import PARAM_DEFAULTS, Directives
from agentrealm_adk.item_table import InventorySupply
from agentrealm_adk.knowledge_base import KnowledgeBase
from agentrealm_adk.oddities import ODDITIES_KEY, RETURN_SECONDS, find, oddities_line
from agentrealm_agent.plan import Plan
from agentrealm_agent.strategist import build_prompt, system_prompt
from agentrealm_adk.world import WorldModel

TICK_HZ = 10


def _world(rows: list[str], at=(0, 0), held=("mallet",)) -> WorldModel:
    glyph = {",": "grass", ".": "dirt", "r": "rock", "b": "bush"}
    w = WorldModel(character_id=1, map_id=7, pos=at, tick=1000)
    for y, row in enumerate(rows):
        for x, g in enumerate(row):
            w.view.tiles[(x, y)] = glyph[g]
    w.held_supplies = [InventorySupply(i + 1, code) for i, code in enumerate(held)]
    return w


GARDEN = [",,,,,,,,,"] * 4 + [",,,,r,,,,"] + [",,,,,,,,,"] * 4
FIELD = ["rrrrrrrrr"] * 9


def _rows(line: str) -> list[dict]:
    return json.loads(line.split("=", 1)[1].split(" (+")[0])


class FindTest(unittest.TestCase):
    def test_a_lone_rock_in_grass_is_listed(self):
        w = _world(GARDEN, at=(1, 1))
        self.assertEqual(find(w), {(4, 4): "rock"})
        (row,) = _rows(oddities_line(w, KnowledgeBase.empty("w"), TICK_HZ))
        self.assertEqual((row["x"], row["y"], row["block"]), (4, 4, "rock"))
        self.assertEqual(row["may_break_with"], ["smash"])

    def test_a_field_of_rocks_is_not(self):
        w = _world(FIELD, at=(4, 4))
        self.assertEqual(find(w), {})
        self.assertEqual(oddities_line(w, None, TICK_HZ), "oddities=none seen")

    def test_a_rock_with_no_held_opener_says_none_held(self):
        w = _world(GARDEN, at=(1, 1), held=("bronze_sword",))
        (row,) = _rows(oddities_line(w, None, TICK_HZ))
        self.assertEqual(row["may_break_with"], "none held")

    def test_too_little_seen_round_it_is_not_judged(self):
        w = _world([",,,", ",r,", ",,,"], at=(0, 0))
        self.assertEqual(find(w), {})


class RememberedTest(unittest.TestCase):
    def test_a_broken_oddity_carries_its_return_time(self):
        kb = KnowledgeBase.empty("w")
        w = _world(GARDEN, at=(1, 1))
        oddities_line(w, kb, TICK_HZ)
        self.assertIn("7,4,4", kb.extra[ODDITIES_KEY])
        record_attempt(kb, map_id=7, pos=(4, 4), capability="smash", result="opened", tick=1200, code="mallet")
        w.view.tiles[(4, 4)] = "grass"  # broken: no block shows there now
        (row,) = _rows(oddities_line(w, kb, TICK_HZ))
        self.assertEqual(row["block"], "rock")
        self.assertEqual(row["last_try"], {"capability": "smash", "result": "opened", "tick": 1200})
        self.assertEqual(row["back_at_tick"], 1200 + RETURN_SECONDS * TICK_HZ)

    def test_a_remembered_oddity_seen_more_fully_and_no_longer_odd_is_dropped(self):
        kb = KnowledgeBase.empty("w")
        w = _world(GARDEN, at=(1, 1))
        oddities_line(w, kb, TICK_HZ)
        self.assertIn("7,4,4", kb.extra[ODDITIES_KEY])
        w.view.tiles[(5, 5)] = "rock"  # more seen: another rock beside it
        self.assertEqual(oddities_line(w, kb, TICK_HZ), "oddities=none seen")
        self.assertNotIn("7,4,4", kb.extra[ODDITIES_KEY])

    def test_a_failed_capability_is_recorded_and_not_offered(self):
        kb = KnowledgeBase.empty("w")
        w = _world(GARDEN, at=(1, 1), held=("mallet", "bomb"))
        record_attempt(kb, map_id=7, pos=(4, 4), capability="smash", result="applied_no_effect", tick=1100)
        (row,) = _rows(oddities_line(w, kb, TICK_HZ))
        self.assertEqual(row["failed"], ["smash"])
        self.assertEqual(row["last_try"]["result"], "applied_no_effect")
        self.assertNotIn("smash", row["may_break_with"])


class PlannerSeesItTest(unittest.TestCase):
    def test_state_and_contract_carry_oddities(self):
        w = _world(GARDEN, at=(1, 1))
        msgs = build_prompt(
            triggers=[], w=w, plan=Plan([], dict(PARAM_DEFAULTS)), directives=Directives(), knowledge=None
        )
        self.assertIn("oddities=[", msgs[1]["content"])
        self.assertIn("5, 7 or 10 gems", system_prompt())


if __name__ == "__main__":
    unittest.main()
