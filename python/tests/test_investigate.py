"""Interest list and Investigate state (A30)."""

import random
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agentrealm_agent import config, runner as runner_mod
from agentrealm_agent.brain import choose_call, decide
from agentrealm_agent.config import CharacterConfig, Policy
from agentrealm_agent.door_look import apply_door_look, infer_needs, look_key, ready_to_look
from agentrealm_agent.interest_list import MAX_REJECTIONS, list_interest, pick_interest_tick, sight_range
from agentrealm_agent.investigation import cell_was_read, mark_cell_read, mark_npc_spoken, spoken_npc_ids
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.knowledge_maps import iter_doors, record_warp, sync_map_from_view, sync_tiles
from agentrealm_agent.memory import Memory
from agentrealm_agent.runner import Runner
from agentrealm_agent.states import dispatch
from agentrealm_agent.states.base import PlayContext
from agentrealm_agent.states.intents import read_block
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


class InterestListTest(unittest.TestCase):
    def test_readable_in_sight_is_nominated(self):
        w = world(["...", ".S.", "..."], at=(1, 1))
        items = list_interest(w, KnowledgeBase.empty("sandbox"), Policy(kind="scripted"), Memory())
        self.assertEqual([(it.kind, it.pos) for it in items], [("read_block", (1, 1))])

    def test_readable_out_of_sight_waits(self):
        w = world(["S....."], at=(5, 0), perception=3)
        self.assertEqual(list_interest(w, KnowledgeBase.empty("sandbox"), Policy(kind="scripted"), Memory()), [])

    def test_read_cells_are_not_repeated(self):
        w = world(["...", ".S.", "..."], at=(1, 1))
        kb = KnowledgeBase.empty("sandbox")
        mark_cell_read(kb, 7, (1, 1))
        self.assertTrue(cell_was_read(kb, 7, (1, 1)))
        self.assertFalse(cell_was_read(kb, 8, (1, 1)), "reads are per map")
        self.assertEqual(list_interest(w, kb, Policy(kind="scripted"), Memory()), [])

    def test_npc_say_within_25_blocks(self):
        w = world(["...", "...", "..."], at=(1, 1))
        w.entities = [Entity("npc", 9, (1, 4), "guard"), Entity("npc", 10, (1, 30), "guard")]
        kb = KnowledgeBase.empty("sandbox")
        pol = Policy(kind="scripted", hostile=[])
        items = list_interest(w, kb, pol, Memory())
        self.assertEqual([(it.kind, it.npc.id) for it in items], [("say", 9)])
        mark_npc_spoken(kb, 9)
        self.assertIn(9, spoken_npc_ids(kb))
        self.assertEqual(list_interest(w, kb, pol, Memory()), [])

    def test_hostiles_pause_curiosity(self):
        w = world(["...", ".S.", "..."], at=(1, 1))
        w.entities = [Entity("npc", 3, (2, 1), "wolf")]
        pol = Policy(kind="scripted", hostile=["npc"], hostile_range=2)
        self.assertEqual(list_interest(w, KnowledgeBase.empty("sandbox"), pol, Memory()), [])

    def test_no_zone_items(self):
        # Unknown zones are A7's spare-window probes, not a second list here.
        w = world(["...", "...", "..."], at=(1, 1))
        m = Memory(path=[(2, 1), (2, 2)])
        self.assertEqual(list_interest(w, KnowledgeBase.empty("sandbox"), Policy(kind="scripted"), m), [])

    def test_unlooked_entrance_is_nominated(self):
        w = world(["...", "...", "..."], at=(0, 0))
        kb = KnowledgeBase.empty("sandbox")
        kb.entrances["7:2,1"] = {"map_id": 7, "x": 2, "y": 1}
        items = list_interest(w, kb, Policy(kind="scripted"), Memory())
        self.assertEqual([it.kind for it in items], ["look_door"])
        self.assertEqual(items[0].pos, (2, 1))

    def test_looked_entrance_is_skipped(self):
        w = world(["...", "...", "..."], at=(0, 0))
        kb = KnowledgeBase.empty("sandbox")
        kb.entrances["7:2,1"] = {"map_id": 7, "x": 2, "y": 1, "looked": True}
        self.assertEqual(list_interest(w, kb, Policy(kind="scripted"), Memory()), [])

    def test_other_map_entrance_is_nominated(self):
        w = world(["...", "...", "..."], at=(0, 0))
        kb = KnowledgeBase.empty("sandbox")
        kb.entrances["9:1,1"] = {"map_id": 9, "x": 1, "y": 1}
        items = list_interest(w, kb, Policy(kind="scripted"), Memory())
        self.assertEqual([(it.kind, it.map_id, it.pos) for it in items], [("look_door", 9, (1, 1))])

    def test_unlooked_door_is_nominated_and_visited_door_is_not(self):
        w = world(["....", "....", "...."], at=(0, 0))
        kb = KnowledgeBase.empty("sandbox")
        sync_tiles(kb, 7, {(3, 0): "framed_door", (3, 2): "framed_door"})
        for d in iter_doors(kb, 7):
            if (d["x"], d["y"]) == (3, 2):
                d.update({"to_map_id": 8, "to_x": 1, "to_y": 1})
        items = list_interest(w, kb, Policy(kind="scripted"), Memory())
        self.assertEqual([(it.kind, it.pos) for it in items], [("look_door", (3, 0))])

    def test_entrance_mark_with_door_tile_is_nominated(self):
        w = world(["...", ".D.", "..."], at=(0, 1))
        kb = KnowledgeBase.empty("sandbox")
        kb.entrances["7:1,1"] = {"map_id": 7, "x": 1, "y": 1}
        items = list_interest(w, kb, Policy(kind="scripted"), Memory())
        self.assertEqual([it.kind for it in items], ["look_door"])

    def test_look_given_up_after_max_rejections(self):
        w = world(["...", "...", "..."], at=(0, 0))
        kb = KnowledgeBase.empty("sandbox")
        kb.entrances["7:2,1"] = {"map_id": 7, "x": 2, "y": 1}
        m = Memory(investigate_rejections={look_key(7, (2, 1)): MAX_REJECTIONS})
        self.assertEqual(list_interest(w, kb, Policy(kind="scripted"), m), [])


