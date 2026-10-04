"""Interest list and Investigate state (A30)."""

import random
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agentrealm_agent import config, runner as runner_mod
from agentrealm_agent.brain import Decision, choose_call, decide
from agentrealm_agent.config import CharacterConfig, Policy
from agentrealm_agent.curiosity import curiosity_ticks_used, detour_allowed, record_curiosity_queue
from agentrealm_agent.directives import Directives
from agentrealm_agent.interest_list import MAX_REJECTIONS, InterestItem, list_interest, pick_interest_tick, sight_range
from agentrealm_agent.investigation import cell_was_read, mark_cell_read, mark_npc_spoken, spoken_npc_ids
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.memory import Memory
from agentrealm_agent.runner import Runner
from agentrealm_agent.states import dispatch
from agentrealm_agent.states.base import PlayContext
from agentrealm_agent.states.intents import read_block, set_position
from agentrealm_agent.world import Entity, WorldModel, ZoneFact


def world(rows: list[str], at=(1, 1), perception=3) -> WorldModel:
    glyph = {".": "dirt", "#": "wall", "S": "wall"}
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

    def test_detour_waits_for_budget_free_items_do_not(self):
        w = world(["..."], at=(1, 0))
        detour = InterestItem("read_block", "detour", False, "k")
        free = InterestItem("read_block", "free", True, "f")
        m = Memory()
        record_curiosity_queue(m, 0, 150)
        w.tick = 100
        d = Directives(params={"curiosity": 0.2})
        with mock.patch("agentrealm_agent.interest_list.list_interest", return_value=[detour]):
            self.assertIsNone(pick_interest_tick(w, None, Policy(kind="scripted"), m, d), "150 > 0.2 * 600")
            self.assertIs(pick_interest_tick(w, None, Policy(kind="scripted"), m, Directives(params={"curiosity": 0.5})), detour)
        with mock.patch("agentrealm_agent.interest_list.list_interest", return_value=[free]):
            self.assertIs(pick_interest_tick(w, None, Policy(kind="scripted"), m, d), free)


class InvestigateStateTest(unittest.TestCase):
    def test_investigate_beats_explore_for_unread_sign(self):
        w = world(["...", ".S.", "..."], at=(0, 1))
        ctx = PlayContext(Memory(), Policy(kind="scripted", goals=["explore"]), random.Random(0), knowledge=KnowledgeBase.empty("sandbox"))
        out = dispatch(w, ctx)
        self.assertEqual(out.state, "Investigate")
        self.assertEqual(out.intents, [read_block(7, (1, 1))])

    def test_decide_says_to_npc(self):
        w = world(["...", "...", "..."], at=(0, 1))
        w.entities = [Entity("npc", 4, (2, 2), "helper")]
        d = decide(w, Memory(), Policy(kind="scripted", hostile=[]), random.Random(0), knowledge=KnowledgeBase.empty("sandbox"))
        self.assertEqual(d.intent["verb"], "Say")
        self.assertEqual(d.intent["target"], {"kind": "npc", "npc_id": 4})

    def test_act_has_no_side_effects(self):
        w = world(["...", ".S.", "..."], at=(0, 1))
        kb = KnowledgeBase.empty("sandbox")
        ctx = PlayContext(Memory(), Policy(kind="scripted"), random.Random(0), knowledge=kb)
        before = kb.to_dict()
        dispatch(w, ctx)
        dispatch(w, ctx)
        after = kb.to_dict()
        self.assertEqual(before, after, "only an applied result marks the knowledge base")


class CuriosityBudgetTest(unittest.TestCase):
    def test_segments_sum_in_window(self):
        m = Memory()
        record_curiosity_queue(m, 100, 50)
        record_curiosity_queue(m, 200, 40)
        self.assertEqual(curiosity_ticks_used(m, 250), 90)
        self.assertEqual(curiosity_ticks_used(m, 740), 50, "only the part of a segment inside the window")
        self.assertEqual(curiosity_ticks_used(m, 900), 0)

    def test_record_drops_segments_outside_the_window(self):
        m = Memory()
        record_curiosity_queue(m, 0, 10)
        record_curiosity_queue(m, 1000, 10)
        self.assertEqual(m.curiosity_segments, [(1000, 10)])

    def test_detour_allowed_tracks_param(self):
        m = Memory()
        record_curiosity_queue(m, 0, 150)
        self.assertFalse(detour_allowed(m, 100, 0.2))
        self.assertTrue(detour_allowed(m, 100, 1.0))


class SightRangeTest(unittest.TestCase):
    def test_brightness_caps_sight(self):
        w = WorldModel(1, map_id=1, pos=(0, 0), perception=5)
        w.zones[1] = {(0, 0): ZoneFact(safe=False, brightness=0.5)}
        self.assertLess(sight_range(w, 1, (0, 0)), 5)


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
        cfg = CharacterConfig("T", "default", "test", "sandbox", Policy(kind="scripted", goals=["explore"]), Path("t.toml"))
        r = Runner(cfg, client, 1, threading.Event(), out=lambda _: None, knowledge=KnowledgeBase.empty("sandbox"))
        self.addCleanup(r.trace.close)
        r.world = world([".....", ".S...", "....."], at=(0, 1))
        r.mem = Memory(need_self=False, need_position=False)
        return r

    def test_applied_read_is_remembered(self):
        r = self.runner([])
        intent = read_block(7, (1, 1))
        r.mem.pending = intent
        self.assertFalse(r.on_result({"outcome": "applied", "tick": 5}, 0))
        self.assertTrue(cell_was_read(r.knowledge, 7, (1, 1)))

    def test_rejected_read_is_capped(self):
        r = self.runner([])
        intent = read_block(7, (1, 1))
        for _ in range(MAX_REJECTIONS):
            self.assertEqual(pick_interest_tick(r.world, r.knowledge, r.cfg.policy, r.mem, Directives()).kind, "read_block")
            r.mem.pending = intent
            self.assertTrue(r.on_result({"outcome": "rejected", "tick": 5,
                                         "rejection": {"category": "target", "code": "nothing_to_read"}}, 0))
        self.assertFalse(cell_was_read(r.knowledge, 7, (1, 1)))
        self.assertIsNone(pick_interest_tick(r.world, r.knowledge, r.cfg.policy, r.mem, Directives()),
                          "a refused read stops holding Investigate above Explore")

    def test_investigate_walk_queue_is_charged(self):
        r = self.runner([{"tick": 10, "window_remaining_ms": 0}])
        r.mem.state = "Investigate"
        r.mem.path = [(1, 0), (2, 0)]
        with mock.patch.object(runner_mod, "decide", return_value=Decision(set_position((1, 0)), "detour")):
            r.tick()
        sent = r.client.tick.call_args[0][1]
        self.assertTrue(any(i["verb"] == "Step" for i in sent))
        self.assertEqual(curiosity_ticks_used(r.mem, 10), len(sent))

    def test_free_read_and_other_states_are_not_charged(self):
        for state, decision in (("Investigate", Decision(read_block(7, (1, 1)), "read")),
                                ("Explore", Decision(set_position((1, 0)), "explore"))):
            with self.subTest(state=state):
                r = self.runner([{"tick": 10, "window_remaining_ms": 0}])
                r.mem.state = state
                with mock.patch.object(runner_mod, "decide", return_value=decision):
                    r.tick()
                self.assertEqual(r.mem.curiosity_segments, [])


if __name__ == "__main__":
    unittest.main()
