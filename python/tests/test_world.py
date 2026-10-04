"""World model snapshot versioning and deltas (M6)."""

import json
import unittest
from pathlib import Path

from agentrealm_agent.world import Entity, WorldModel

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "observations"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


class SnapshotObservationTest(unittest.TestCase):
    def setUp(self):
        self.w = WorldModel(character_id=1, map_id=7, pos=(5, 5), perception=3)

    def test_complete_snapshot_sets_version_and_entities(self):
        self.w.apply_observation(load("complete_v10.json"))
        self.assertEqual(self.w.snapshot_version, 10)
        kinds = {(e.kind, e.id) for e in self.w.entities}
        self.assertIn(("npc", 301), kinds)
        self.assertIn(("supply", 77), kinds)
        self.assertEqual(self.w.health, 8)
        self.assertEqual(self.w.max_health, 10)

    def test_unchanged_advances_version_only(self):
        self.w.apply_observation(load("complete_v10.json"))
        self.w.entities = [Entity("npc", 999, (0, 0), "rat")]
        self.w.apply_observation(load("unchanged_v10.json"))
        self.assertEqual(self.w.snapshot_version, 10)
        self.assertEqual(self.w.entities[0].id, 999)

    def test_entity_delta_chains_from_prior_complete(self):
        self.w.apply_observation(load("complete_v10.json"))
        self.w.apply_observation(load("delta_v11_entities.json"))
        self.assertEqual(self.w.snapshot_version, 11)
        by_id = {e.id: e for e in self.w.entities}
        self.assertEqual(by_id[301].pos, (14, 9))
        self.assertNotIn(77, by_id)
        self.assertIn(88, by_id)

    def test_resync_complete_replaces_entity_map(self):
        self.w.apply_observation(load("complete_v10.json"))
        self.w.apply_observation(load("delta_v11_entities.json"))
        self.w.apply_observation(load("complete_resync_v20.json"))
        self.assertEqual(self.w.snapshot_version, 20)
        self.assertEqual(len(self.w.entities), 1)
        self.assertEqual(self.w.entities[0].id, 400)

    def test_terrain_delta_updates_tiles(self):
        self.w.view.tiles[(2, 0)] = "grass"
        self.w.apply_observation(load("delta_v12_terrain.json"))
        self.assertEqual(self.w.snapshot_version, 12)
        self.assertEqual(self.w.view.tiles[(2, 0)], "water")
        self.assertNotIn((3, 0), self.w.view.tiles)

    def test_complete_snapshot_terrain_cells(self):
        self.w.apply_observation(load("complete_with_terrain.json"))
        self.assertEqual(self.w.view.tiles[(0, 0)], "dirt")
        self.assertEqual(self.w.view.tiles[(1, 0)], "grass")

    def test_chest_contents_from_delta_changed(self):
        self.w.death_chest = (7, (0, 0), 80)
        self.w.map_id, self.w.pos = 7, (1, 0)
        self.w.apply_observation(load("complete_v10.json"))
        self.w.apply_observation(load("delta_chest_empty.json"))
        self.assertEqual(self.w.chest_contents.get(80), [])
        self.assertIsNone(self.w.death_chest)

    def test_complete_without_version(self):
        self.w.apply_observation({"complete": True, "snapshot": {"entities": {"chests": [
            {"id": 80, "x": 0, "y": 0, "contents": [{"id": 1, "supply_subtype_code": "apple"}]},
        ]}}})
        self.assertEqual(self.w.chest_contents[80], [1])
        self.assertIsNone(self.w.snapshot_version)

    def test_snapshot_without_complete_flag_is_ignored(self):
        self.w.apply_observation({"snapshot": {"entities": {"npcs": [{"id": 5, "x": 1, "y": 1}]}}})
        self.assertEqual(self.w.entities, [])

    def test_delta_scalars(self):
        self.w.apply_observation({"version": "2", "delta": {
            "lives": 3, "alive": False, "health": 0, "max_health": 12}})
        self.assertEqual((self.w.lives, self.w.alive, self.w.health, self.w.max_health), (3, False, 0, 12))
        self.w.apply_observation({"version": "3", "delta": {"health": None}})
        self.assertIsNone(self.w.health)
        self.assertEqual(self.w.max_health, 12)

    def test_snapshot_scalars(self):
        self.w.apply_observation({"version": "4", "complete": True, "snapshot": {
            "lives": 2, "alive": True, "health": 4, "max_health": 10}})
        self.assertEqual((self.w.lives, self.w.alive, self.w.health, self.w.max_health), (2, True, 4, 10))

    def test_delta_position_update_and_null(self):
        self.w.apply_observation({"version": "2", "delta": {"position": {"map_id": 7, "x": 9, "y": 4}}})
        self.assertEqual((self.w.map_id, self.w.pos), (7, (9, 4)))
        self.assertEqual(self.w.snapshot_version, 2)
        self.w.apply_observation({"version": "3", "delta": {"position": None}})
        self.assertIsNone(self.w.pos)
        self.assertIsNone(self.w.map_id)

    def test_snapshot_without_entities_keeps_entities(self):
        self.w.apply_observation(load("complete_v10.json"))
        self.w.entities_tick = -1
        self.w.apply_observation({"version": "11", "complete": True, "snapshot": {"lives": 5}})
        self.assertIn(("npc", 301), {(e.kind, e.id) for e in self.w.entities})
        self.assertEqual(self.w.chest_contents[80], [1321])
        self.assertEqual(self.w.entities_tick, -1)

    def test_snapshot_entities_mark_entity_read(self):
        self.w.tick = 50
        self.w.apply_observation(load("complete_v10.json"))
        self.assertEqual(self.w.entities_tick, 50)

    def test_chest_removed_clears_contents(self):
        self.w.apply_observation(load("complete_v10.json"))
        self.w.apply_observation({"version": "11", "delta": {"entities": {"chests": {"removed": [80]}}}})
        self.assertNotIn(80, self.w.chest_contents)
        self.assertNotIn(("chest", 80), {(e.kind, e.id) for e in self.w.entities})

    def test_self_filtered_from_delta(self):
        me = {"id": 1, "x": 5, "y": 5, "outfit_code": "a"}
        other = {"id": 2, "x": 6, "y": 5, "outfit_code": "b"}
        self.w.apply_observation({"version": "2", "delta": {"entities": {"characters": {"added": [me, other]}}}})
        self.w.apply_observation({"version": "3", "delta": {"entities": {"characters": {"changed": [me]}}}})
        self.assertEqual([(e.kind, e.id) for e in self.w.entities], [("character", 2)])

    def test_terrain_delta_on_unseen_map(self):
        self.w.apply_observation({"version": "2", "delta": {"terrain": {
            "changed": [{"map_id": 9, "x": 1, "y": 1, "block_type": "stone"}],
            "removed": [{"map_id": 10, "x": 0, "y": 0}]}}})
        self.assertEqual(self.w.maps[9].tiles[(1, 1)], "stone")
        self.assertEqual(self.w.maps[10].tiles, {})

    def test_separate_reads_invalidate_version(self):
        self.w.apply_observation(load("complete_v10.json"))
        self.w.apply_entities({"tick": 3, "npcs": []})
        self.assertIsNone(self.w.snapshot_version)
        self.w.apply_observation(load("complete_v10.json"))
        self.w.apply_position({"map_id": 8, "x": 0, "y": 0})
        self.assertIsNone(self.w.snapshot_version)
        self.w.apply_observation(load("complete_v10.json"))
        self.w.apply_position({"map_id": 7, "x": 1, "y": 1})
        self.assertEqual(self.w.snapshot_version, 10)
        self.w.forget_position()
        self.assertIsNone(self.w.snapshot_version)


if __name__ == "__main__":
    unittest.main()
