"""M6: health from complete tick snapshots (fixture payloads, no server)."""

import unittest

from agentrealm_agent.world import WorldModel


def complete(**snap):
    return {"complete": True, "snapshot": snap}


class HealthObservationTest(unittest.TestCase):
    def test_complete_snapshot_sets_health(self):
        w = WorldModel(character_id=1)
        w.apply_observation(complete(health=10, max_health=10, entities={"chests": []}))
        self.assertEqual((w.health, w.max_health), (10, 10))

    def test_damage_then_refill(self):
        w = WorldModel(character_id=1)
        w.apply_observation(complete(health=7, max_health=10))
        self.assertEqual(w.health, 7)
        w.apply_observation(complete(health=0, max_health=10))
        self.assertEqual(w.health, 0)
        w.apply_observation(complete(health=15, max_health=15))
        self.assertEqual((w.health, w.max_health), (15, 15))

    def test_complete_snapshot_without_health_clears_it(self):
        w = WorldModel(character_id=1, health=3, max_health=10)
        w.apply_observation(complete(entities={"chests": []}))
        self.assertIsNone(w.health)
        self.assertIsNone(w.max_health)

    def test_malformed_health_reads_as_unknown(self):
        w = WorldModel(character_id=1, health=6, max_health=10)
        w.apply_observation(complete(health="lots", max_health=[10]))
        self.assertIsNone(w.health)
        self.assertIsNone(w.max_health)

    def test_non_complete_observation_leaves_health(self):
        w = WorldModel(character_id=1, health=6, max_health=10)
        w.apply_observation({"version": "41", "delta": {"health": 2}})
        w.apply_observation(None)
        self.assertEqual((w.health, w.max_health), (6, 10))


if __name__ == "__main__":
    unittest.main()
