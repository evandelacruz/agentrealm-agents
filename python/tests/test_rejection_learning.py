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
from agentrealm_agent.directives import PARAM_DEFAULTS
from agentrealm_agent.navigation import CostGridParams, cost_path
from agentrealm_agent.navigation.rejection import (
    OCCUPANT_LEARN_TICKS,
    NavMemory,
    learn_step_rejection,
    navigation_avoid_costly,
    on_block_changed,
)
from agentrealm_agent.plan import Plan
from agentrealm_agent.runner import Runner
from agentrealm_agent.world import Entity, WorldModel, ZoneFact
from tests.test_cost_grid import grid
from tests.test_runner import FakeClient, rejected


def scripted(**kw) -> Policy:
    kw.setdefault("pickup", False)
    return Policy(kind="scripted", **kw)


def builtin_plan(policy: Policy) -> Plan:
    return Plan.from_policy(policy, dict(PARAM_DEFAULTS))


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
        policy = scripted(goals=["goto"], goto=(2, 0))
        d = decide(w, m, policy, random.Random(0), knowledge=self.kb, plan=builtin_plan(policy))
        self.assertEqual(target(d), (1, 0), "a map-1 block does not stop a step on map 2")

    def test_block_occupied_waits_one_decision_then_costs_until_expiry(self):
        w = grid(["...", "..."])
        w.tick = 10
        m = Memory()
        learn_step_rejection(m, w, None, (1, 0), "block_occupied", 10)
        avoid, costly = navigation_avoid_costly(m.nav, None, 1, 10)
        self.assertEqual((avoid, costly), ({(1, 0)}, {(1, 0)}))
        policy = scripted(goals=["goto"], goto=(2, 0))
        d = decide(w, m, policy, random.Random(0), plan=builtin_plan(policy))
        self.assertEqual(target(d), (1, 1), "routes round the occupied cell")
        avoid, costly = navigation_avoid_costly(m.nav, None, 1, 11)
        self.assertEqual((avoid, costly), (set(), {(1, 0)}))
        until = 10 + OCCUPANT_LEARN_TICKS
        self.assertEqual(navigation_avoid_costly(m.nav, None, 1, until)[1], set(), "expired")
        self.assertIn((1, (1, 0)), m.nav.occupant_until, "reading does not prune")
        w.tick = until
        decide(w, m, scripted(goals=[]), random.Random(0))
        self.assertEqual(m.nav.occupant_until, {}, "a decision prunes expired costs")

    def test_conflict_lost_does_not_block_the_tile(self):
        w = grid(["..."])
        m = Memory()
        learn_step_rejection(m, w, None, (1, 0), "conflict_lost", 10)
        self.assertEqual(navigation_avoid_costly(m.nav, None, 1, 10), (set(), set()))

    def test_unknown_code_keeps_off_for_one_decision_only(self):
        # would_strand is handled like any other code (PLAN.md A14, reflex 1 table).
        for code in ("some_new_code", "would_strand", None):
            with self.subTest(code=code):
                w = grid(["...", "..."])
                m = Memory()
                learn_step_rejection(m, w, None, (1, 0), code, 10)
                self.assertEqual(m.nav.impassable, set())
                self.assertEqual(m.nav.occupant_until, {})
                policy = scripted(goals=["goto"], goto=(2, 0))
                d = decide(w, m, policy, random.Random(0), plan=builtin_plan(policy))
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


class RunnerRejectionTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for mod, name in ((config, "STATE_DIR"), (kb_mod, "WORLDS_DIR")):
            patch = mock.patch.object(mod, name, Path(tmp.name) / name)
            patch.start()
            self.addCleanup(patch.stop)

    def runner(self, client, knowledge=None) -> Runner:
        pol = Policy(goals=["goto"], goto=(4, 0), pickup=False)
        cfg = CharacterConfig("T", "sandbox", pol, Path("t.toml"))
        r = Runner(cfg, client, 1, threading.Event(), out=lambda _: None, knowledge=knowledge)
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

    def test_block_occupied_and_door_locked_reach_the_learnings(self):
        kb = kb_mod.KnowledgeBase.empty("sandbox")
        for code, check in (
            ("block_occupied", lambda r: self.assertEqual(
                r.mem.nav.occupant_until, {(7, (1, 0)): 10 + OCCUPANT_LEARN_TICKS})),
            ("door_locked", lambda r: self.assertEqual(
                kb.maps["7"]["doors"], [{"x": 1, "y": 0, "block_type": "framed_door", "locked": True}])),
        ):
            with self.subTest(code=code):
                fake = FakeClient([
                    {"tick": 10, "window_remaining_ms": 0},
                    {"tick": 11, "window_remaining_ms": 0,
                     "intent_results": [rejected("q1", code, "terrain", 10)]},
                ])
                r = self.runner(fake, knowledge=kb)
                r.tick()
                r.tick()
                check(r)

    def test_reflex_probe_while_held_does_not_age_the_learnings(self):
        r = self.runner(FakeClient([]))
        r.world.entities = [Entity("npc", 9, (2, 0), "gnawer")]
        r.world.hostile_types.add(("npc", "gnawer"))  # a type seen attacking (survival.is_hostile)
        r.cfg.policy.hostile, r.cfg.policy.hostile_range = ["npc"], 2
        r.mem.nav.wait_tile = (7, (1, 1))
        r.mem.nav.occupant_until[(7, (0, 1))] = 5
        r.world.tick = 50
        d = r.reflex_while_held()
        self.assertIsNotNone(d)
        self.assertTrue(d.reason.startswith("flee"), d.reason)
        self.assertEqual(r.mem.nav.wait_tile, (7, (1, 1)))
        self.assertEqual(r.mem.nav.occupant_until, {(7, (0, 1)): 5})

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
