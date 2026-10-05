"""Odd-block detector and Break curiosity nominations (A31)."""

import importlib
import random
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agentrealm_agent import config
from agentrealm_agent.break_memory import attempt_open, record_attempt
from agentrealm_agent.config import CharacterConfig, Policy
from agentrealm_agent.curiosity_budget import cap_ticks
from agentrealm_agent.item_table import InventorySupply
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.memory import Memory
from agentrealm_agent.interest_list import MAX_REJECTIONS
from agentrealm_agent.odd_block import is_odd_block, note_odd_unreachable, pick_odd_break
from agentrealm_agent.runner import Runner
from agentrealm_agent.states import dispatch
from agentrealm_agent.states.base import PlayContext, StateOutcome
from agentrealm_agent.states.boss import BossState
from agentrealm_agent.states.intents import arm, use_block
from agentrealm_agent.states.break_state import BreakState, OddBreakState, break_outcome
from agentrealm_agent.states.level import LevelState
from agentrealm_agent.states.solve import SolveState
from agentrealm_agent.world import WorldModel, chebyshev

dispatch_module = importlib.import_module("agentrealm_agent.states.dispatch")


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
        self.assertIsNotNone(choice, "the stale map-2 target is ignored and the map-1 bush picked afresh")
        self.assertEqual(choice.pos, (4, 4))

    def test_unreachable_target_is_dropped_after_the_refusal_cap(self):
        rows = ["........."] * 9
        rows[4] = "....b...."
        w = self._world(rows, at=(4, 4))
        m = Memory()
        params = {"curiosity": 0.2}
        for _ in range(MAX_REJECTIONS):
            m.break_odd = (1, (4, 4))
            self.assertIsNotNone(pick_odd_break(w, None, Policy(kind="scripted"), m, params=params, stick_to=m.break_odd))
            note_odd_unreachable(m, 1, (4, 4))
            self.assertIsNone(m.break_odd)
        self.assertIsNone(pick_odd_break(w, None, Policy(kind="scripted"), m, params=params, stick_to=(1, (4, 4))))

    def test_skips_block_with_every_capability_failed(self):
        rows = ["........."] * 9
        rows[4] = "....b...."
        w = self._world(rows, at=(4, 4))
        kb = KnowledgeBase.empty("sandbox")
        for cap in ("cut", "chop"):
            record_attempt(kb, map_id=1, pos=(4, 4), capability=cap, result="applied_no_effect")
        m = Memory()
        self.assertIsNone(pick_odd_break(w, kb, Policy(kind="scripted"), m, params={"curiosity": 0.2}))


    def test_regrown_block_is_not_retried(self):
        rows = ["........."] * 9
        rows[4] = "....b...."
        w = self._world(rows, at=(4, 4))
        kb = KnowledgeBase.empty("sandbox")
        # Cut once; the bush grew back and looks odd again.
        record_attempt(kb, map_id=1, pos=(4, 4), capability="cut", result="opened", block_after="grass")
        m = Memory()
        params = {"curiosity": 0.2}
        self.assertIsNone(pick_odd_break(w, kb, Policy(kind="scripted"), m, params=params))
        self.assertIsNone(
            pick_odd_break(w, kb, Policy(kind="scripted"), m, params=params, stick_to=(1, (4, 4))),
            "a sticky target that was opened is not swung at again",
        )

    def test_untried_block_is_preferred_over_a_partly_failed_one(self):
        rows = ["........."] * 15
        rows[4] = "....b.....b...."
        w = self._world(rows, at=(4, 5))
        kb = KnowledgeBase.empty("sandbox")
        record_attempt(kb, map_id=1, pos=(4, 4), capability="cut", result="applied_no_effect")
        m = Memory()
        choice = pick_odd_break(w, kb, Policy(kind="scripted"), m, params={"curiosity": 0.2})
        self.assertIsNotNone(choice)
        self.assertEqual(choice.pos, (10, 4), "the nearer bush already failed once")

    def test_pick_does_not_write_memory(self):
        rows = ["........."] * 9
        rows[4] = "....b...."
        w = self._world(rows, at=(4, 4))
        m = Memory()
        m.break_odd = (2, (1, 1))
        self.assertIsNotNone(pick_odd_break(w, None, Policy(kind="scripted"), m, params={"curiosity": 0.2}, stick_to=m.break_odd))
        self.assertEqual(m.break_odd, (2, (1, 1)))
        self.assertEqual(m.break_odd_refusals, {})

    def test_consumable_cost_ranks_a_weapon_break_first(self):
        # Two bushes a clue names, each with one failed pair: the nearer one is
        # left only to matches, the farther one to the knife. A priced tool
        # puts the nearer bush second.
        rows = ["..............."] * 9
        rows[4] = "......b....b..."
        w = self._world(rows, at=(5, 5))
        w.held_supplies = [InventorySupply(1, "pocket_knife"), InventorySupply(2, "matches")]
        kb = KnowledgeBase.empty("sandbox")
        kb.clues.append({"text": "the bush hides something", "map_id": 1})
        record_attempt(kb, map_id=1, pos=(6, 4), capability="cut", result="applied_no_effect")
        record_attempt(kb, map_id=1, pos=(11, 4), capability="burn", result="applied_no_effect")
        kb.items["matches"] = {"gem_price": 20}
        choice = pick_odd_break(w, kb, Policy(kind="scripted"), Memory(), params={"curiosity": 0.2})
        self.assertEqual((choice.pos, choice.supply.code), ((11, 4), "pocket_knife"))
        kb.items["matches"] = {"gem_price": 0}
        choice = pick_odd_break(w, kb, Policy(kind="scripted"), Memory(), params={"curiosity": 0.2})
        self.assertEqual((choice.pos, choice.supply.code), ((6, 4), "matches"), "a free tool loses only to distance")

    def test_neighbourhood_reads_remembered_terrain_outside_the_live_view(self):
        # Live view holds only the bush; the knowledge base remembers the grass
        # round it. The 7x7 window still sees the motif.
        w = WorldModel(character_id=1, map_id=1, pos=(4, 4), perception=8)
        w.view.tiles = {(4, 4): "bush"}
        w.terrain_center, w.terrain_map = (4, 4), 1
        w.held_supplies = [InventorySupply(1, "bronze_sword")]
        kb = KnowledgeBase.empty("sandbox")
        kb.maps["1"] = {"terrain": {f"{x},{y}": "grass" for x in range(9) for y in range(9) if (x, y) != (4, 4)}}
        choice = pick_odd_break(w, kb, Policy(kind="scripted"), Memory(), params={"curiosity": 0.2})
        self.assertIsNotNone(choice)
        self.assertEqual(choice.pos, (4, 4))


