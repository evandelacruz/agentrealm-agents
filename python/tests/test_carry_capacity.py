"""A115: carry capacity is derived from what the server tells us at every
inventory sync, and the applied chest (which the server never serves) is
kept per character so a new run starts at its size. A157: every death resets
it to the blue chest, a death seen or one since the chest was saved."""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agentrealm_adk import config
from agentrealm_adk.config import CharacterConfig, Policy
from agentrealm_adk.item_table import DEFAULT_CARRY_CAPACITY
from agentrealm_adk.knowledge_base import KnowledgeBase
from agentrealm_adk.loot import (
    CARRIED_CHESTS_KEY,
    learn_chest_upgrade,
    learn_loot_rejection,
    load_carried_chest,
    save_carried_chest,
)
from agentrealm_agent.runner import Runner
from agentrealm_adk.world import WorldModel


def inventory(n_held: int) -> dict:
    return {
        "armed": {"id": 1, "supply_subtype_code": "pocket_knife"},
        "worn": {},
        "held": [{"id": 100 + i, "supply_subtype_code": "torch"} for i in range(n_held)],
        "chest": [],
    }


def sync(w: WorldModel, n_held: int) -> None:
    w.apply_observation({"complete": True, "snapshot": {"inventory": inventory(n_held)}})


class DerivedCapacityTest(unittest.TestCase):
    def test_a_new_character_carries_the_blue_chest(self):
        w = WorldModel(character_id=1)
        sync(w, 2)
        self.assertEqual(w.carry_capacity, DEFAULT_CARRY_CAPACITY)

    def test_an_applied_chest_sets_capacity_at_every_sync(self):
        w = WorldModel(character_id=1)
        learn_chest_upgrade(w, "middle_chest")
        self.assertEqual((w.carried_chest, w.carry_capacity), ("middle_chest", 30))
        sync(w, 4)
        self.assertEqual(w.carry_capacity, 30)

    def test_an_applied_chest_is_the_chest_whatever_was_recorded(self):
        # Applied means it made the chest bigger (Manual §11): a record above it was stale.
        w = WorldModel(character_id=1)
        learn_chest_upgrade(w, "red_chest")
        learn_chest_upgrade(w, "middle_chest")
        self.assertEqual((w.carried_chest, w.carry_capacity), ("middle_chest", 30))

    def test_a_supply_that_is_no_chest_changes_nothing(self):
        w = WorldModel(character_id=1)
        learn_chest_upgrade(w, "middle_chest")
        learn_chest_upgrade(w, "bronze_sword")
        self.assertEqual(w.carried_chest, "middle_chest")

    def test_capacity_is_never_below_what_the_inventory_carries(self):
        # The server let us carry 14: the pack holds at least 14, whatever was recorded.
        w = WorldModel(character_id=1)
        sync(w, 13)
        self.assertEqual(w.carry_capacity, 14)

    def test_a_full_refusal_bounds_the_next_syncs(self):
        w = WorldModel(character_id=1)
        sync(w, 5)
        learn_loot_rejection(w, {"verb": "Take", "supply_id": 40}, "carry_capacity_full")
        self.assertEqual(w.carry_capacity, 6)
        sync(w, 4)
        self.assertEqual(w.carry_capacity, 6)

    def test_a_full_refusal_below_the_recorded_chest_forgets_it(self):
        w = WorldModel(character_id=1)
        learn_chest_upgrade(w, "middle_chest")
        sync(w, 11)
        learn_loot_rejection(w, {"verb": "Take", "supply_id": 40}, "carry_capacity_full")
        self.assertIsNone(w.carried_chest)
        self.assertEqual(w.carry_capacity, 12)

    def test_a_refused_withdraw_of_many_bounds_by_what_did_not_fit(self):
        # All or nothing: 26 ids refused with 4 carried shows room for at most 29.
        w = WorldModel(character_id=1)
        learn_chest_upgrade(w, "middle_chest")
        sync(w, 3)
        withdraw = {"verb": "WithdrawFromChest", "chest_id": 50, "supply_ids": list(range(200, 226))}
        learn_loot_rejection(w, withdraw, "carry_capacity_full")
        self.assertEqual(w.full_at, 29)
        self.assertIsNone(w.carried_chest, "29 is below the middle chest's 30")
        self.assertEqual(w.carry_capacity, DEFAULT_CARRY_CAPACITY, "back to the blue chest, not pinned at 4")

    def test_a_refused_withdraw_that_fits_the_recorded_chest_keeps_it(self):
        w = WorldModel(character_id=1)
        learn_chest_upgrade(w, "middle_chest")
        sync(w, 9)
        withdraw = {"verb": "WithdrawFromChest", "chest_id": 50, "supply_ids": list(range(200, 230))}
        learn_loot_rejection(w, withdraw, "carry_capacity_full")
        self.assertEqual(w.carried_chest, "middle_chest")
        self.assertEqual(w.carry_capacity, 30)

    def test_a_refused_withdraw_of_everything_shows_no_room_for_one(self):
        w = WorldModel(character_id=1)
        sync(w, 7)
        learn_loot_rejection(w, {"verb": "WithdrawFromChest", "chest_id": 50}, "carry_capacity_full")
        self.assertEqual(w.carry_capacity, 8)

    def test_an_applied_chest_lifts_an_earlier_full_bound(self):
        w = WorldModel(character_id=1)
        sync(w, 9)
        learn_loot_rejection(w, {"verb": "Take", "supply_id": 40}, "carry_capacity_full")
        learn_chest_upgrade(w, "middle_chest")
        sync(w, 9)
        self.assertEqual(w.carry_capacity, 30)

    def test_a_death_resets_the_chest(self):
        w = WorldModel(character_id=1)
        learn_chest_upgrade(w, "middle_chest")
        w.apply_events([{"tick": 5, "events": [{"kind": "Died", "map_id": 1, "x": 2, "y": 3, "chest_id": 40}]}])
        self.assertIsNone(w.carried_chest)
        self.assertEqual(w.carry_capacity, DEFAULT_CARRY_CAPACITY)

    def test_a_served_death_chest_never_known_is_a_death_unseen(self):
        w = WorldModel(character_id=1)
        learn_chest_upgrade(w, "middle_chest")
        w.apply_observation({"complete": True, "snapshot": {"death_chest": {"chest_id": 40, "map_id": 1, "x": 2, "y": 3}}})
        self.assertIsNone(w.carried_chest)

    def test_a_served_death_chest_already_known_resets_nothing(self):
        w = WorldModel(character_id=1)
        w.remember_death_chest(40, 1, (2, 3))
        learn_chest_upgrade(w, "middle_chest")
        w.apply_observation({"complete": True, "snapshot": {"death_chest": {"chest_id": 40, "map_id": 1, "x": 2, "y": 3}}})
        self.assertEqual(w.carried_chest, "middle_chest")

    def test_respawn_brings_the_blue_chest(self):
        w = WorldModel(character_id=1)
        learn_chest_upgrade(w, "middle_chest")
        w.apply_events([{"tick": 5, "events": [{"kind": "Respawned", "map_id": 1, "x": 2, "y": 3}]}])
        self.assertIsNone(w.carried_chest)
        self.assertEqual(w.carry_capacity, DEFAULT_CARRY_CAPACITY)


