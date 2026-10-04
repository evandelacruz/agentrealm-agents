"""A6: threat table from Damaged events."""

import unittest

from agentrealm_agent.threat import (
    UNMEASURED_DEFAULT,
    ThreatTable,
    absorb_damaged,
    type_key_for_entity,
    type_key_from_damaged,
)
from agentrealm_agent.world import Entity, WorldModel


class ThreatTableTest(unittest.TestCase):
    def test_default_before_any_hit(self):
        t = ThreatTable()
        self.assertEqual(t.damage_per_hit(("npc", "snotling")), UNMEASURED_DEFAULT)
        self.assertFalse(t.measured(("npc", "snotling")))

    def test_unmeasured_type_uses_worst_seen(self):
        t = ThreatTable()
        t.record(("npc", "snotling"), 1)
        self.assertEqual(t.damage_per_hit(("npc", "snotling")), 1)
        self.assertEqual(t.damage_per_hit(("npc", "gristlewick")), 1)
        t.record(("npc", "gristlewick"), 3)
        self.assertEqual(t.damage_per_hit(("npc", "snotling")), 1)
        self.assertEqual(t.damage_per_hit(("npc", "unknown")), 3)

    def test_keeps_max_per_type(self):
        t = ThreatTable()
        t.record(("npc", "rat"), 2)
        t.record(("npc", "rat"), 1)
        self.assertEqual(t.by_type[("npc", "rat")], 2)

    def test_npc_key_from_entities(self):
        ev = {"source_kind": "npc", "source_id": 301, "amount": 2}
        ents = [Entity("npc", 301, (1, 1), "rat")]
        self.assertEqual(type_key_from_damaged(ev, ents), ("npc", "rat"))

    def test_trap_without_supply_id(self):
        ev = {"source_kind": "trap", "amount": 5}
        self.assertEqual(type_key_from_damaged(ev, []), ("trap", "trap"))

    def test_entity_helper(self):
        self.assertEqual(type_key_for_entity(Entity("npc", 1, (0, 0), "pest")), ("npc", "pest"))

    def test_world_model_folds_damaged_into_threat(self):
        w = WorldModel(character_id=1)
        w.entities = [Entity("npc", 4, (2, 2), "gristlewick")]
        w.apply_events(
            [
                {
                    "tick": 11,
                    "events": [{"tick": 11, "kind": "Damaged", "source_kind": "npc", "source_id": 4, "amount": 3}],
                }
            ]
        )
        self.assertEqual(w.threat.damage_per_hit(("npc", "gristlewick")), 3)


if __name__ == "__main__":
    unittest.main()
