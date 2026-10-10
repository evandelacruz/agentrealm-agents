"""Investigate: the executor for plan ``read`` and ``say`` ops, and the
investigation memory it reads from the knowledge base (A30)."""

import json
import random
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agentrealm_agent import config
from agentrealm_agent.brain import choose_call, decide
from agentrealm_agent.config import CharacterConfig, Policy
from agentrealm_agent.directives import PARAM_DEFAULTS, Directives
from agentrealm_agent.investigation import (
    MAX_REJECTIONS,
    SPEECH_RANGE,
    cell_was_read,
    in_sight,
    mark_cell_read,
    mark_npc_spoken,
    read_key,
    read_supply_key,
    say_key,
    sight_range,
    spoken_npc_ids,
)
from agentrealm_agent.item_table import InventorySupply
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.memory import Memory
from agentrealm_agent.plan import Plan
from agentrealm_agent.runner import Runner
from agentrealm_agent.strategist import SIGNS_SHOWN, build_prompt
from agentrealm_agent.scroll_investigation import (
    probed_supply_codes,
    log_supply_codes_seen,
    mark_code_probed,
    mark_scroll_subtype,
    mark_supply_read,
    probed_supply_codes,
    scroll_subtype_codes,
    seen_supply_codes,
    supply_was_read,
)
from agentrealm_agent.states import dispatch
from agentrealm_agent.states.base import PlayContext
from agentrealm_agent.states.intents import read_block, read_supply
from agentrealm_agent.states.investigate import InvestigateState
from agentrealm_agent.travel.knowledge import entrance_from_kb, sync_entrances
from agentrealm_agent.world import Entity, WorldModel, ZoneFact


def world(rows: list[str], at=(1, 1), perception=3) -> WorldModel:
    glyph = {".": "dirt", "#": "wall", "S": "wall", "D": "framed_door"}
    w = WorldModel(character_id=1, map_id=7, pos=at, perception=perception)
    for y, row in enumerate(rows):
        for x, g in enumerate(row):
            w.view.tiles[(x, y)] = glyph[g]
            if g == "S":
                w.view.readable[(x, y)] = True
    w.terrain_center, w.terrain_map = at, 7
    return w


def plan(*ops: dict) -> Plan:
    return Plan(list(ops), dict(PARAM_DEFAULTS))


def ctx(p: Plan | None, kb: KnowledgeBase | None = None, m: Memory | None = None) -> PlayContext:
    return PlayContext(
        m or Memory(),
        Policy(kind="scripted", goals=["explore"], hostile=[]),
        random.Random(0),
        knowledge=kb if kb is not None else KnowledgeBase.empty("sandbox"),
        plan=p,
    )


class InvestigationMemoryTest(unittest.TestCase):
    def test_keys(self):
        self.assertEqual(read_key(7, (1, 2)), "read:7:1,2")
        self.assertEqual(say_key(9), "say:9")
        self.assertEqual(read_supply_key(12), "read_supply:12")

    def test_read_cells_are_per_map(self):
        kb = KnowledgeBase.empty("sandbox")
        self.assertFalse(cell_was_read(kb, 7, (1, 1)))
        mark_cell_read(kb, 7, (1, 1))
        mark_cell_read(kb, 7, (1, 1))
        self.assertTrue(cell_was_read(kb, 7, (1, 1)))
        self.assertFalse(cell_was_read(kb, 8, (1, 1)), "reads are per map")
        self.assertEqual(kb.extra["read_cells"], {"7": ["1,1"]})

    def test_spoken_npcs(self):
        kb = KnowledgeBase.empty("sandbox")
        mark_npc_spoken(kb, 9)
        mark_npc_spoken(kb, 9)
        self.assertEqual(spoken_npc_ids(kb), {9})
        kb.extra["spoken_npcs"] = [9, "10", "x", None]
        self.assertEqual(spoken_npc_ids(kb), {9, 10}, "bad rows are skipped")

    def test_no_knowledge_base_records_nothing(self):
        mark_cell_read(None, 7, (1, 1))
        mark_npc_spoken(None, 9)
        self.assertFalse(cell_was_read(None, 7, (1, 1)))
        self.assertEqual(spoken_npc_ids(None), set())

    def test_supply_codes_seen_probed_and_scrolls(self):
        kb = KnowledgeBase.empty("sandbox")
        log_supply_codes_seen(kb, ["apple", "clue_scroll", "apple"])
        self.assertEqual(seen_supply_codes(kb), {"apple", "clue_scroll"})
        mark_code_probed(kb, "apple")
        self.assertIn("apple", probed_supply_codes(kb))
        self.assertEqual(probed_supply_codes(kb), {"apple"})
        self.assertNotIn("apple", scroll_subtype_codes(kb))
        mark_scroll_subtype(kb, "clue_scroll")
        self.assertEqual(scroll_subtype_codes(kb), {"clue_scroll"})

    def test_supply_reads(self):
        kb = KnowledgeBase.empty("sandbox")
        self.assertFalse(supply_was_read(kb, 12))
        mark_supply_read(kb, 12)
        self.assertTrue(supply_was_read(kb, 12))


