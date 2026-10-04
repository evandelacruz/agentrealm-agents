"""Item table learning (A18)."""

import unittest
from pathlib import Path

from agentrealm_agent import item_table as it
from agentrealm_agent.runner import Runner
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.config import CharacterConfig, Policy


class ItemTableMergeTest(unittest.TestCase):
    def test_merge_keeps_max_weapon_damage_and_damage_taken(self):
        items: dict = {}
        it.merge_item(items, "bronze_sword", weapon_damage=2)
        it.merge_item(items, "bronze_sword", weapon_damage=1)
        it.merge_item(items, "bronze_mail", damage_taken=3)
        it.merge_item(items, "bronze_mail", damage_taken=5)
        self.assertEqual(items["bronze_sword"]["weapon_damage"], 2)
        self.assertEqual(items["bronze_mail"]["damage_taken"], 5)

    def test_capabilities_union(self):
        items: dict = {}
        it.merge_item(items, "bronze_sword", capabilities=["cut"])
        it.merge_item(items, "bronze_sword", capabilities=["chop"])
        self.assertEqual(items["bronze_sword"]["capabilities"], ["chop", "cut"])

    def test_supply_entity_price_and_capabilities(self):
        items: dict = {}
        it.absorb_supply_entry(
            items,
            {
                "id": 1,
                "supply_subtype_code": "torch",
                "gem_price": 10,
                "capabilities": ["light"],
            },
        )
        self.assertEqual(items["torch"], {"gem_price": 10, "capabilities": ["light"]})


class RunnerItemLearningTest(unittest.TestCase):
    def _runner(self, kb: KnowledgeBase | None = None) -> Runner:
        cfg = CharacterConfig("t", "default", "test", "sandbox", Policy(goals=["hold"]), Path("t.toml"))
        return Runner(cfg, client=object(), character_id=1, stop=__import__("threading").Event(), knowledge=kb)

    def test_tick_folds_inventory_and_combat_into_kb(self):
        kb = KnowledgeBase.empty("sandbox")
        r = self._runner(kb)
        r.world.armed_code = "bronze_sword"
        r.world.worn_codes = {"body": "bronze_mail"}
        obs = {
            "version": 3,
            "delta": {
                "inventory": {
                    "armed": {"id": 9, "supply_subtype_code": "bronze_sword", "capabilities": ["cut", "chop"]},
                    "worn": {"body": {"id": 10, "supply_subtype_code": "bronze_mail"}},
                    "gems": 5,
                    "held": [],
                    "chest": [],
                },
                "entities": {
                    "supplies": {
                        "added": [
                            {"id": 77, "x": 1, "y": 1, "supply_subtype_code": "small_potion", "gem_price": 10}
                        ]
                    }
                },
            },
        }
        events = [
            {"kind": "NPCDamaged", "amount": 2, "npc_id": 1, "map_id": 1, "x": 0, "y": 0},
            {"kind": "Damaged", "source_kind": "npc", "source_id": 4, "amount": 4},
        ]
        r._learn_items_from_tick(events, obs)
        self.assertEqual(kb.items["bronze_sword"]["weapon_damage"], 2)
        self.assertEqual(kb.items["bronze_sword"]["capabilities"], ["chop", "cut"])
        self.assertEqual(kb.items["bronze_mail"]["damage_taken"], 4)
        self.assertEqual(kb.items["small_potion"]["gem_price"], 10)

    def test_self_read_records_attack_range_for_armed_weapon(self):
        kb = KnowledgeBase.empty("sandbox")
        r = self._runner(kb)
        r.world.armed_code = "pocket_knife"
        r.world.attack_range = 1
        r._learn_items_from_self()
        self.assertEqual(kb.items["pocket_knife"]["attack_range"], 1)


if __name__ == "__main__":
    unittest.main()
