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


def hit(kind, sid, amount):
    ev = {"kind": "Damaged", "source_kind": kind, "amount": amount}
    if sid is not None:
        ev["source_id"] = sid
    return ev


class ThreatTableTest(unittest.TestCase):
    def test_default_before_any_hit(self):
        t = ThreatTable()
        self.assertEqual(t.damage_per_hit(("npc", "snotling")), UNMEASURED_DEFAULT)
        self.assertEqual(t.damage_per_hit(None), UNMEASURED_DEFAULT)
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
        ents = [Entity("npc", 301, (1, 1), "rat")]
        self.assertEqual(type_key_from_damaged(hit("npc", 301, 2), ents), ("npc", "rat"))

    def test_lookup_matches_kind_as_well_as_id(self):
        ents = [Entity("supply", 7, (0, 0), "spike_trap"), Entity("character", 7, (1, 0), "rogue"),
                Entity("npc", 7, (2, 0), "rat")]
        self.assertEqual(type_key_from_damaged(hit("npc", 7, 1), ents), ("npc", "rat"))
        self.assertEqual(type_key_from_damaged(hit("character", 7, 1), ents), ("character", "rogue"))
        self.assertEqual(type_key_from_damaged(hit("trap", 7, 1), ents), ("trap", "spike_trap"))
        self.assertIsNone(type_key_from_damaged(hit("npc", 7, 1), [Entity("supply", 7, (0, 0), "spike_trap")]))

    def test_unknown_source_is_not_recorded(self):
        t = ThreatTable()
        self.assertIsNone(absorb_damaged(t, hit("npc", 99, 9), [Entity("npc", 1, (0, 0), "rat")]))
        self.assertIsNone(absorb_damaged(t, hit("npc", None, 9), []))
        self.assertIsNone(absorb_damaged(t, hit("npc", 1, 9), [Entity("npc", 1, (0, 0), "")]))
        self.assertIsNone(absorb_damaged(t, hit("mystery", 1, 9), [Entity("npc", 1, (0, 0), "rat")]))
        self.assertEqual(t.by_type, {})
        self.assertEqual(t.damage_per_hit(("npc", "rat")), UNMEASURED_DEFAULT)

    def test_earlier_view_resolves_a_source_that_left(self):
        earlier = [Entity("npc", 4, (2, 2), "gristlewick")]
        self.assertEqual(type_key_from_damaged(hit("npc", 4, 3), [], earlier), ("npc", "gristlewick"))

    def test_trap_and_occupy_stay_out_of_the_hostile_default(self):
        t = ThreatTable()
        ents = [Entity("supply", 5, (0, 0), "spike_trap")]
        self.assertEqual(absorb_damaged(t, hit("trap", 5, 5), ents), ("trap", "spike_trap"))
        self.assertEqual(absorb_damaged(t, hit("occupy", None, 4), ents), ("occupy", "occupy"))
        self.assertEqual(t.damage_per_hit(("trap", "spike_trap")), 5)
        self.assertEqual(t.damage_per_hit(("npc", "rat")), UNMEASURED_DEFAULT)
        t.record(("npc", "rat"), 1)
        self.assertEqual(t.damage_per_hit(("npc", "pest")), 1)

    def test_trap_without_a_perceived_supply_is_not_recorded(self):
        self.assertIsNone(type_key_from_damaged(hit("trap", None, 5), []))
        self.assertIsNone(type_key_from_damaged(hit("trap", 5, 5), []))

    def test_non_positive_or_non_numeric_amount_is_ignored(self):
        ents = [Entity("npc", 1, (0, 0), "rat")]
        for amount in (0, -2, None, "x", True, [1]):
            with self.subTest(amount=amount):
                t = ThreatTable()
                self.assertIsNone(absorb_damaged(t, hit("npc", 1, amount), ents))
                self.assertEqual(t.by_type, {})

    def test_entity_helper(self):
        self.assertEqual(type_key_for_entity(Entity("npc", 1, (0, 0), "pest")), ("npc", "pest"))
        self.assertIsNone(type_key_for_entity(Entity("npc", 1, (0, 0), "")))
        self.assertIsNone(type_key_for_entity(Entity("supply", 1, (0, 0), "spike_trap")))

    def test_world_model_learns_threat_after_events(self):
        w = WorldModel(character_id=1)
        w.entities = [Entity("npc", 4, (2, 2), "gristlewick")]
        events = w.apply_events([{"tick": 11, "events": [{"tick": 11, **hit("npc", 4, 3)}]}])
        w.learn_threat(events, [])
        self.assertEqual(w.threat.damage_per_hit(("npc", "gristlewick")), 3)

    def test_recent_damage_skips_a_non_numeric_amount(self):
        w = WorldModel(character_id=1)
        w.apply_events([{"tick": 11, "events": [hit("npc", 4, "x"), hit("npc", 4, 2)]}])
        self.assertEqual(w.recent_damage, [(11, 2)])


if __name__ == "__main__":
    unittest.main()