class InvestigateStateTest(unittest.TestCase):
    def test_guard_needs_a_read_or_say_op(self):
        w = world(["...", ".S.", "..."], at=(0, 1))
        state = InvestigateState()
        self.assertFalse(state.guard(w, ctx(None)), "no plan: no curiosity of its own")
        self.assertFalse(state.guard(w, ctx(plan({"op": "explore_area", "x": 1, "y": 1, "radius": 1}))))
        self.assertTrue(state.guard(w, ctx(plan({"op": "read", "x": 1, "y": 1}))))
        self.assertTrue(state.guard(w, ctx(plan({"op": "say", "npc_type": "helper", "text": "hi"}))))

    def test_unread_sign_without_a_read_op_is_left_alone(self):
        w = world(["...", ".S.", "..."], at=(0, 1))
        out = dispatch(w, ctx(None))
        self.assertNotEqual(out.state, "Investigate")
        self.assertNotIn("Read", [i["verb"] for i in out.intents or []])

    def test_reads_a_sign_in_sight(self):
        w = world(["...", ".S.", "..."], at=(0, 1))
        p = plan({"op": "read", "x": 1, "y": 1})
        out = dispatch(w, ctx(p))
        self.assertEqual(out.state, "Investigate")
        # Exactly {kind, x, y}: a map_id is a field Read does not take (API rules § Read).
        self.assertEqual(out.intents, [{"verb": "Read", "target": {"kind": "block", "x": 1, "y": 1}}])
        self.assertEqual(p.current(), {"op": "read", "x": 1, "y": 1}, "finished only once recorded")

    def test_walks_toward_a_readable_out_of_sight(self):
        w = world(["......."], at=(6, 0), perception=3)
        w.view.readable[(0, 0)] = True  # a readable on walkable ground
        self.assertFalse(in_sight(w, 7, (6, 0), (0, 0)))
        out = dispatch(w, ctx(plan({"op": "read", "x": 0, "y": 0})))
        self.assertEqual(out.state, "Investigate")
        self.assertEqual(out.intents, [{"verb": "SetPosition", "x": 5, "y": 0}])

    def test_walks_toward_a_wall_sign_out_of_sight(self):
        # Suspected source bug: Investigate paths to the sign cell itself, and a
        # sign on a wall block has no path, so the op only stalls (states/investigate.py _walk).
        w = world(["S......"], at=(6, 0), perception=3)
        out = dispatch(w, ctx(plan({"op": "read", "x": 0, "y": 0})))
        self.assertEqual(out.state, "Investigate")
        self.assertEqual(out.intents, [{"verb": "SetPosition", "x": 5, "y": 0}])

    def test_recorded_read_finishes_the_op(self):
        w = world(["...", ".S.", "..."], at=(0, 1))
        kb = KnowledgeBase.empty("sandbox")
        mark_cell_read(kb, 7, (1, 1))
        explore = {"op": "explore_area", "x": 2, "y": 2, "radius": 1}
        p = plan({"op": "read", "x": 1, "y": 1}, explore)
        out = dispatch(w, ctx(p, kb))
        self.assertNotEqual(out.state, "Investigate")
        self.assertEqual(p.current(), explore)

    def test_refused_read_finishes_the_op_after_max_rejections(self):
        w = world(["...", ".S.", "..."], at=(0, 1))
        m = Memory()
        p = plan({"op": "read", "x": 1, "y": 1})
        m.investigate_rejections[read_key(7, (1, 1))] = MAX_REJECTIONS - 1
        self.assertEqual(dispatch(w, ctx(p, m=m)).state, "Investigate")
        m.investigate_rejections[read_key(7, (1, 1))] = MAX_REJECTIONS
        out = dispatch(w, ctx(p, m=m))
        self.assertNotEqual(out.state, "Investigate")
        self.assertIsNone(p.current())

    def test_reads_a_supply(self):
        w = world(["..."], at=(1, 0))
        w.held_supplies = [InventorySupply(12, "clue_scroll")]
        kb = KnowledgeBase.empty("sandbox")
        p = plan({"op": "read", "supply_id": 12})
        out = dispatch(w, ctx(p, kb))
        self.assertEqual(out.state, "Investigate")
        self.assertEqual(out.intents, [read_supply(12)])
        mark_supply_read(kb, 12)
        dispatch(w, ctx(p, kb))
        self.assertIsNone(p.current())

    def test_says_the_ops_text_to_an_npc_by_id(self):
        w = world(["...", "...", "..."], at=(0, 1))
        w.entities = [Entity("npc", 4, (2, 2), "helper")]
        p = plan({"op": "say", "npc_id": 4, "text": "any news?"})
        d = decide(w, Memory(), Policy(kind="scripted", hostile=[]), random.Random(0),
                   knowledge=KnowledgeBase.empty("sandbox"), plan=p)
        self.assertEqual(d.intent, {"verb": "Say", "npc_id": 4, "text": "any news?"})

    def test_say_by_type_picks_the_nearest(self):
        w = world(["....", "....", "...."], at=(0, 0))
        w.entities = [Entity("npc", 8, (3, 2), "helper"), Entity("npc", 5, (1, 1), "helper"),
                      Entity("npc", 2, (1, 0), "guard")]
        out = dispatch(w, ctx(plan({"op": "say", "npc_type": "helper", "text": "hello"})))
        self.assertEqual(out.intents, [{"verb": "Say", "npc_id": 5, "text": "hello"}])

    def test_walks_toward_an_npc_beyond_speech_range(self):
        far = SPEECH_RANGE + 2
        w = world(["." * (far + 1)], at=(0, 0), perception=far + 1)
        w.entities = [Entity("npc", 4, (far, 0), "helper")]
        out = dispatch(w, ctx(plan({"op": "say", "npc_id": 4, "text": "hello"})))
        self.assertEqual(out.state, "Investigate")
        self.assertEqual(out.intents, [{"verb": "SetPosition", "x": 1, "y": 0}])

    def test_no_such_npc_in_sight_yields(self):
        w = world(["...", "...", "..."], at=(0, 1))
        p = plan({"op": "say", "npc_type": "helper", "text": "hello"})
        out = dispatch(w, ctx(p))
        self.assertNotEqual(out.state, "Investigate")
        self.assertIn("Investigate: no such NPC in sight", out.yielded)
        self.assertEqual(p.current()["op"], "say", "kept until its stall clock runs out")

    def test_spoken_npc_finishes_the_op(self):
        w = world(["...", "...", "..."], at=(0, 1))
        w.entities = [Entity("npc", 4, (2, 2), "helper")]
        kb = KnowledgeBase.empty("sandbox")
        mark_npc_spoken(kb, 4)
        p = plan({"op": "say", "npc_id": 4, "text": "hello"})
        dispatch(w, ctx(p, kb))
        self.assertIsNone(p.current())

    def test_act_has_no_side_effects_for_read(self):
        w = world(["...", ".S.", "..."], at=(0, 1))
        kb = KnowledgeBase.empty("sandbox")
        c = ctx(plan({"op": "read", "x": 1, "y": 1}), kb)
        before = kb.to_dict()
        dispatch(w, c)
        dispatch(w, c)
        self.assertEqual(before, kb.to_dict(), "only an applied result marks the knowledge base")


