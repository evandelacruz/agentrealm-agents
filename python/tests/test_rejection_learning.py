"""Rejection learning for the navigation map (A14)."""

import random
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agentrealm_agent import config
from agentrealm_agent import knowledge_base as kb_mod
from agentrealm_agent.brain import Memory, decide
from agentrealm_agent.config import CharacterConfig, Policy
from agentrealm_agent.navigation import CostGridParams, cost_path
from agentrealm_agent.navigation.rejection import (
    LAND_TRIES,
    OCCUPANT_LEARN_TICKS,
    NavMemory,
    learn_step_rejection,
    navigation_avoid_costly,
    on_block_changed,
)
from agentrealm_agent.runner import Runner
from agentrealm_agent.world import Entity, WorldModel, ZoneFact
from tests.test_cost_grid import grid
from tests.test_runner import FakeClient, rejected


def scripted(**kw) -> Policy:
    kw.setdefault("pickup", False)
    return Policy(kind="scripted", **kw)


def target(d) -> tuple[int, int]:
    assert d.intent is not None, d.reason
    return (d.intent["x"], d.intent["y"])


class RejectionLearningTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.worlds = Path(self.tmp.name) / "worlds"
        patch = mock.patch.object(kb_mod, "WORLDS_DIR", self.worlds)
        patch.start()
        self.addCleanup(patch.stop)
        self.kb = kb_mod.KnowledgeBase.empty("sandbox")

    def test_not_traversable_stays_impassable_until_block_changed(self):
        w = grid(["#####", "#...#", "#####"], at=(1, 1))
        m = Memory()
        learn_step_rejection(m, w, None, (2, 1), "not_traversable", 10)
        for tick in (10, 10_000):
            avoid, _ = navigation_avoid_costly(m.nav, None, 1, tick)
            self.assertIn((2, 1), avoid, "not aged out by time")
        self.assertIsNone(cost_path(w, (3, 1), CostGridParams(avoid=avoid)))
        on_block_changed(m, 2, (2, 1))
        self.assertIn((2, 1), navigation_avoid_costly(m.nav, None, 1, 11)[0], "other map's change")
        on_block_changed(m, 1, (2, 1))
        avoid, _ = navigation_avoid_costly(m.nav, None, 1, 11)
        self.assertNotIn((2, 1), avoid)
        self.assertIsNotNone(cost_path(w, (3, 1), CostGridParams(avoid=avoid)))

    def test_learnings_stay_on_their_map(self):
        w = grid(["...", "..."])
        m = Memory()
        learn_step_rejection(m, w, self.kb, (1, 0), "not_traversable", 10)
        learn_step_rejection(m, w, self.kb, (1, 1), "block_occupied", 10)
        learn_step_rejection(m, w, self.kb, (2, 0), "door_locked", 10)
        self.assertEqual(navigation_avoid_costly(m.nav, self.kb, 2, 10), (set(), set()))
        w = grid(["...", "..."])
        w.maps[2] = w.maps.pop(1)
        w.map_id = w.terrain_map = 2
        d = decide(w, m, scripted(goals=["goto"], goto=(2, 0)), random.Random(0), knowledge=self.kb)
        self.assertEqual(target(d), (1, 0), "a map-1 block does not stop a step on map 2")

    def test_block_occupied_waits_one_decision_then_costs_until_expiry(self):
        w = grid(["...", "..."])
        w.tick = 10
        m = Memory()
        learn_step_rejection(m, w, None, (1, 0), "block_occupied", 10)
        avoid, costly = navigation_avoid_costly(m.nav, None, 1, 10)
        self.assertEqual((avoid, costly), ({(1, 0)}, {(1, 0)}))
        d = decide(w, m, scripted(goals=["goto"], goto=(2, 0)), random.Random(0))
        self.assertEqual(target(d), (1, 1), "routes round the occupied cell")
        avoid, costly = navigation_avoid_costly(m.nav, None, 1, 11)
        self.assertEqual((avoid, costly), (set(), {(1, 0)}))
        until = 10 + OCCUPANT_LEARN_TICKS
        self.assertEqual(navigation_avoid_costly(m.nav, None, 1, until)[1], set(), "expired")
        self.assertIn((1, (1, 0)), m.nav.occupant_until, "reading does not prune")
        w.tick = until
        decide(w, m, scripted(goals=["hold"]), random.Random(0))
        self.assertEqual(m.nav.occupant_until, {}, "a decision prunes expired costs")

    def test_conflict_lost_does_not_block_the_tile(self):
        w = grid(["..."])
        m = Memory()
        learn_step_rejection(m, w, None, (1, 0), "conflict_lost", 10)
        self.assertEqual(navigation_avoid_costly(m.nav, None, 1, 10), (set(), set()))

    def test_unknown_code_keeps_off_for_one_decision_only(self):
        for code in ("some_new_code", None):
            with self.subTest(code=code):
                w = grid(["...", "..."])
                m = Memory()
                learn_step_rejection(m, w, None, (1, 0), code, 10)
                self.assertEqual(m.nav.impassable, set())
                self.assertEqual(m.nav.occupant_until, {})
                d = decide(w, m, scripted(goals=["goto"], goto=(2, 0)), random.Random(0))
                self.assertNotEqual(target(d), (1, 0))
                self.assertEqual(navigation_avoid_costly(m.nav, None, 1, 10), (set(), set()))

    def test_door_locked_records_in_knowledge_base(self):
        w = grid(["..D"], at=(0, 0))
        m = Memory()
        learn_step_rejection(m, w, self.kb, (1, 0), "door_locked", 5)
        doors = self.kb.maps["1"]["doors"]
        self.assertTrue(any(d["x"] == 1 and d.get("locked") for d in doors))
        avoid, _ = navigation_avoid_costly(Memory().nav, self.kb, 1, 5)
        self.assertIn((1, 0), avoid, "a fresh character reads the lock from the knowledge base")

    def test_over_strength_ceiling_records_hunting_closure(self):
        w = grid(["..."])
        w.zones[1] = {(1, 0): ZoneFact(safe=False, strength_ceiling=12)}
        m = Memory()
        learn_step_rejection(m, w, self.kb, (1, 0), "over_strength_ceiling", 20)
        hunting = self.kb.maps["1"]["hunting"]["1,0"]
        self.assertTrue(hunting["closed"])
        self.assertEqual(hunting["strength_ceiling"], 12)
        self.assertIn((1, 0), navigation_avoid_costly(m.nav, self.kb, 1, 20)[0])


