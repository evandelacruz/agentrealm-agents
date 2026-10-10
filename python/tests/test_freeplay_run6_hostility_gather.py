"""Free-play run 6 offline: hostility kept across runs, and Gather's pick (A22, A71, A67).

1. Run 6 started beside the NPC that killed run 5 and did not know its type
   was hostile: hostile types and sightings lived only for one run, and the
   knowledge base's ``npc_types`` was never written. They are now saved at
   exit and loaded at start (``hostile_memory``), and feed the same danger
   tests as anything learned this run.
2. Gather walked back and forth between a bush 4–9 cells off and grass by
   the character, 8 walks in 34 s, because its pick took any bush before any
   grass. It now takes the nearest of either by walk.
"""

from __future__ import annotations

import json
import random
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agentrealm_agent import config
from agentrealm_agent.config import CharacterConfig, Policy
from agentrealm_agent.directives import PARAM_DEFAULTS
from agentrealm_agent.hostile_memory import SIGHTINGS_KEY, load_hostiles, save_hostiles
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.memory import Memory
from agentrealm_agent.plan import Plan
from agentrealm_agent.runner import Runner
from agentrealm_agent.states import PlayContext
from agentrealm_agent.states.detour import detour_find
from agentrealm_agent.states.gather import gather_outcome
from agentrealm_agent.hostile_ground import known_reach
from agentrealm_agent.states.gather_safe import gather_ground
from agentrealm_agent.survival import hostiles_in_range, known_hostile
from agentrealm_agent.world import EMPTY_POST_HALF_LIFE_TICKS, POST_STILL_TICKS, SIGHTING_TICKS, Entity, WorldModel

MAP = 1
CODE = "fake_gnasher"
POST = (20, 10)


def policy(**kw) -> Policy:
    return Policy(kind="scripted", **{"goals": [], "on_hostile": "ignore", **kw})


def field(at=(2, 10), size=40, perception=6) -> WorldModel:
    w = WorldModel(character_id=1, map_id=MAP, pos=at, perception=perception)
    for x in range(size):
        for y in range(size):
            w.view.tiles[(x, y)] = "dirt"
    w.terrain_center, w.terrain_map = at, MAP
    w.health, w.max_health = 100, 100
    return w


def gnasher(at=POST, npc_id=9) -> Entity:
    return Entity("npc", npc_id, at, CODE)


def see(w: WorldModel, entities: list[Entity], tick: int) -> None:
    w.tick = tick
    w._set_entities(entities, tick)


def hit_by(w: WorldModel, npc_id: int, tick: int) -> None:
    """``npc_id`` swings at and hits us."""
    events = [
        {"kind": "Attacked", "actor_kind": "npc", "actor_id": npc_id, "tick": tick},
        {"kind": "Damaged", "amount": 2, "source_kind": "npc", "source_id": npc_id, "tick": tick},
    ]
    w.apply_events([{"tick": tick, "events": events}])
    w.learn_threat(events, w.entities)


def run_five() -> WorldModel:
    """A run that met the gnasher at its post, was hit by it, and walked off."""
    w = field(at=(15, 10))
    see(w, [gnasher()], 0)
    see(w, [gnasher()], POST_STILL_TICKS)
    hit_by(w, 9, POST_STILL_TICKS + 1)
    w.pos = (2, 10)
    see(w, [], POST_STILL_TICKS + 2)
    return w


def through_json(kb: KnowledgeBase) -> KnowledgeBase:
    """``kb`` as the next run loads it from its file."""
    return KnowledgeBase.from_dict(kb.world_code, json.loads(json.dumps(kb.to_dict())))