class EntranceKeyTest(unittest.TestCase):
    def test_same_cell_on_two_maps_stays_apart(self):
        kb = KnowledgeBase.empty("sandbox")
        sync_entrances(kb, {"maps": [
            {"map_id": 7, "entrances": [{"x": 2, "y": 1}]},
            {"map_id": 9, "entrances": [{"x": 2, "y": 1}]},
        ]})
        self.assertEqual(set(kb.entrances), {"7:2,1", "9:2,1"})
        w = WorldModel(1, map_id=9, pos=(1, 1), perception=5)
        w.view.tiles[(2, 1)] = "framed_door"
        w.view.locked[(2, 1)] = True
        self.assertTrue(apply_door_look(kb, w, 9, (2, 1)))
        self.assertEqual(kb.entrances["9:2,1"]["needs"], "key")
        self.assertNotIn("looked", kb.entrances["7:2,1"])

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


class DoorLookTest(unittest.TestCase):
    def test_locked_door_records_key_need(self):
        w = WorldModel(1, map_id=7, pos=(1, 1), perception=5)
        w.view.tiles[(2, 1)] = "framed_door"
        w.view.locked[(2, 1)] = True
        kb = KnowledgeBase.empty("sandbox")
        kb.entrances["7:2,1"] = {"map_id": 7, "x": 2, "y": 1}
        self.assertTrue(ready_to_look(w, 7, (2, 1)))
        self.assertTrue(apply_door_look(kb, w, 7, (2, 1)))
        self.assertEqual(kb.entrances["7:2,1"]["needs"], "key")
        self.assertTrue(kb.entrances["7:2,1"]["locked"])
        door = [d for d in iter_doors(kb, 7) if (d["x"], d["y"]) == (2, 1)][0]
        self.assertTrue(door["locked"] and door["looked"])

    def test_infer_needs_is_key_only(self):
        self.assertEqual(infer_needs("framed_door", locked=True), "key")
        self.assertIsNone(infer_needs("framed_door", locked=False))
        # Unsourced: a block never says whether it breaks (break), water is the
        # route not the cell (cross_water), and a catch-all would guess (blocked).
        for block in ("bush", "tree", "rock", "wall", "water", "dirt"):
            self.assertIsNone(infer_needs(block, locked=False), block)
        self.assertIsNone(infer_needs(None, locked=True))

    def test_non_door_entrance_records_block_without_needs(self):
        w = WorldModel(1, map_id=7, pos=(1, 1), perception=5)
        w.view.tiles[(2, 1)] = "water"
        kb = KnowledgeBase.empty("sandbox")
        kb.entrances["7:2,1"] = {"map_id": 7, "x": 2, "y": 1}
        self.assertTrue(ready_to_look(w, 7, (2, 1)))
        self.assertTrue(apply_door_look(kb, w, 7, (2, 1)))
        row = kb.entrances["7:2,1"]
        self.assertEqual((row["looked"], row["block_type"]), (True, "water"))
        self.assertNotIn("needs", row)
        self.assertEqual(iter_doors(kb, 7), [], "a non-door cell is not added to the door list")


