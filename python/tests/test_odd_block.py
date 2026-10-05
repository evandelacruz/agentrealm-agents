"""Odd-block detector and Break curiosity nominations (A31)."""

import random
import unittest

from agentrealm_agent.break_memory import record_attempt
from agentrealm_agent.config import Policy
from agentrealm_agent.curiosity_budget import cap_ticks
from agentrealm_agent.item_table import InventorySupply
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.memory import Memory
from agentrealm_agent.interest_list import MAX_REJECTIONS
from agentrealm_agent.odd_block import is_odd_block, note_odd_unreachable, pick_odd_break
from agentrealm_agent.states import dispatch
from agentrealm_agent.states.base import PlayContext
from agentrealm_agent.states.break_state import BreakState, break_outcome
from agentrealm_agent.world import WorldModel


def _tiles(rows: list[str], glyph: dict[str, str] | None = None) -> dict[tuple[int, int], str]:
    g = glyph or {".": "grass", "b": "bush", "r": "rock", "w": "wall", "B": "bush"}
    out: dict[tuple[int, int], str] = {}
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            out[(x, y)] = g[ch]
    return out


class OddBlockDetectorTest(unittest.TestCase):
    def test_one_bush_in_grass_is_odd(self):
        tiles = _tiles(
            [
                ".........",
                ".........",
                "..b......",
                ".........",
                ".........",
                ".........",
                ".........",
            ]
        )
        self.assertTrue(is_odd_block(tiles, (2, 2)))

    def test_bush_in_a_hedge_line_is_not_odd(self):
        tiles = _tiles(["bbbbbbb"] * 7)
        self.assertFalse(is_odd_block(tiles, (3, 3)))

    def test_non_breakable_is_not_odd(self):
        tiles = _tiles(["......."] * 7, {".": "grass", "~": "water"})
        tiles[(3, 3)] = "water"
        self.assertFalse(is_odd_block(tiles, (3, 3)))