class StateListsSignsTest(unittest.TestCase):
    """The planner's State lists the signs and statues seen, unread first, then nearest."""

    def line(self, w, kb):
        msgs = build_prompt(
            triggers=[], w=w, plan=Plan([], dict(PARAM_DEFAULTS)), directives=Directives(params=dict(PARAM_DEFAULTS)), knowledge=kb
        )
        return next(l for l in msgs[1]["content"].splitlines() if l.startswith("signs_seen="))

    def test_unread_first_then_nearest(self):
        w = WorldModel(character_id=1, map_id=7, pos=(5, 5))
        for p, block in [((6, 5), "sign"), ((10, 5), "statue"), ((5, 1), "sign")]:
            w.view.tiles[p] = block
            w.view.readable[p] = True
        kb = KnowledgeBase.empty("sandbox")
        mark_cell_read(kb, 7, (6, 5))
        line = self.line(w, kb)
        rows = json.loads(line.split("=", 1)[1].rsplit(" unread=", 1)[0])
        self.assertEqual([(r["x"], r["y"], r["read"]) for r in rows], [(5, 1, False), (10, 5, False), (6, 5, True)])
        self.assertEqual(rows[1], {"map_id": 7, "x": 10, "y": 5, "block": "statue", "cells": 5, "dir": "east", "read": False})
        self.assertTrue(line.endswith(" unread=2"))

    def test_capped(self):
        w = WorldModel(character_id=1, map_id=7, pos=(0, 0))
        for x in range(SIGNS_SHOWN + 3):
            w.view.readable[(x + 1, 0)] = True
        line = self.line(w, None)
        rows = json.loads(line.split("=", 1)[1].rsplit(" unread=", 1)[0])
        self.assertEqual(len(rows), SIGNS_SHOWN)
        self.assertTrue(line.endswith(f" unread={SIGNS_SHOWN + 3}"))

    def test_none_seen(self):
        self.assertEqual(self.line(WorldModel(character_id=1, map_id=7, pos=(0, 0)), None), "signs_seen=none on this map")


