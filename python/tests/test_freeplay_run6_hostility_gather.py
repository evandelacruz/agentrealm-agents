"""Free-play run 6 offline: hostility kept across runs, and Gather's pick (A22, A71, A67).

1. Run 6 started beside the NPC that killed run 5 and did not know its type
   was hostile: hostile types and sightings lived only for one run, and the
   knowledge base's ``npc_types`` was never written. They are now saved at
   exit and loaded at start (``knowledge_base.hostiles``), and feed the same danger
   tests as anything learned this run.
2. Gather walked back and forth between a bush and grass by the character:
   bushes drop berries, not gems (A81), so it cuts only grass
   (``test_gather.TargetSearchTest``).
"""

from __future__ import annotations

import json
import random
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agentrealm_adk import config
from agentrealm_adk.config import CharacterConfig, Policy
from agentrealm_adk.directives import PARAM_DEFAULTS
from agentrealm_adk.knowledge_base.hostiles import SIGHTINGS_KEY, load_hostiles, save_hostiles
from agentrealm_adk.knowledge_base import KnowledgeBase
from agentrealm_adk.memory import Memory
from agentrealm_agent.plan import Plan
from agentrealm_agent.runner import Runner
from agentrealm_agent.states import PlayContext
from agentrealm_agent.states.detour import detour_find
from agentrealm_adk.hostile_ground import known_reach
from agentrealm_agent.states.gather_safe import gather_ground
from agentrealm_adk.survival import hostiles_in_range, known_hostile
from agentrealm_adk.world import SIGHTING_TICKS, STILL_TICKS, Entity, WorldModel

MAP = 1
CODE = "fake_gnasher"
STOOD = (20, 10)  # where the gnasher stood still


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


def gnasher(at=STOOD, npc_id=9) -> Entity:
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
    """A run that met the gnasher standing still, was hit by it, and walked off."""
    w = field(at=(15, 10))
    see(w, [gnasher()], 0)
    see(w, [gnasher()], STILL_TICKS)
    hit_by(w, 9, STILL_TICKS + 1)
    w.pos = (2, 10)
    see(w, [], STILL_TICKS + 2)
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
        """With what it measured: its largest hit and its swings that hit (A85)."""
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

    def test_where_it_stood_still_holds_ground_out_of_view(self):
        w = field()  # its cell is far out of view
        load_hostiles(self.saved(), w)
        self.assertIn((STOOD, policy().hostile_range + 1), known_reach(w, policy()))
        self.assertFalse(gather_ground(w, (21, 11), policy()))

    def test_detour_skips_a_pile_beside_it(self):
        w = field(at=(17, 10))
        load_hostiles(self.saved(), w)
        see(w, [gnasher(), Entity("supply", 50, (21, 11), "gem")], 10_000)  # seen there again
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
        base = {"code": CODE, "map_id": MAP, "x": 20, "y": 10, "tick": 1000, "still": False}
        kb.extra[SIGHTINGS_KEY] = {"9": {**base, **row}}
        return kb

    def test_a_passer_by_expires_by_world_time(self):
        w = field()
        load_hostiles(self.kb(), w)
        see(w, [], 1000 + SIGHTING_TICKS // 2)
        self.assertEqual(len(known_reach(w, policy())), 1)
        see(w, [], 1000 + SIGHTING_TICKS + 1)
        self.assertEqual(known_reach(w, policy()), [])

    def test_a_still_one_found_gone_is_forgotten_and_removed_from_the_file(self):
        kb = self.kb(still=True)
        w = field(at=(17, 10))
        loaded = load_hostiles(kb, w)
        see(w, [], 5000)  # its cell is in sight, with nobody on it
        self.assertEqual(w.sightings, {})
        save_hostiles(kb, w, loaded)
        self.assertNotIn(SIGHTINGS_KEY, kb.extra)

    def test_a_dead_npc_is_removed_from_the_file(self):
        kb = self.kb(still=True)
        w = field()
        loaded = load_hostiles(kb, w)
        w.learn_threat([{"kind": "NPCDied", "npc_id": 9, "npc_type": CODE}], [])
        save_hostiles(kb, w, loaded)
        self.assertNotIn(SIGHTINGS_KEY, kb.extra)

    def test_rows_this_run_never_loaded_are_left_alone(self):
        kb = self.kb(still=True)
        w = field()
        save_hostiles(kb, w, set())
        self.assertIn("9", kb.extra[SIGHTINGS_KEY])

    def test_a_sighting_on_another_map_holds_nothing_here(self):
        w = field()
        load_hostiles(self.kb(still=True, map_id=MAP + 1), w)
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


if __name__ == "__main__":
    unittest.main()
