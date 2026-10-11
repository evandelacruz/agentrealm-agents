"""Game update of 2026-10-11, item capabilities (A96).

Worn armor's defense lowers both the chance and the size of a hostile's hit,
and armor in hand protects nothing. ``Damaged.amount`` is already net of the
armor worn, so the threat table files it gross and pricing takes armor off
once. The Supplies reference's ``blue_chest`` is a consumable chest of the
starting size.
"""

from __future__ import annotations

import random
import unittest

from agentrealm_agent import supplies
from agentrealm_agent.config import Policy
from agentrealm_agent.directives import PARAM_DEFAULTS
from agentrealm_agent.engagement import decide
from agentrealm_agent.equip import best_equip_upgrade, is_consumable
from agentrealm_agent.item_table import DEFAULT_CARRY_CAPACITY, InventorySupply
from agentrealm_agent.loot import learn_chest_upgrade
from agentrealm_agent.memory import Memory
from agentrealm_agent.survival import (
    health_floor,
    hit_damage,
    hostile_swing_damage,
    win_ratio,
    worn_defense,
)
from agentrealm_agent.threat import UNMEASURED_DEFAULT, ThreatTable, absorb_damaged
from agentrealm_agent.world import Entity, WorldModel

CODE = "fake_biter"
KEY = ("npc", CODE)


def world(health=10) -> WorldModel:
    w = WorldModel(character_id=1, map_id=1, pos=(10, 10), perception=8, health=health, max_health=10, lives=9)
    w.hostile_types.add(KEY)
    return w


def biter(npc_id=7) -> Entity:
    return Entity("npc", npc_id, (11, 10), code=CODE)


class WornArmorTest(unittest.TestCase):
    def test_only_worn_armor_adds_defense(self):
        w = world()
        self.assertEqual(worn_defense(w), 0)
        w.armed_code = "bronze_mail"  # armor in hand protects nothing
        self.assertEqual(worn_defense(w), 0)
        w.worn_codes = {"body": "bronze_mail", "accessory": "goggles"}
        self.assertEqual(worn_defense(w), supplies.armor_defense("bronze_mail"))
        w.worn_codes["head"] = "iron_helm"
        self.assertEqual(worn_defense(w), 2)

    def test_armor_lowers_the_chance_and_size_of_a_hit(self):
        bare = hostile_swing_damage(UNMEASURED_DEFAULT)
        mailed = hostile_swing_damage(UNMEASURED_DEFAULT, armor=1)
        self.assertAlmostEqual(bare, 0.65 * 2)
        self.assertAlmostEqual(mailed, 0.60 * 1)

    def test_a_landed_hit_is_never_priced_as_free(self):
        t = ThreatTable()
        self.assertEqual(hit_damage(t, KEY, armor=5), 1)
        self.assertEqual(hit_damage(t, KEY, armor=1), UNMEASURED_DEFAULT - 1)

    def test_the_win_estimate_counts_the_armor_worn(self):
        w, policy = world(health=4), Policy(kind="scripted", goals=["explore"], on_hostile="fight", hostile=["npc"])
        group = [biter()]
        w.entities = group
        bare = win_ratio(4, group, w.threat, "pocket_knife")
        self.assertGreater(win_ratio(4, group, w.threat, "pocket_knife", armor=1), bare)
        w.worn_codes = {"body": "bronze_mail"}
        params = dict(PARAM_DEFAULTS)
        mailed = decide(w, policy, params, frozenset({("npc", 7)}), False)
        self.assertGreater(mailed.ratio, bare)

    def test_retreat_floor_is_net_of_worn_armor(self):
        w = world()
        params = dict(PARAM_DEFAULTS)
        bare = health_floor(w, params, [biter()])
        w.worn_codes = {"body": "bronze_mail"}
        self.assertLess(health_floor(w, params, [biter()]), bare)


class GrossHitTest(unittest.TestCase):
    def test_a_hit_through_armor_is_filed_gross(self):
        t = ThreatTable()
        e = biter()
        absorb_damaged(t, {"kind": "Damaged", "amount": 2, "source_kind": "npc", "source_id": 7}, [e], armor=1)
        self.assertEqual(t.damage_per_hit(KEY), 3)
        # Priced through the same armor, it is the hit that landed: armor comes off once.
        self.assertEqual(hit_damage(t, KEY, armor=1), 2)

    def test_a_hit_armor_absorbed_files_no_number(self):
        t = ThreatTable()
        absorb_damaged(t, {"kind": "Damaged", "amount": 0, "source_kind": "npc", "source_id": 7}, [biter()], armor=1)
        self.assertFalse(t.measured(KEY))

    def test_the_world_files_hits_with_the_armor_it_wears(self):
        w = world()
        w.entities = [biter()]
        w.worn_codes = {"body": "bronze_mail"}
        w.learn_threat([{"kind": "Damaged", "amount": 1, "source_kind": "npc", "source_id": 7, "tick": 1}], [])
        self.assertEqual(w.threat.damage_per_hit(KEY), 2)


class ChestTest(unittest.TestCase):
    def test_blue_chest_is_a_consumable_chest_of_the_starting_size(self):
        row = supplies.row("blue_chest")
        self.assertIsNotNone(row)
        self.assertEqual(row.supply_class, "consumable")
        self.assertEqual(supplies.chest_capacity("blue_chest"), DEFAULT_CARRY_CAPACITY)
        self.assertTrue(is_consumable("blue_chest"))
        self.assertEqual(supplies.what_it_does("blue_chest")["chest_capacity"], 10)

    def test_capacity_comes_from_the_reference_and_only_rises(self):
        w = world()
        learn_chest_upgrade(w, "blue_chest")
        self.assertEqual(w.carry_capacity, DEFAULT_CARRY_CAPACITY)
        learn_chest_upgrade(w, "red_chest")
        self.assertEqual(w.carry_capacity, 50)
        learn_chest_upgrade(w, "middle_chest")
        self.assertEqual(w.carry_capacity, 50)
        learn_chest_upgrade(w, "bronze_sword")
        self.assertEqual(w.carry_capacity, 50)

    def test_equip_never_tries_to_wear_a_chest(self):
        w = world()
        w.held_supplies = [InventorySupply(3, "blue_chest"), InventorySupply(4, "middle_chest")]
        self.assertIsNone(best_equip_upgrade(w, {}, w.threat, Memory()))


class ParseTest(unittest.TestCase):
    def test_chest_capacity_is_read_and_bad_values_dropped(self):
        rows = supplies.parse([
            {"code": "fake_chest", "class": "consumable", "use_effects": ["chest"], "chest_capacity": 20},
            {"code": "bad_chest", "class": "consumable", "use_effects": ["chest"], "chest_capacity": "lots"},
        ])
        self.assertEqual(rows["fake_chest"].chest_capacity, 20)
        self.assertIsNone(rows["bad_chest"].chest_capacity)


if __name__ == "__main__":
    unittest.main()