class EntranceKeyTest(unittest.TestCase):
    def test_cell_only_rows_migrate_at_load(self):
        kb = KnowledgeBase.from_dict("sandbox", {"entrances": {
            "2,1": {"map_id": 7, "x": 2, "y": 1, "needs": "key"},
            "4,4": {"x": 4, "y": 4},  # no map: kept as is, readers skip it
        }})
        self.assertEqual(kb.entrances, {
            "7:2,1": {"map_id": 7, "x": 2, "y": 1, "needs": "key"},
            "4,4": {"x": 4, "y": 4},
        })

    def test_entrance_from_kb_without_map(self):
        kb = KnowledgeBase.empty("sandbox")
        sync_entrances(kb, {"maps": [{"map_id": 7, "entrances": [{"x": 2, "y": 1}, {"x": 5, "y": 5}]},
                                     {"map_id": 9, "entrances": [{"x": 2, "y": 1}]}]})
        self.assertEqual(entrance_from_kb(kb, None, 5, 5), (7, (5, 5)))
        self.assertIsNone(entrance_from_kb(kb, None, 2, 1))
        self.assertEqual(entrance_from_kb(kb, 9, 2, 1), (9, (2, 1)))


class SightRangeTest(unittest.TestCase):
    def test_brightness_caps_sight(self):
        w = WorldModel(1, map_id=1, pos=(0, 0), perception=5)
        w.zones[1] = {(0, 0): ZoneFact(safe=False, brightness=0.5)}
        self.assertEqual(sight_range(w, 1, (0, 0)), 3, "ceil(5 * 0.5)")
        w.zones[1] = {(0, 0): ZoneFact(safe=False, brightness=0.6)}
        self.assertEqual(sight_range(w, 1, (0, 0)), 3, "an exact product is not rounded up")