def _odd_world() -> WorldModel:
    rows = ["........."] * 9
    rows[4] = "....b...."
    w = WorldModel(character_id=1, map_id=1, pos=(4, 4), perception=8)
    w.view.tiles = _tiles(rows)
    w.terrain_center, w.terrain_map = (4, 4), 1
    w.held_supplies = [InventorySupply(1, "bronze_sword")]
    w.armed_code = "bronze_sword"
    return w


class BreakOddDispatchTest(unittest.TestCase):
    def test_break_runs_for_odd_block_without_plan(self):
        w = _odd_world()
        ctx = PlayContext(Memory(), Policy(kind="scripted", goals=["explore"]), random.Random(0))
        self.assertFalse(BreakState().guard(w, ctx), "the priority-4 Break never takes odd blocks")
        self.assertTrue(OddBreakState().guard(w, ctx))
        self.assertIsNone(ctx.memory.break_odd, "the guard writes nothing")
        out = dispatch(w, ctx)
        self.assertEqual(out.state, "OddBreak")
        self.assertEqual(out.intents, [use_block((4, 4))])
        self.assertEqual(ctx.memory.break_odd, (1, (4, 4)))
        self.assertEqual(ctx.memory.break_pending, (1, (4, 4), "cut"))

    def test_walk_with_a_swapped_supply_reaches_the_break(self):
        # Mallet armed, knife held, lone bush six tiles off. OddBreak arms the
        # knife and walks; priority-4 Break must not swap the mallet back.
        rows = ["............."] * 9
        rows[4] = "..........b.."
        w = WorldModel(character_id=1, map_id=1, pos=(4, 4), perception=8)
        w.view.tiles = _tiles(rows)
        w.terrain_center, w.terrain_map = (4, 4), 1
        w.held_supplies = [InventorySupply(1, "bronze_mallet"), InventorySupply(2, "pocket_knife")]
        w.armed_code = "bronze_mallet"
        ctx = PlayContext(Memory(), Policy(kind="scripted", goals=["explore"]), random.Random(0))
        out = dispatch(w, ctx)
        self.assertEqual(out.state, "OddBreak")
        self.assertEqual(out.intents[0], arm(2))
        self.assertEqual(ctx.memory.break_rearm, "bronze_mallet")
        w.armed_code = "pocket_knife"
        for _ in range(10):
            if chebyshev(w.pos, (10, 4)) <= 1:
                break
            out = dispatch(w, ctx)
            self.assertEqual(out.state, "OddBreak", out.reason)
            self.assertNotIn("Arm", [i.get("verb") for i in out.intents])
            w.pos = (w.pos[0] + 1, 4)
            w.terrain_center = w.pos
        out = dispatch(w, ctx)
        self.assertEqual(out.state, "OddBreak")
        self.assertEqual(out.intents, [use_block((10, 4))])

    def test_odd_break_sits_below_solve_boss_and_level(self):
        names = [type(s).__name__ for s in dispatch_module.STATES]
        odd = names.index("OddBreakState")
        for higher in ("SolveState", "GatherState", "TravelState", "BossState", "LevelState"):
            self.assertLess(names.index(higher), odd, higher)
        self.assertLess(odd, names.index("ExploreState"))

    def test_solve_boss_and_level_outrank_an_odd_block(self):
        for cls in (SolveState, BossState, LevelState):
            with self.subTest(state=cls.name):
                w = _odd_world()
                ctx = PlayContext(Memory(), Policy(kind="scripted", goals=["explore"]), random.Random(0))
                step = {"verb": "Step", "direction": "up"}
                with mock.patch.object(cls, "guard", return_value=True), mock.patch.object(
                    cls, "act", return_value=StateOutcome([step], "goal", state=cls.name)
                ):
                    out = dispatch(w, ctx)
                self.assertEqual(out.state, cls.name)
                self.assertEqual(out.intents, [step])

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
        out = break_outcome(w, ctx.memory, None, never_attack=[], ctx=ctx, odd=True)
        self.assertIsNone(out.intents)
        self.assertIsNone(ctx.memory.break_odd)
        self.assertEqual(ctx.memory.break_odd_refusals, {(1, (4, 1)): 1})


class RunnerClearsOddTargetTest(unittest.TestCase):
    def test_opened_odd_block_clears_the_sticky_target(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.object(config, "STATE_DIR", Path(tmp.name))
        patch.start()
        self.addCleanup(patch.stop)
        cfg = CharacterConfig("T", "default", "test", "sandbox", Policy(goals=["hold"]), Path("t.toml"))
        kb = KnowledgeBase.empty("sandbox")
        r = Runner(cfg, None, 1, threading.Event(), out=lambda _: None, knowledge=kb)
        self.addCleanup(r.trace.close)
        w = _odd_world()
        r.world, r.mem = w, Memory(need_self=False, need_position=False)
        r.mem.break_odd = (1, (4, 4))
        r.mem.break_pending = (1, (4, 4), "cut")
        w.view.tiles[(4, 4)] = "grass"
        w.changed_blocks = [(1, (4, 4))]
        r._resolve_pending_break()
        self.assertIsNone(r.mem.break_odd)
        self.assertIsNone(r.mem.break_pending)
        self.assertTrue(attempt_open(kb, 1, (4, 4), "cut"))
