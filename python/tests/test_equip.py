"""A19: Equip scoring and state."""

import random
import unittest
from unittest import mock

from agentrealm_agent.config import Policy
from agentrealm_agent.directives import default_directives
from agentrealm_agent.equip import (
    armor_score,
    best_equip_upgrade,
    infer_wear_slot,
    weapon_score,
)
from agentrealm_agent.item_table import InventorySupply
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.memory import Memory
from agentrealm_agent.states import PlayContext, dispatch
from agentrealm_agent.threat import ThreatTable
from agentrealm_agent.world import WorldModel


def ctx(w: WorldModel, kb: KnowledgeBase | None = None) -> PlayContext:
    return PlayContext(
        Memory(),
        Policy(kind="scripted", goals=["hold"]),
        random.Random(0),
        directives=default_directives(),
        knowledge=kb or KnowledgeBase.empty("sandbox"),
    )


class EquipScoringTest(unittest.TestCase):
    def test_infer_wear_slots(self):
        self.assertEqual(infer_wear_slot("bronze_helm"), "head")
        self.assertEqual(infer_wear_slot("bronze_mail"), "body")
        self.assertIsNone(infer_wear_slot("bronze_sword"))
        self.assertIsNone(infer_wear_slot("small_potion"))
        self.assertIsNone(infer_wear_slot("middle_chest"))

    def test_armor_score_weights_damage_saved(self):
        items = {"bronze_mail": {"damage_saved": {"rat": 3}}}
        threat = ThreatTable()
        threat.record(("npc", "rat"), 5)
        self.assertEqual(armor_score("bronze_mail", items, threat), 15)
        self.assertEqual(armor_score("leather_cap", items, threat), 0)

    def test_weapon_score_uses_gem_price_without_hits(self):
        items = {"bronze_sword": {"gem_price": 15}, "pocket_knife": {}}
        threat = ThreatTable()
        self.assertEqual(weapon_score("bronze_sword", items, threat), 15)
        self.assertEqual(weapon_score("pocket_knife", items, threat), 0)

    def test_best_upgrade_picks_weapon(self):
        w = WorldModel(character_id=1, map_id=7, pos=(0, 0))
        w.armed_code = "pocket_knife"
        w.held_supplies = [InventorySupply(5, "bronze_sword")]
        items = {"bronze_sword": {"gem_price": 15}}
        up = best_equip_upgrade(w, items, w.threat)
        self.assertIsNotNone(up)
        assert up is not None
        self.assertEqual(up.intents, ({"verb": "Arm", "supply_id": 5},))
        self.assertIn("bronze_sword", up.reason)

    def test_best_upgrade_swaps_body_armor(self):
        w = WorldModel(character_id=1, map_id=7, pos=(0, 0))
        w.worn_codes = {"body": "leather_vest"}
        w.held_supplies = [InventorySupply(8, "bronze_mail")]
        items = {
            "leather_vest": {"gem_price": 5},
            "bronze_mail": {"gem_price": 20},
        }
        up = best_equip_upgrade(w, items, w.threat)
        self.assertIsNotNone(up)
        assert up is not None
        self.assertEqual(
            up.intents,
            (
                {"verb": "Remove", "slot": "body"},
                {"verb": "Wear", "supply_id": 8},
            ),
        )


class EquipDispatchTest(unittest.TestCase):
    def test_equip_before_loot(self):
        w = WorldModel(character_id=1, map_id=7, pos=(0, 0), perception=5)
        w.armed_code = "pocket_knife"
        w.held_supplies = [InventorySupply(5, "bronze_sword")]
        kb = KnowledgeBase.empty("sandbox")
        kb.items["bronze_sword"] = {"gem_price": 15}
        out = dispatch(w, ctx(w, kb))
        self.assertEqual(out.state, "Equip")
        self.assertEqual(out.intents, [{"verb": "Arm", "supply_id": 5}])

    def test_skips_potion_for_heal(self):
        w = WorldModel(character_id=1, map_id=7, pos=(0, 0))
        w.held_supplies = [InventorySupply(3, "small_potion")]
        kb = KnowledgeBase.empty("sandbox")
        kb.items["small_potion"] = {"gem_price": 10}
        out = dispatch(w, ctx(w, kb))
        self.assertNotEqual(out.state, "Equip")

    def test_equip_falls_through_when_upgrade_gone(self):
        import agentrealm_agent.states.equip as equip_mod

        w = WorldModel(character_id=1, map_id=7, pos=(0, 0), perception=3)
        for x in range(3):
            w.view.tiles[(x, 0)] = "dirt"
        w.armed_code = "pocket_knife"
        w.held_supplies = [InventorySupply(5, "bronze_sword")]
        kb = KnowledgeBase.empty("sandbox")
        kb.items["bronze_sword"] = {"gem_price": 15}
        with mock.patch.object(equip_mod, "best_equip_upgrade", return_value=None):
            out = dispatch(w, ctx(w, kb))
        self.assertIn("Equip: nothing to equip", out.yielded)
        self.assertNotEqual(out.state, "Equip")


if __name__ == "__main__":
    unittest.main()