class ReadableParsingTest(unittest.TestCase):
    def test_terrain_read_patch_and_remove(self):
        w = WorldModel(character_id=1, pos=(1, 1), map_id=7)
        w.apply_terrain({"map_id": 7, "x0": 0, "y0": 0, "width": 2, "height": 1,
                         "legend": {"s": {"block_type": "wall", "readable": True}, "d": {"block_type": "dirt"}},
                         "rows": ["sd"]})
        self.assertEqual(w.view.readable, {(0, 0): True})
        w.apply_terrain({"map_id": 7, "x0": 0, "y0": 0, "width": 1, "height": 1,
                         "legend": {"w": {"block_type": "wall"}}, "rows": ["w"]})
        self.assertEqual(w.view.readable, {}, "a full read without the flag clears it")
        w.apply_observation({"version": "2", "delta": {"terrain": {
            "changed": [{"map_id": 7, "x": 1, "y": 0, "block_type": "wall", "readable": True}]}}})
        self.assertEqual(w.view.readable, {(1, 0): True})
        w.apply_observation({"version": "3", "delta": {"terrain": {"removed": [{"map_id": 7, "x": 1, "y": 0}]}}})
        self.assertEqual(w.view.readable, {})


class LockedParsingTest(unittest.TestCase):
    def test_terrain_delta_and_snapshot(self):
        w = WorldModel(character_id=1, pos=(1, 1), map_id=7)
        w.apply_observation({"version": "1", "complete": True, "snapshot": {"terrain": {"cells": [
            {"map_id": 7, "x": 2, "y": 1, "block_type": "framed_door", "locked": True},
            {"map_id": 7, "x": 0, "y": 1, "block_type": "framed_door"},
        ]}}})
        self.assertEqual(w.view.locked, {(2, 1): True})
        w.apply_observation({"version": "2", "delta": {"terrain": {
            "changed": [{"map_id": 7, "x": 0, "y": 1, "block_type": "framed_door", "locked": True}]}}})
        self.assertEqual(w.view.locked, {(2, 1): True, (0, 1): True})
        w.apply_observation({"version": "3", "delta": {"terrain": {
            "changed": [{"map_id": 7, "x": 2, "y": 1, "block_type": "framed_door", "locked": False}]}}})
        self.assertEqual(w.view.locked, {(0, 1): True}, "an explicit false unlocks")
        w.apply_observation({"version": "4", "delta": {"terrain": {"removed": [{"map_id": 7, "x": 0, "y": 1}]}}})
        self.assertEqual(w.view.locked, {})


class ZoneProbeOrderTest(unittest.TestCase):
    def test_respawn_ring_probe_comes_before_path(self):
        w = WorldModel(character_id=1, map_id=7, pos=(5, 5), perception=5)
        for y in range(15):
            for x in range(15):
                w.view.tiles[(x, y)] = "grass"
        w.terrain_center, w.terrain_map = (5, 5), 7
        w.record_respawn_anchor(7, (10, 10))
        w.tick = w.entities_tick = 12
        m = Memory(need_self=False, need_position=False, last_poll_tick=10, calm_poll_interval=7,
                   path=[(6, 5), (7, 5)])
        self.assertEqual(choose_call(w, m, Policy()), "zone")
        self.assertEqual(m.zone_probe, (7, (10, 10)), "A7 safety probe first")


class RunnerInvestigationTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.object(config, "STATE_DIR", Path(tmp.name))
        patch.start()
        self.addCleanup(patch.stop)

    def runner(self, ticks: list[dict]) -> Runner:
        client = mock.Mock()
        client.tick.side_effect = [dict(t, queue_id=f"q{i + 1}") for i, t in enumerate(ticks)]
        cfg = CharacterConfig("T", "sandbox", Policy(kind="scripted", goals=["explore"]), Path("t.toml"))
        r = Runner(cfg, client, 1, threading.Event(), out=lambda _: None, knowledge=KnowledgeBase.empty("sandbox"))
        self.addCleanup(r.trace.close)
        r.world = world([".....", ".S...", "....."], at=(0, 1))
        r.mem = Memory(need_self=False, need_position=False)
        return r

    def test_applied_read_is_remembered(self):
        r = self.runner([])
        intent = read_block((1, 1))
        r.mem.pending = intent
        self.assertFalse(r.on_result({"outcome": "applied", "tick": 5}, 0))
        self.assertTrue(cell_was_read(r.knowledge, 7, (1, 1)))

    def test_rejected_read_is_capped(self):
        r = self.runner([])
        intent = read_block((1, 1))
        p = plan({"op": "read", "x": 1, "y": 1})
        for i in range(MAX_REJECTIONS):
            self.assertEqual(dispatch(r.world, ctx(p, r.knowledge, r.mem)).intents, [intent])
            r.mem.pending = intent
            self.assertTrue(r.on_result({"outcome": "rejected", "tick": 5,
                                         "rejection": {"category": "target", "code": "nothing_to_read"}}, 0))
            self.assertEqual(r.mem.investigate_rejections[read_key(7, (1, 1))], i + 1)
        self.assertFalse(cell_was_read(r.knowledge, 7, (1, 1)))
        out = dispatch(r.world, ctx(p, r.knowledge, r.mem))
        self.assertNotEqual(out.state, "Investigate", "a refused read ends its op")
        self.assertIsNone(p.current())

    def test_applied_say_is_remembered(self):
        r = self.runner([])
        r.mem.pending = {"verb": "Say", "npc_id": 4, "text": "hello"}
        self.assertFalse(r.on_result({"outcome": "applied", "tick": 5}, 0))
        self.assertEqual(spoken_npc_ids(r.knowledge), {4})

    def test_supply_probe_nothing_to_read_marks_code_probed(self):
        r = self.runner([])
        r.world.held_supplies = [InventorySupply(12, "apple")]
        intent = read_supply(12)
        r.mem.pending = intent
        r.on_result({"outcome": "rejected", "tick": 5,
                     "rejection": {"category": "target", "code": "nothing_to_read"}}, 0)
        self.assertIn("apple", probed_supply_codes(r.knowledge))
        self.assertFalse(supply_was_read(r.knowledge, 12))

    def test_applied_supply_read_remembers_scroll_code(self):
        r = self.runner([])
        r.world.held_supplies = [InventorySupply(12, "clue_scroll")]
        intent = read_supply(12)
        r.mem.pending = intent
        r.on_result({"outcome": "applied", "tick": 5}, 0)
        self.assertTrue(supply_was_read(r.knowledge, 12))
        self.assertIn("clue_scroll", scroll_subtype_codes(r.knowledge))

    def test_applied_scroll_read_stores_its_text_as_a_clue(self):
        r = self.runner([])
        r.world.held_supplies = [InventorySupply(12, "clue_scroll"), InventorySupply(13, "clue_scroll")]
        for sid, text in ((12, "Go north"), (13, "Go north")):
            r.mem.pending = read_supply(sid)
            r.on_result({"outcome": "applied", "tick": 5, "text": text}, 0)
        rows = [c for c in r.knowledge.clues if c["kind"] == "scroll"]
        self.assertEqual([(c["supply_id"], c["text"], c["x"], c["y"]) for c in rows],
                         [(12, "Go north", 0, 1), (13, "Go north", 0, 1)],
                         "two carried scrolls read from one cell are two clues")

    def test_known_scroll_answering_nothing_to_read_is_not_read_again(self):
        r = self.runner([])
        r.world.held_supplies = [InventorySupply(12, "clue_scroll")]
        mark_scroll_subtype(r.knowledge, "clue_scroll")
        p = plan({"op": "read", "supply_id": 12})
        self.assertEqual(dispatch(r.world, ctx(p, r.knowledge, r.mem)).intents, [read_supply(12)])
        r.mem.pending = read_supply(12)
        r.on_result({"outcome": "rejected", "tick": 5,
                     "rejection": {"category": "target", "code": "nothing_to_read"}}, 0)
        self.assertTrue(supply_was_read(r.knowledge, 12))
        self.assertIn("clue_scroll", scroll_subtype_codes(r.knowledge), "one blank scroll keeps its code")
        dispatch(r.world, ctx(p, r.knowledge, r.mem))
        self.assertIsNone(p.current(), "a blank scroll counts as read: its op ends")


    def test_runner_plan_read_op_sends_the_read(self):
        r = self.runner([{"tick": 100}])
        r.plan = plan({"op": "read", "x": 1, "y": 1})
        r.tick()
        self.assertEqual(r.mem.state, "Investigate")
        sent = r.client.tick.call_args.args[1]
        self.assertEqual(sent[0], read_block((1, 1)))


if __name__ == "__main__":
    unittest.main()