class InvestigateStateTest(unittest.TestCase):
    def test_investigate_beats_explore_for_unread_sign(self):
        w = world(["...", ".S.", "..."], at=(0, 1))
        ctx = PlayContext(Memory(), Policy(kind="scripted", goals=["explore"]), random.Random(0), knowledge=KnowledgeBase.empty("sandbox"))
        out = dispatch(w, ctx)
        self.assertEqual(out.state, "Investigate")
        # Exactly {kind, x, y}: a map_id is a field Read does not take (API rules § Read).
        self.assertEqual(out.intents, [{"verb": "Read", "target": {"kind": "block", "x": 1, "y": 1}}])

    def test_decide_says_to_npc(self):
        w = world(["...", "...", "..."], at=(0, 1))
        w.entities = [Entity("npc", 4, (2, 2), "helper")]
        d = decide(w, Memory(), Policy(kind="scripted", hostile=[]), random.Random(0), knowledge=KnowledgeBase.empty("sandbox"))
        self.assertEqual(d.intent["verb"], "Say")
        self.assertEqual(d.intent, {"verb": "Say", "npc_id": 4, "text": "hello"})

    def test_act_has_no_side_effects_for_read(self):
        w = world(["...", ".S.", "..."], at=(0, 1))
        kb = KnowledgeBase.empty("sandbox")
        ctx = PlayContext(Memory(), Policy(kind="scripted"), random.Random(0), knowledge=kb)
        before = kb.to_dict()
        dispatch(w, ctx)
        dispatch(w, ctx)
        after = kb.to_dict()
        self.assertEqual(before, after, "only an applied result marks the knowledge base")

    def test_investigate_routes_to_other_map_entrance(self):
        w = WorldModel(1, map_id=1, pos=(0, 0), perception=8)
        for y in range(4):
            for x in range(4):
                w.view.tiles[(x, y)] = "dirt"
        w.view.tiles[(2, 0)] = "framed_door"
        w.maps[1] = w.view
        kb = KnowledgeBase.empty("sandbox")
        sync_map_from_view(kb, 1, w.view)
        record_warp(kb, 1, (2, 0), "framed_door", 2, (0, 0))
        other = WorldModel(1, map_id=2, pos=(0, 0), perception=8)
        for y in range(4):
            for x in range(4):
                other.view.tiles[(x, y)] = "dirt"
        sync_map_from_view(kb, 2, other.view)
        kb.entrances["2:3,3"] = {"map_id": 2, "x": 3, "y": 3}
        ctx = PlayContext(Memory(), Policy(kind="scripted"), random.Random(0), knowledge=kb)
        out = dispatch(w, ctx)
        self.assertEqual(out.state, "Investigate")
        self.assertEqual(out.intents[0]["verb"], "SetPosition")
        self.assertEqual(ctx.memory.goal, look_key(2, (3, 3)))

    def test_investigate_walks_to_entrance_mark(self):
        rows = ["." * 8 for _ in range(8)]
        w = world(rows, at=(0, 0), perception=8)
        kb = KnowledgeBase.empty("sandbox")
        kb.entrances["7:5,5"] = {"map_id": 7, "x": 5, "y": 5}
        w.view.tiles[(5, 5)] = "framed_door"
        ctx = PlayContext(Memory(), Policy(kind="scripted"), random.Random(0), knowledge=kb)
        out = dispatch(w, ctx)
        self.assertEqual(out.state, "Investigate")
        self.assertEqual(out.intents[0]["verb"], "SetPosition")

    def test_investigate_walks_beside_a_water_entrance(self):
        rows = ["." * 8 for _ in range(8)]
        w = world(rows, at=(0, 0), perception=8)
        w.view.tiles[(5, 5)] = "water"
        kb = KnowledgeBase.empty("sandbox")
        kb.entrances["7:5,5"] = {"map_id": 7, "x": 5, "y": 5}
        ctx = PlayContext(Memory(), Policy(kind="scripted"), random.Random(0), knowledge=kb)
        out = dispatch(w, ctx)
        self.assertEqual(out.state, "Investigate")
        self.assertEqual(out.intents[0]["verb"], "SetPosition")
        self.assertEqual(ctx.memory.investigate_rejections, {})

    def test_investigate_records_entrance_when_already_adjacent(self):
        w = world(["...", ".D.", "..."], at=(0, 0))
        kb = KnowledgeBase.empty("sandbox")
        kb.entrances["7:1,1"] = {"map_id": 7, "x": 1, "y": 1}
        ctx = PlayContext(Memory(), Policy(kind="scripted"), random.Random(0), knowledge=kb)
        out = dispatch(w, ctx)
        self.assertTrue(kb.entrances["7:1,1"]["looked"])
        self.assertIn("Investigate: looked", out.yielded[0])

    def test_unreachable_mark_counts_a_rejection(self):
        # Every cell beside the mark is known wall: there is nowhere to look from.
        w = world([".....", ".###.", ".#D#.", ".###.", "....."], at=(0, 0), perception=8)
        kb = KnowledgeBase.empty("sandbox")
        kb.entrances["7:2,2"] = {"map_id": 7, "x": 2, "y": 2}
        ctx = PlayContext(Memory(), Policy(kind="scripted"), random.Random(0), knowledge=kb)
        for n in range(1, MAX_REJECTIONS + 1):
            dispatch(w, ctx)
            self.assertEqual(ctx.memory.investigate_rejections.get(look_key(7, (2, 2))), n)
        self.assertEqual(list_interest(w, kb, Policy(kind="scripted"), ctx.memory), [])

    def test_unrevealed_look_counts_a_rejection(self):
        # Adjacent, but the look cannot finish: capped like an unreachable mark.
        w = world(["...", ".D.", "..."], at=(0, 0))
        kb = KnowledgeBase.empty("sandbox")
        kb.entrances["7:1,1"] = {"map_id": 7, "x": 1, "y": 1}
        ctx = PlayContext(Memory(), Policy(kind="scripted"), random.Random(0), knowledge=kb)
        with mock.patch("agentrealm_agent.states.investigate.apply_door_look", return_value=False):
            for n in range(1, MAX_REJECTIONS + 1):
                dispatch(w, ctx)
                self.assertEqual(ctx.memory.investigate_rejections.get(look_key(7, (1, 1))), n)
        self.assertEqual(list_interest(w, kb, Policy(kind="scripted"), ctx.memory), [])


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
        # The parsed flag reaches the look: a locked door records a key need.
        w.apply_observation({"version": "5", "delta": {"terrain": {
            "changed": [{"map_id": 7, "x": 2, "y": 1, "block_type": "framed_door", "locked": True}]}}})
        kb = KnowledgeBase.empty("sandbox")
        kb.entrances["7:2,1"] = {"map_id": 7, "x": 2, "y": 1}
        self.assertTrue(apply_door_look(kb, w, 7, (2, 1)))
        self.assertEqual(kb.entrances["7:2,1"]["needs"], "key")


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

    def test_directive_curiosity_sets_the_cap_through_the_state(self):
        def outcome(curiosity: float):
            w = world(["." * 8 for _ in range(8)], at=(0, 0), perception=8)
            w.view.tiles[(5, 5)] = "framed_door"
            w.tick = 600
            kb = KnowledgeBase.empty("sandbox")
            kb.entrances["7:5,5"] = {"map_id": 7, "x": 5, "y": 5}
            # 60 charged ticks already in the window.
            m = Memory(curiosity_spans=[(500, 60)])
            ctx = PlayContext(m, Policy(kind="scripted"), random.Random(0), knowledge=kb)
            ctx.params["curiosity"] = curiosity
            return dispatch(w, ctx)

        self.assertEqual(outcome(0.2).state, "Investigate", "cap 120 leaves room")
        self.assertNotEqual(outcome(0.05).state, "Investigate", "cap 30 is spent")


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
        cfg = CharacterConfig("T", "default", "test", "sandbox", Policy(kind="scripted", goals=["explore"]), Path("t.toml"))
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
        for _ in range(MAX_REJECTIONS):
            self.assertEqual(pick_interest_tick(r.world, r.knowledge, r.cfg.policy, r.mem, params=r.directives.directives.params).kind, "read_block")
            r.mem.pending = intent
            self.assertTrue(r.on_result({"outcome": "rejected", "tick": 5,
                                         "rejection": {"category": "target", "code": "nothing_to_read"}}, 0))
        self.assertFalse(cell_was_read(r.knowledge, 7, (1, 1)))
        self.assertIsNone(pick_interest_tick(r.world, r.knowledge, r.cfg.policy, r.mem, params=r.directives.directives.params),
                          "a refused read stops holding Investigate above Explore")


    def test_sent_look_walk_is_charged_and_then_refused(self):
        r = self.runner([{"tick": 100}])
        r.world = world(["." * 8 for _ in range(8)], at=(0, 0), perception=8)
        r.world.view.tiles[(5, 5)] = "framed_door"
        r.knowledge.entrances["7:5,5"] = {"map_id": 7, "x": 5, "y": 5}
        params = r.directives.directives.params
        params["curiosity"] = 1 / 600  # cap of one tick
        r.tick()
        self.assertEqual(r.mem.state, "Investigate")
        self.assertTrue(r.mem.curiosity_spans)
        self.assertEqual(r.mem.curiosity_spans[0][0], 100)
        self.assertIsNone(pick_interest_tick(r.world, r.knowledge, r.cfg.policy, r.mem, params=params),
                          "the spent budget refuses the look")

if __name__ == "__main__":
    unittest.main()