class HostilityKeptAcrossRunsTest(unittest.TestCase):
    def saved(self) -> KnowledgeBase:
        kb = KnowledgeBase.empty("fake-world")
        save_hostiles(kb, run_five())
        return through_json(kb)

    def test_a_type_that_hit_us_is_written_to_npc_types(self):
        """With what it measured: its largest hit and its swings that hit (A84)."""
        self.assertEqual(self.saved().npc_types, {CODE: {"hostile": True, "max_hit": 2, "hits": 1}})

    def test_the_next_run_knows_the_type_hostile_before_it_swings(self):
        w = field(at=(18, 10))
        load_hostiles(self.saved(), w)
        see(w, [gnasher()], 10_000)
        self.assertTrue(known_hostile(w, w.entities[0]))
        self.assertEqual(hostiles_in_range(w, policy()), w.entities)

    def test_another_npc_of_that_type_is_hostile_too(self):
        w = field(at=(17, 10))
        load_hostiles(self.saved(), w)
        see(w, [gnasher((18, 11), npc_id=12)], 10_000)
        self.assertTrue(known_hostile(w, w.entities[0]))

    def test_its_post_and_reach_hold_ground_out_of_view(self):
        w = field()  # the post is far out of view
        load_hostiles(self.saved(), w)
        reach = max(policy().hostile_range, 5) + 1  # hit from (15, 10), 5 off its post
        self.assertIn((POST, reach), known_reach(w, policy()))
        self.assertFalse(gather_ground(w, (21, 11), policy()))

    def test_detour_skips_a_pile_beside_the_remembered_post(self):
        w = field(at=(17, 10))
        load_hostiles(self.saved(), w)
        see(w, [gnasher(), Entity("supply", 50, (21, 11), "gem")], 10_000)  # back on its post
        m = Memory(path=[(16, 10), (15, 10), (14, 10)], goal="explore")
        ctx = PlayContext(m, policy(), random.Random(0), plan=Plan([], dict(PARAM_DEFAULTS)))
        self.assertIsNone(detour_find(w, ctx))

    def test_a_type_never_hostile_is_not_written(self):
        w = field(at=(15, 10))
        see(w, [Entity("npc", 3, (16, 10), "fake_baker")], 0)
        kb = KnowledgeBase.empty("fake-world")
        save_hostiles(kb, w)
        self.assertEqual(kb.npc_types, {})
        self.assertNotIn(SIGHTINGS_KEY, kb.extra)

    def test_other_rows_of_npc_types_are_kept(self):
        kb = KnowledgeBase.empty("fake-world")
        kb.npc_types[CODE] = {"name": "Fake Gnasher"}
        save_hostiles(kb, run_five())
        self.assertEqual(kb.npc_types[CODE], {"name": "Fake Gnasher", "hostile": True, "max_hit": 2, "hits": 1})