class PickOddBreakTest(unittest.TestCase):
    def _world(self, rows: list[str], at=(0, 0)) -> WorldModel:
        w = WorldModel(character_id=1, map_id=1, pos=at, perception=8)
        w.view.tiles = _tiles(rows)
        w.terrain_center, w.terrain_map = at, 1
        w.held_supplies = [InventorySupply(1, "bronze_sword")]
        return w

    def test_picks_the_lone_bush(self):
        rows = ["........."] * 9
        rows[4] = "....b...."
        w = self._world(rows, at=(4, 4))
        m = Memory()
        params = {"curiosity": 0.2}
        choice = pick_odd_break(w, None, Policy(kind="scripted"), m, params=params)
        self.assertIsNotNone(choice)
        self.assertEqual(choice.pos, (4, 4))

    def test_respects_curiosity_cap(self):
        rows = ["........."] * 9
        rows[4] = "....b...."
        w = self._world(rows, at=(4, 4))
        w.tick = 600
        params = {"curiosity": 0.2}
        m = Memory()
        m.curiosity_spans = [(1, cap_ticks(0.2) - 1)]
        self.assertIsNotNone(pick_odd_break(w, None, Policy(kind="scripted"), m, params=params))
        m = Memory()
        m.curiosity_spans = [(1, cap_ticks(0.2))]
        self.assertIsNone(pick_odd_break(w, None, Policy(kind="scripted"), m, params=params))

    def test_clue_boost_allows_matches_on_high_score(self):
        rows = ["........."] * 9
        rows[4] = "....b...."
        w = self._world(rows, at=(4, 4))
        w.held_supplies = [InventorySupply(2, "matches")]
        kb = KnowledgeBase.empty("sandbox")
        kb.clues.append({"text": "the bush rings hollow", "map_id": 1, "x": 4, "y": 4})
        m = Memory()
        choice = pick_odd_break(w, kb, Policy(kind="scripted"), m, params={"curiosity": 0.2})
        self.assertIsNotNone(choice)
        self.assertEqual(choice.capability, "burn")

    def test_clue_on_another_map_does_not_allow_matches(self):
        rows = ["........."] * 9
        rows[4] = "....b...."
        w = self._world(rows, at=(4, 4))
        w.held_supplies = [InventorySupply(2, "matches")]
        kb = KnowledgeBase.empty("sandbox")
        kb.clues.append({"text": "the bush rings hollow", "map_id": 2, "x": 4, "y": 4})
        m = Memory()
        self.assertIsNone(pick_odd_break(w, kb, Policy(kind="scripted"), m, params={"curiosity": 0.2}))

    def test_sticky_target_is_scoped_to_its_map(self):
        rows = ["........."] * 9
        rows[4] = "....b...."
        w = self._world(rows, at=(4, 4))
        m = Memory()
        m.break_odd = (2, (4, 4))
        choice = pick_odd_break(w, None, Policy(kind="scripted"), m, params={"curiosity": 0.2}, stick_to=m.break_odd)
        self.assertIsNotNone(choice)
        self.assertEqual(m.break_odd, (1, (4, 4)))

    def test_unreachable_target_is_dropped_after_the_refusal_cap(self):
        rows = ["........."] * 9
        rows[4] = "....b...."
        w = self._world(rows, at=(4, 4))
        m = Memory()
        params = {"curiosity": 0.2}
        for _ in range(MAX_REJECTIONS):
            self.assertIsNotNone(pick_odd_break(w, None, Policy(kind="scripted"), m, params=params, stick_to=m.break_odd))
            self.assertEqual(m.break_odd, (1, (4, 4)))
            note_odd_unreachable(m, 1, (4, 4))
            self.assertIsNone(m.break_odd)
        self.assertIsNone(pick_odd_break(w, None, Policy(kind="scripted"), m, params=params, stick_to=(1, (4, 4))))
        self.assertIsNone(m.break_odd)

    def test_skips_block_with_every_capability_failed(self):
        rows = ["........."] * 9
        rows[4] = "....b...."
        w = self._world(rows, at=(4, 4))
        kb = KnowledgeBase.empty("sandbox")
        for cap in ("cut", "chop"):
            record_attempt(kb, map_id=1, pos=(4, 4), capability=cap, result="applied_no_effect")
        m = Memory()
        self.assertIsNone(pick_odd_break(w, kb, Policy(kind="scripted"), m, params={"curiosity": 0.2}))


class BreakOddDispatchTest(unittest.TestCase):
    def test_break_runs_for_odd_block_without_plan(self):
        rows = ["........."] * 9
        rows[4] = "....b...."
        w = WorldModel(character_id=1, map_id=1, pos=(4, 4), perception=8)
        w.view.tiles = _tiles(rows)
        w.terrain_center, w.terrain_map = (4, 4), 1
        w.held_supplies = [InventorySupply(1, "bronze_sword")]
        w.armed_code = "bronze_sword"
        ctx = PlayContext(Memory(), Policy(kind="scripted", goals=["explore"]), random.Random(0))
        self.assertTrue(BreakState().guard(w, ctx))
        out = dispatch(w, ctx)
        self.assertEqual(out.state, "Break")
        self.assertTrue(out.intents)

    def test_no_route_counts_against_the_odd_target(self):
        rows = ["........."] * 9
        rows[1] = "....b...."
        w = WorldModel(character_id=1, map_id=1, pos=(4, 5), perception=8)
        w.view.tiles = _tiles(rows)
        for x in range(3, 6):  # water all round the agent
            for y in range(4, 7):
                if (x, y) != (4, 5):
                    w.view.tiles[(x, y)] = "water"
        w.terrain_center, w.terrain_map = (4, 5), 1
        w.held_supplies = [InventorySupply(1, "bronze_sword")]
        w.armed_code = "bronze_sword"
        ctx = PlayContext(Memory(), Policy(kind="scripted", goals=["explore"]), random.Random(0))
        out = break_outcome(w, ctx.memory, None, never_attack=[], ctx=ctx)
        self.assertIsNone(out.intents)
        self.assertIsNone(ctx.memory.break_odd)
        self.assertEqual(ctx.memory.break_odd_refusals, {(1, (4, 1)): 1})