class CarriedChestRecordTest(unittest.TestCase):
    def test_a_new_run_starts_at_the_applied_chests_size(self):
        kb = KnowledgeBase.empty("sandbox")
        before = WorldModel(character_id=7)
        learn_chest_upgrade(before, "middle_chest")
        before.lives = 9
        save_carried_chest(kb, before)
        self.assertEqual(kb.extra[CARRIED_CHESTS_KEY], {"7": {"code": "middle_chest", "lives": 9}})

        after = WorldModel(character_id=7)
        load_carried_chest(kb, after)
        sync(after, 3)
        self.assertEqual(after.carry_capacity, 30)

    def saved(self, lives: int) -> KnowledgeBase:
        kb = KnowledgeBase.empty("sandbox")
        kb.extra[CARRIED_CHESTS_KEY] = {"7": {"code": "middle_chest", "lives": lives}}
        return kb

    def test_fewer_lives_than_saved_is_a_death_since(self):
        w = WorldModel(character_id=7)
        load_carried_chest(self.saved(9), w)
        self.assertEqual(w.carry_capacity, 30, "nothing read yet: the record stands")
        w.apply_self({"lives": 8, "alive": True})
        self.assertIsNone(w.carried_chest)
        self.assertEqual(w.carry_capacity, DEFAULT_CARRY_CAPACITY)

    def test_fewer_lives_in_the_first_snapshot_is_a_death_since(self):
        w = WorldModel(character_id=7)
        load_carried_chest(self.saved(9), w)
        w.apply_observation({"complete": True, "snapshot": {"lives": 8, "inventory": inventory(2)}})
        self.assertIsNone(w.carried_chest)
        self.assertEqual(w.carry_capacity, DEFAULT_CARRY_CAPACITY)

    def test_the_lives_saved_keep_the_chest(self):
        w = WorldModel(character_id=7)
        load_carried_chest(self.saved(9), w)
        w.apply_self({"lives": 9, "alive": True})
        self.assertEqual(w.carried_chest, "middle_chest")
        self.assertIsNone(w.chest_lives, "checked once, at the first lives read")
        w.apply_self({"lives": 8, "alive": True})  # a later drop with no Died is not this check's
        self.assertEqual(w.carried_chest, "middle_chest")

    def test_lives_unread_save_the_lives_loaded(self):
        kb = self.saved(9)
        w = WorldModel(character_id=7)
        load_carried_chest(kb, w)
        save_carried_chest(kb, w)
        self.assertEqual(kb.extra[CARRIED_CHESTS_KEY], {"7": {"code": "middle_chest", "lives": 9}})

    def test_a_record_saved_before_a157_still_loads(self):
        kb = KnowledgeBase.empty("sandbox")
        kb.extra[CARRIED_CHESTS_KEY] = {"7": "middle_chest"}
        w = WorldModel(character_id=7)
        load_carried_chest(kb, w)
        self.assertEqual((w.carried_chest, w.chest_lives), ("middle_chest", None))

    def test_the_record_is_per_character(self):
        kb = KnowledgeBase.empty("sandbox")
        kb.extra[CARRIED_CHESTS_KEY] = {"7": "middle_chest"}
        other = WorldModel(character_id=8)
        load_carried_chest(kb, other)
        self.assertEqual(other.carry_capacity, DEFAULT_CARRY_CAPACITY)

    def test_the_blue_chest_removes_the_record(self):
        kb = KnowledgeBase.empty("sandbox")
        kb.extra[CARRIED_CHESTS_KEY] = {"7": "middle_chest"}
        w = WorldModel(character_id=7)
        save_carried_chest(kb, w)
        self.assertNotIn(CARRIED_CHESTS_KEY, kb.extra)

    def test_a_record_naming_no_chest_is_ignored(self):
        kb = KnowledgeBase.empty("sandbox")
        kb.extra[CARRIED_CHESTS_KEY] = {"7": "torch"}
        w = WorldModel(character_id=7)
        load_carried_chest(kb, w)
        self.assertIsNone(w.carried_chest)

    def test_the_runner_starts_from_the_record(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(config, "STATE_DIR", Path(tmp.name)):
            kb = KnowledgeBase.empty("sandbox")
            kb.extra[CARRIED_CHESTS_KEY] = {"1": "middle_chest"}
            cfg = CharacterConfig("T", "sandbox", Policy(goals=[]), Path("t.toml"))
            r = Runner(cfg, object(), 1, threading.Event(), out=lambda _: None, knowledge=kb)
            r.trace.close()
        self.assertEqual(r.world.carry_capacity, 30)


if __name__ == "__main__":
    unittest.main()
