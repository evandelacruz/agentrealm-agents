"""M6: health from tick observations and deltas (fixture payloads, no server)."""

import unittest

from agentrealm_agent.world import WorldModel


class HealthObservationTest(unittest.TestCase):
    def test_complete_snapshot_sets_health(self):
        w = WorldModel(character_id=1)
        w.apply_observation({
            "complete": True,
            "snapshot": {"health": 10, "max_health": 10, "entities": {"chests": []}},
        })
        self.assertEqual(w.health, 10)
        self.assertEqual(w.max_health, 10)

    def test_delta_updates_health_after_damage(self):
        w = WorldModel(character_id=1, health=10, max_health=10)
        w.apply_observation({"version": "41", "delta": {"health": 7}})
        self.assertEqual(w.health, 7)
        self.assertEqual(w.max_health, 10)

    def test_delta_can_raise_max_health(self):
        w = WorldModel(character_id=1, health=10, max_health=10)
        w.apply_observation({"version": "99", "delta": {"health": 15, "max_health": 15}})
        self.assertEqual((w.health, w.max_health), (15, 15))

    def test_delta_null_clears_vitals_when_asleep(self):
        w = WorldModel(character_id=1, health=3, max_health=10)
        w.apply_observation({"version": "50", "delta": {"health": None, "max_health": None}})
        self.assertIsNone(w.health)
        self.assertIsNone(w.max_health)

    def test_unchanged_leaves_health(self):
        w = WorldModel(character_id=1, health=6, max_health=10)
        w.apply_observation({"version": "41", "unchanged": True})
        self.assertEqual(w.health, 6)

    def test_downed_complete_snapshot(self):
        w = WorldModel(character_id=1, health=10, max_health=10)
        w.apply_observation({
            "complete": True,
            "snapshot": {"health": 0, "max_health": 10, "alive": False},
        })
        self.assertEqual(w.health, 0)

    def test_respawn_complete_snapshot_refills(self):
        w = WorldModel(character_id=1, health=0, max_health=10)
        w.apply_observation({
            "complete": True,
            "snapshot": {"health": 10, "max_health": 10, "alive": True},
        })
        self.assertEqual(w.health, 10)


if __name__ == "__main__":
    unittest.main()