class LoadedSightingsTest(unittest.TestCase):
    def kb(self, **row) -> KnowledgeBase:
        kb = KnowledgeBase.empty("fake-world")
        kb.npc_types[CODE] = {"hostile": True}
        base = {"code": CODE, "map_id": MAP, "x": 20, "y": 10, "tick": 1000, "home": [20, 10], "post": False, "reach": 0}
        kb.extra[SIGHTINGS_KEY] = {"9": {**base, **row}}
        return kb

    def test_a_passer_by_expires_by_world_time(self):
        w = field()
        load_hostiles(self.kb(), w)
        see(w, [], 1000 + SIGHTING_TICKS // 2)
        self.assertEqual(len(known_reach(w, policy())), 1)
        see(w, [], 1000 + SIGHTING_TICKS + 1)
        self.assertEqual(known_reach(w, policy()), [])

    def test_a_post_found_empty_is_forgotten_and_removed_from_the_file(self):
        kb = self.kb(post=True)
        w = field(at=(17, 10))
        loaded = load_hostiles(kb, w)
        for t in range(5000, 5000 + 4 * EMPTY_POST_HALF_LIFE_TICKS):
            see(w, [], t)  # the post is in sight, with nobody on it
        self.assertEqual(w.sightings, {})
        save_hostiles(kb, w, loaded)
        self.assertNotIn(SIGHTINGS_KEY, kb.extra)

    def test_a_dead_npc_is_removed_from_the_file(self):
        kb = self.kb(post=True)
        w = field()
        loaded = load_hostiles(kb, w)
        w.learn_threat([{"kind": "NPCDied", "npc_id": 9, "npc_type": CODE}], [])
        save_hostiles(kb, w, loaded)
        self.assertNotIn(SIGHTINGS_KEY, kb.extra)

    def test_rows_this_run_never_loaded_are_left_alone(self):
        kb = self.kb(post=True)
        w = field()
        save_hostiles(kb, w, set())
        self.assertIn("9", kb.extra[SIGHTINGS_KEY])

    def test_a_post_on_another_map_holds_nothing_here(self):
        w = field()
        load_hostiles(self.kb(post=True, map_id=MAP + 1), w)
        self.assertEqual(known_reach(w, policy()), [])

    def test_a_broken_row_is_skipped(self):
        kb = self.kb()
        kb.extra[SIGHTINGS_KEY]["10"] = {"code": CODE, "x": "far"}
        kb.extra[SIGHTINGS_KEY]["x"] = {"code": CODE}
        w = field()
        self.assertEqual(load_hostiles(kb, w), {9})


class _WorldOnlyClient:
    """Answers the start-up reads, then stops the run."""

    def __init__(self, stop: threading.Event):
        self.stop = stop

    def world(self, cid):
        self.stop.set()
        return {"code": "fake-world", "status": "live", "tick_rate_hz": 10}

    def minimap(self, cid):
        return {}


class RunnerKeepsHostilityTest(unittest.TestCase):
    def test_the_runner_loads_at_start_and_saves_at_exit(self):
        kb = KnowledgeBase.empty("fake-world")
        save_hostiles(kb, run_five())
        kb = through_json(kb)
        stop = threading.Event()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(config, "STATE_DIR", Path(tmp)):
            cfg = CharacterConfig("T", "sandbox", Policy(), Path("t.toml"))
            r = Runner(cfg, _WorldOnlyClient(stop), 1, stop, out=lambda _: None, knowledge=kb)
            self.assertIn(("npc", CODE), r.world.hostile_types)
            self.assertIn(("npc", 9), r.world.sightings)
            r.world.hostile_types.add(("npc", "fake_biter"))
            r.run()
        self.assertEqual(
            kb.npc_types, {CODE: {"hostile": True, "max_hit": 2, "hits": 1}, "fake_biter": {"hostile": True}}
        )
        self.assertIn("9", kb.extra[SIGHTINGS_KEY])


def grid(rows: list[str], at) -> WorldModel:
    glyph = {".": "dirt", "g": "grass", "b": "bush"}
    w = WorldModel(character_id=1, map_id=7, pos=at, perception=12)
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            w.view.tiles[(x, y)] = glyph[ch]
    w.terrain_center, w.terrain_map = at, 7
    w.attack_range = 1
    return w


class GatherPicksTheNearestCutTest(unittest.TestCase):
    """Run 6: a bush at the region's west edge, grass to the east. Bushes
    drop berries, not gems (A81), so Gather only ever walks to the grass."""

    ROWS = ["b..........", "...gggggggg"]

    def test_grass_beside_us_beats_a_bush_far_off(self):
        w = grid(self.ROWS, at=(6, 0))
        m = Memory()
        gather_outcome(w, m, policy())
        self.assertEqual(m.gather_target, ("grass", (5, 1)))  # one step off; ties go to the smaller cell

    def test_a_nearer_bush_is_passed_for_grass(self):
        w = grid(self.ROWS, at=(1, 0))
        m = Memory()
        gather_outcome(w, m, policy())
        self.assertEqual(m.gather_target, ("grass", (3, 1)))

    def test_no_walk_back_to_the_bush_while_grass_is_near(self):
        """Cut the grass east of us one cell after another: never a walk to the bush, nor a cut of it."""
        w = grid(self.ROWS, at=(4, 0))
        m = Memory()
        targets = []
        for _ in range(40):
            out = gather_outcome(w, m, policy())
            verb = out.intents[0]["verb"] if out.intents else None
            if verb == "SetPosition":
                w.pos = (out.intents[0]["x"], out.intents[0]["y"])
                if m.gather_target not in targets:
                    targets.append(m.gather_target)
            elif verb == "Use":
                t = out.intents[0]["target"]
                w.view.tiles[(t["x"], t["y"])] = "dirt"  # cut: bare until it grows back
            else:
                break
        kinds = [k for k, _ in targets]
        self.assertEqual(kinds, ["grass"] * len(kinds))
        self.assertEqual(sum(1 for t in w.view.tiles.values() if t == "grass"), 0)
        self.assertEqual(w.view.tiles[(0, 0)], "bush", "the bush is left standing")


if __name__ == "__main__":
    unittest.main()