class WouldStrandTest(unittest.TestCase):
    def test_steps_to_land_before_goals(self):
        w = grid(["..."], at=(0, 0))
        m = Memory()
        learn_step_rejection(m, w, None, (1, 0), "would_strand", 5)
        d = decide(w, m, scripted(goals=["goto"], goto=(0, 0), pickup=True), random.Random(0))
        self.assertEqual((target(d), d.reason), ((1, 0), "land first"))

    def test_threat_reflexes_come_first(self):
        w = grid([".....", "....."], at=(1, 0))
        w.entities = [Entity("npc", 9, (0, 0))]
        m = Memory(nav=NavMemory(prefer_land=(1, (2, 0))))
        d = decide(w, m, scripted(goals=["hold"], on_hostile="flee", hostile=["npc"], hostile_range=2),
                   random.Random(0))
        self.assertTrue(d.reason.startswith("flee"), d.reason)
        self.assertEqual(m.nav.prefer_land, (1, (2, 0)), "kept for after the threat")

    def test_land_step_is_bounded(self):
        w = grid(["..."], at=(0, 0))
        m = Memory(nav=NavMemory(prefer_land=(1, (1, 0))))
        pol = scripted(goals=["hold"])
        for _ in range(LAND_TRIES):
            self.assertEqual(decide(w, m, pol, random.Random(0)).reason, "land first")
        self.assertNotEqual(decide(w, m, pol, random.Random(0)).reason, "land first")
        self.assertIsNone(m.nav.prefer_land)

    def test_dropped_when_not_open_or_refused_again(self):
        w = grid([".#."], at=(0, 0))
        m = Memory(nav=NavMemory(prefer_land=(1, (1, 0))))
        decide(w, m, scripted(goals=["hold"]), random.Random(0))
        self.assertIsNone(m.nav.prefer_land, "not open now")

        w = grid(["..."], at=(0, 0))
        m = Memory()
        learn_step_rejection(m, w, None, (1, 0), "would_strand", 5)
        learn_step_rejection(m, w, None, (1, 0), "would_strand", 6)
        self.assertIsNone(m.nav.prefer_land, "a refused land step is not retried")
        self.assertEqual(m.nav.wait_tile, (1, (1, 0)))


class RunnerRejectionTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.object(config, "STATE_DIR", Path(tmp.name))
        patch.start()
        self.addCleanup(patch.stop)

    def runner(self, client) -> Runner:
        pol = Policy(goals=["goto"], goto=(4, 0), pickup=False)
        cfg = CharacterConfig("T", "default", "test", "sandbox", pol, Path("t.toml"))
        r = Runner(cfg, client, 1, threading.Event(), out=lambda _: None)
        self.addCleanup(r.trace.close)
        w = WorldModel(character_id=1, map_id=7, pos=(0, 0), perception=3)
        for y in range(2):
            for x in range(5):
                w.view.tiles[(x, y)] = "dirt"
        r.world, r.mem = w, Memory(need_self=False, need_position=False)
        return r

    def test_rejection_code_teaches_the_map_and_block_changed_clears_it(self):
        changed = {"tick": 12, "events": [{"kind": "BlockChanged", "map_id": 7, "x": 1, "y": 0,
                                           "block_type": "dirt"}]}
        fake = FakeClient([
            {"tick": 10, "window_remaining_ms": 0},
            {"tick": 11, "window_remaining_ms": 0,
             "intent_results": [rejected("q1", "not_traversable", "terrain", 10)]},
            {"tick": 13, "window_remaining_ms": 0, "events_by_tick": [changed]},
        ])
        r = self.runner(fake)
        r.tick()
        self.assertEqual(fake.sent[0][0][0], {"verb": "Step", "direction": "right"})
        r.tick()
        self.assertEqual(r.mem.nav.impassable, {(7, (1, 0))})
        r.mem.need_position = False
        r.tick()
        self.assertNotEqual(fake.sent[2][0][0], {"verb": "Step", "direction": "right"})
        self.assertEqual(r.mem.nav.impassable, set(), "BlockChanged on our map clears it")

    def test_block_changed_on_another_map_keeps_the_block(self):
        other = {"tick": 12, "events": [{"kind": "BlockChanged", "map_id": 8, "x": 1, "y": 0,
                                         "block_type": "dirt"}]}
        fake = FakeClient([{"tick": 13, "window_remaining_ms": 0, "events_by_tick": [other]}])
        r = self.runner(fake)
        r.world.maps[8] = type(r.world.view)()
        r.mem.nav.impassable.add((7, (1, 0)))
        r.tick()
        self.assertEqual(r.mem.nav.impassable, {(7, (1, 0))})


if __name__ == "__main__":
    unittest.main()
