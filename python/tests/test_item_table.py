"""Item table learning (A18)."""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agentrealm_agent import config, item_table as it
from agentrealm_agent.brain import Decision
from agentrealm_agent.config import CharacterConfig, Policy
from agentrealm_agent.healing import absorb_heal_pending
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.runner import Runner
from agentrealm_agent.world import Entity, WorldModel

OUT_OF_RANGE = {
    "outcome": "rejected",
    "rejection": {
        "category": "range",
        "code": "target_out_of_range",
        "retryability": "precondition",
        "attack_range": 2,
        "distance": 3,
    },
}


class MergeTest(unittest.TestCase):
    def test_capability_is_filed_once_and_sorted(self):
        items: dict = {"iron_axe": {"gem_price": 9}}
        it.merge_capability(items, "iron_axe", "smash")
        it.merge_capability(items, "iron_axe", "chop")
        it.merge_capability(items, "iron_axe", "chop")
        it.merge_capability(items, None, "cut")
        self.assertEqual(items, {"iron_axe": {"gem_price": 9, "capabilities": ["chop", "smash"]}})

    def test_latest_value_overwrites(self):
        items: dict = {}
        it.merge_item(items, "potion", gem_price=5)
        it.merge_item(items, "potion", gem_price=7)
        it.merge_item(items, "bronze_sword", attack_range=1)
        it.merge_item(items, "bronze_sword", attack_range=2)
        self.assertEqual(items, {"potion": {"gem_price": 7}, "bronze_sword": {"attack_range": 2}})

    def test_heal_keeps_max_and_pickup_flag(self):
        items: dict = {}
        it.merge_heal(items, "apple", 3, on_pickup=True)
        it.merge_heal(items, "apple", 5, on_pickup=True)
        it.merge_heal(items, "berry", 0, on_pickup=False)
        self.assertEqual(
            items,
            {"apple": {"heal_amount": 5, "heal_on_pickup": True}, "berry": {"heal_on_pickup": False}},
        )

    def test_invalid_values_are_ignored_and_make_no_row(self):
        items: dict = {}
        for bad in (0, -1, True, "x", None, 2.5e400):
            it.merge_item(items, "potion", gem_price=bad, attack_range=bad)
        it.merge_item(items, None, gem_price=5)
        it.merge_item(items, "", gem_price=5)
        self.assertEqual(items, {})

    def test_unknown_facts_are_not_stored(self):
        items: dict = {}
        it.merge_item(items, "bronze_sword", weapon_damage=3, damage_taken=2, capabilities=["cut"], attack_range=1)
        self.assertEqual(items, {"bronze_sword": {"attack_range": 1}})

    def test_weapon_hit_keeps_the_max_per_npc_type(self):
        items: dict = {"bronze_sword": {"attack_range": 1}}
        for npc_type, amount in (("rat", 3), ("rat", 1), ("rat", 5), ("wolf", 2)):
            it.merge_weapon_hit(items, "bronze_sword", npc_type, amount)
        self.assertEqual(items, {"bronze_sword": {"attack_range": 1, "weapon_damage": {"rat": 5, "wolf": 2}}})

    def test_weapon_hit_ignores_invalid_input(self):
        items: dict = {}
        for code, npc_type, amount in (
            (None, "rat", 3),
            ("bronze_sword", "", 3),
            ("bronze_sword", "rat", None),
            ("bronze_sword", "rat", "four"),
            ("bronze_sword", "rat", 0),
            ("bronze_sword", "rat", True),
        ):
            it.merge_weapon_hit(items, code, npc_type, amount)
        self.assertEqual(items, {})

    def test_free_supply_makes_no_row(self):
        items: dict = {}
        it.absorb_supply_entry(items, {"id": 1, "x": 0, "y": 0, "supply_subtype_code": "apple"})
        self.assertEqual(items, {})

    def test_supply_read_does_not_store_capabilities_until_served(self):
        # Entity reads serve gem_price today, not capability tags (PLAN.md Server gaps, A54).
        items: dict = {}
        it.absorb_supply_entry(
            items,
            {"id": 1, "supply_subtype_code": "bronze_sword", "gem_price": 5, "capabilities": ["cut", "chop"]},
        )
        self.assertEqual(items, {"bronze_sword": {"gem_price": 5}})


class EntitiesTest(unittest.TestCase):
    def test_entity_read_list(self):
        items: dict = {}
        it.absorb_entities_payload(
            items,
            {"supplies": [{"id": 12, "x": 152, "y": 151, "supply_subtype_code": "potion", "gem_price": 5}]},
        )
        self.assertEqual(items, {"potion": {"gem_price": 5}})

    def test_delta_patch_added_and_changed_not_removed(self):
        items: dict = {}
        it.absorb_entities_payload(
            items,
            {
                "supplies": {
                    "added": [{"id": 1, "supply_subtype_code": "potion", "gem_price": 5}],
                    "changed": [{"id": 2, "supply_subtype_code": "mallet", "gem_price": 20}],
                    "removed": [3],
                }
            },
        )
        self.assertEqual(items, {"potion": {"gem_price": 5}, "mallet": {"gem_price": 20}})


def _npc_damaged(tick=11, amount=4, x=3, y=4, map_id=1, npc_id=9):
    return {"tick": tick, "kind": "NPCDamaged", "npc_id": npc_id, "amount": amount, "map_id": map_id, "x": x, "y": y}


class WornDamageTest(unittest.TestCase):
    RAT = Entity("npc", 4, (2, 1), "rat")

    def hit(self, amount, tick=11):
        return {"tick": tick, "kind": "Damaged", "source_kind": "npc", "source_id": 4, "amount": amount}

    def test_one_worn_slot_records_damage_taken(self):
        items: dict = {}
        it.absorb_damaged_worn(items, [self.hit(3)], {"body": "bronze_mail"}, None, [self.RAT])
        self.assertEqual(items, {"bronze_mail": {"damage_taken": {"rat": 3}}})

    def test_two_worn_slots_record_nothing(self):
        items: dict = {}
        worn = {"body": "bronze_mail", "head": "bronze_helm"}
        it.absorb_damaged_worn(items, [self.hit(3)], worn, None, [self.RAT])
        self.assertEqual(items, {})

    def test_bare_hit_without_removed_item_records_nothing(self):
        items: dict = {"bronze_mail": {"damage_taken": {"rat": 2}}}
        it.absorb_damaged_worn(items, [self.hit(5)], {}, None, [self.RAT])
        self.assertEqual(items, {"bronze_mail": {"damage_taken": {"rat": 2}}})

    def test_bare_hit_goes_only_to_removed_item(self):
        items: dict = {"bronze_mail": {"damage_taken": {"rat": 2}}, "bronze_helm": {"damage_taken": {"rat": 1}}}
        it.absorb_damaged_worn(items, [self.hit(5)], {}, "bronze_mail", [self.RAT])
        self.assertEqual(
            items,
            {
                "bronze_mail": {"damage_taken": {"rat": 2}, "damage_without": {"rat": 5}, "damage_saved": {"rat": 3}},
                "bronze_helm": {"damage_taken": {"rat": 1}},
            },
        )

    def test_damage_saved_follows_both_maxes(self):
        items: dict = {}
        mail = {"body": "bronze_mail"}
        it.absorb_damaged_worn(items, [self.hit(2)], mail, None, [self.RAT])
        it.absorb_damaged_worn(items, [self.hit(6)], {}, "bronze_mail", [self.RAT])
        self.assertEqual(items["bronze_mail"]["damage_saved"], {"rat": 4})
        # A larger hit taken while worn shrinks the gap; the saving never stays stale.
        it.absorb_damaged_worn(items, [self.hit(5)], mail, None, [self.RAT])
        self.assertEqual(items["bronze_mail"]["damage_saved"], {"rat": 1})
        # No positive gap left: the field goes rather than keeping an old number.
        it.absorb_damaged_worn(items, [self.hit(7)], mail, None, [self.RAT])
        self.assertNotIn("damage_saved", items["bronze_mail"])
        it.absorb_damaged_worn(items, [self.hit(9)], {}, "bronze_mail", [self.RAT])
        self.assertEqual(items["bronze_mail"]["damage_saved"], {"rat": 2})


class WeaponDamageTest(unittest.TestCase):
    USE = it.AppliedUse(tick=11, map_id=1, x=3, y=4, npc_type="rat")

    def absorb(self, events, uses=(USE,), others_in_sight=False):
        items: dict = {}
        it.absorb_npc_damaged(
            items, events, list(uses), default_map_id=1, armed_code="bronze_sword", others_in_sight=others_in_sight
        )
        return items

    def test_one_hit_on_our_block_and_tick(self):
        self.assertEqual(self.absorb([_npc_damaged()]), {"bronze_sword": {"weapon_damage": {"rat": 4}}})

    def test_event_map_id_defaults_to_ours(self):
        ev = _npc_damaged()
        del ev["map_id"]
        self.assertEqual(self.absorb([ev]), {"bronze_sword": {"weapon_damage": {"rat": 4}}})

    def test_no_use_records_nothing(self):
        self.assertEqual(self.absorb([_npc_damaged()], uses=()), {})

    def test_wrong_block_tick_or_map_records_nothing(self):
        for ev in (_npc_damaged(x=0, y=0), _npc_damaged(tick=12), _npc_damaged(map_id=2)):
            with self.subTest(ev=ev):
                self.assertEqual(self.absorb([ev]), {})

    def test_two_hits_on_the_block_and_tick_record_nothing(self):
        events = [_npc_damaged(amount=4), _npc_damaged(amount=7, npc_id=10)]
        self.assertEqual(self.absorb(events), {})

    def test_another_character_in_sight_records_nothing(self):
        self.assertEqual(self.absorb([_npc_damaged()], others_in_sight=True), {})

    def test_another_character_in_sight_at_the_use_records_nothing(self):
        use = it.AppliedUse(tick=11, map_id=1, x=3, y=4, npc_type="rat", others_in_sight=True)
        self.assertEqual(self.absorb([_npc_damaged()], uses=(use,)), {})

    def test_malformed_tick_is_skipped_without_raising(self):
        for tick in ("soon", None, [1]):
            with self.subTest(tick=tick):
                self.assertEqual(self.absorb([_npc_damaged(tick=tick)]), {})
        ev = _npc_damaged()
        del ev["tick"]
        self.assertEqual(self.absorb([ev]), {})

    def test_bad_amount_records_nothing(self):
        for amount in ("four", None, 0, -2, True, [4]):
            with self.subTest(amount=amount):
                self.assertEqual(self.absorb([_npc_damaged(amount=amount)]), {})

    def test_use_with_no_npc_type_records_nothing(self):
        use = it.AppliedUse(tick=11, map_id=1, x=3, y=4)
        self.assertEqual(self.absorb([_npc_damaged()], uses=(use,)), {})

    def test_hits_on_two_types_are_kept_apart(self):
        wolf = it.AppliedUse(tick=12, map_id=1, x=5, y=4, npc_type="wolf")
        events = [_npc_damaged(amount=4), _npc_damaged(tick=12, amount=6, x=5)]
        self.assertEqual(
            self.absorb(events, uses=(self.USE, wolf)),
            {"bronze_sword": {"weapon_damage": {"rat": 4, "wolf": 6}}},
        )

    def test_damaged_is_not_weapon_damage(self):
        hit = {"tick": 11, "kind": "Damaged", "source_kind": "trap", "source_id": 4, "amount": 3}
        self.assertEqual(self.absorb([hit]), {})


class UseTargetBlockTest(unittest.TestCase):
    def test_block_target(self):
        use = {"verb": "Use", "target": {"kind": "block", "x": 2, "y": 1}}
        self.assertEqual(it.use_target_block(use, []), (2, 1))

    def test_character_target_resolves_to_its_position(self):
        other = Entity("character", 7, (5, 6))
        use = {"verb": "Use", "target": {"kind": "character", "character_id": 7}}
        self.assertEqual(it.use_target_block(use, [Entity("npc", 7, (1, 1)), other]), (5, 6))

    def test_character_out_of_sight_has_no_block(self):
        use = {"verb": "Use", "target": {"kind": "character", "character_id": 7}}
        self.assertIsNone(it.use_target_block(use, []))

    def test_npc_target_resolves_to_where_it_was_seen(self):
        use = {"verb": "Use", "target": {"kind": "npc", "npc_id": 7}}
        self.assertEqual(it.use_target_block(use, [Entity("character", 7, (1, 1)), Entity("npc", 7, (5, 6))]), (5, 6))
        self.assertIsNone(it.use_target_block(use, []))

    def test_npc_use_attributed_only_to_its_target(self):
        # A45: another NPC on the block the target was seen on is not the hit.
        use = {"verb": "Use", "target": {"kind": "npc", "npc_id": 7}}
        goblin, rat = Entity("npc", 7, (5, 6), "goblin"), Entity("npc", 8, (5, 6), "rat")
        self.assertEqual(it.use_npc_type(use, (5, 6), [goblin]), "goblin")
        self.assertEqual(it.use_npc_type(use, (5, 6), [Entity("npc", 8, (5, 6), "rat")]), "")
        self.assertEqual(it.use_npc_type(use, (5, 6), [goblin, rat]), "")
        block = {"verb": "Use", "target": {"kind": "block", "x": 5, "y": 6}}
        self.assertEqual(it.use_npc_type(block, (5, 6), [rat]), "rat")

    def test_npc_type_only_for_one_npc_on_the_block(self):
        rat = Entity("npc", 1, (2, 1), "rat")
        self.assertEqual(it.npc_type_on_block((2, 1), [rat, Entity("character", 2, (2, 1))]), "rat")
        self.assertEqual(it.npc_type_on_block((2, 1), [rat, Entity("npc", 3, (2, 1), "wolf")]), "")
        self.assertEqual(it.npc_type_on_block((2, 1), [Entity("npc", 1, (2, 1))]), "")
        self.assertEqual(it.npc_type_on_block((3, 1), [rat]), "")


class RejectionTest(unittest.TestCase):
    def test_reach_only_from_target_out_of_range(self):
        self.assertEqual(it.rejection_attack_range(OUT_OF_RANGE), 2)
        self.assertIsNone(it.rejection_attack_range({**OUT_OF_RANGE, "outcome": "applied"}))
        other = {"outcome": "rejected", "rejection": {"code": "attack_cooldown", "attack_range": 2}}
        self.assertIsNone(it.rejection_attack_range(other))
        # Say and Read answer target_out_of_range with no attack_range.
        bare = {"outcome": "rejected", "rejection": {"code": "target_out_of_range"}}
        self.assertIsNone(it.rejection_attack_range(bare))

    def test_nothing_armed_records_nothing(self):
        items: dict = {}
        it.absorb_attack_range(items, None, 2)
        it.absorb_attack_range(items, "bronze_sword", None)
        self.assertEqual(items, {})


class LoadoutTest(unittest.TestCase):
    INV = {
        "gems": 5,
        "armed": {"id": 9, "supply_subtype_code": "pocket_knife"},
        "worn": {"body": {"id": 10, "supply_subtype_code": "bronze_mail"}},
        "held": [],
        "chest": [{"id": 11, "supply_subtype_code": "potion"}],
    }

    def test_loadout_from_inventory(self):
        self.assertEqual(it.loadout_from_inventory(self.INV), ("pocket_knife", {"body": "bronze_mail"}))
        self.assertEqual(it.loadout_from_inventory({"gems": 0, "armed": None, "worn": {}}), (None, {}))
        self.assertEqual(it.loadout_from_inventory(None), (None, {}))

    def test_world_tracks_loadout_from_snapshot_and_delta(self):
        w = WorldModel(1)
        w.apply_observation({"version": 1, "complete": True, "snapshot": {"inventory": self.INV}})
        self.assertEqual(w.armed_code, "pocket_knife")
        self.assertEqual(w.worn_codes, {"body": "bronze_mail"})
        inv = {**self.INV, "armed": {"id": 12, "supply_subtype_code": "bronze_sword"}, "worn": {}}
        w.apply_observation({"version": 2, "delta": {"inventory": inv}})
        self.assertEqual(w.armed_code, "bronze_sword")
        self.assertEqual(w.worn_codes, {})
        # A delta without inventory leaves the loadout alone.
        w.apply_observation({"version": 3, "delta": {"health": 9}})
        self.assertEqual(w.armed_code, "bronze_sword")

    def test_self_attack_range_clears_when_absent(self):
        w = WorldModel(1)
        w.apply_self({"lives": 3, "attack_range": 2})
        self.assertEqual(w.attack_range, 2)
        w.apply_self({"lives": 3})
        self.assertIsNone(w.attack_range)


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.sent = []

    def tick(self, cid, intents, snapshot_version=None):
        self.sent.append(intents)
        return self.responses.pop(0)


class RunnerItemLearningTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.object(config, "STATE_DIR", Path(tmp.name))
        patch.start()
        self.addCleanup(patch.stop)

    def _runner(self, kb: KnowledgeBase | None, client=None) -> Runner:
        cfg = CharacterConfig("t", "default", "test", "sandbox", Policy(goals=["hold"]), Path("t.toml"))
        r = Runner(cfg, client=client or object(), character_id=1, stop=threading.Event(), out=lambda _: None, knowledge=kb)
        self.addCleanup(r.trace.close)
        return r

    @staticmethod
    def _inv(code: str) -> dict:
        return {"gems": 0, "armed": {"id": 9, "supply_subtype_code": code}, "worn": {}, "held": [], "chest": []}

    def _queue(self, r: Runner, intents: list[dict]) -> None:
        r.mem.pending_queue = "q1"
        r.mem.pending_intents = intents
        r.mem.pending_next_index = 0

    def test_reach_is_filed_under_the_weapon_armed_in_the_same_response(self):
        # Arm resolves, then the Use is rejected, both in one response: the
        # reach belongs to the new weapon, which only the observation names.
        kb = KnowledgeBase.empty("sandbox")
        r = self._runner(kb)
        r.world.armed_code = "pocket_knife"
        self._queue(r, [{"verb": "Arm", "supply_id": 12}, {"verb": "Use", "target": {"kind": "block"}}])
        r.apply_intent_results(
            [
                {"queue_id": "q1", "index": 0, "tick": 10, "outcome": "applied"},
                {"queue_id": "q1", "index": 1, "tick": 11, **OUT_OF_RANGE},
            ]
        )
        self.assertEqual(kb.items, {})
        obs = {"version": 2, "delta": {"inventory": self._inv("long_spear")}}
        r.world.apply_observation(obs)
        r._learn_items_from_tick(obs)
        self.assertEqual(kb.items, {"long_spear": {"attack_range": 2}})
        self.assertIsNone(r._reach_seen)

    def test_reach_kept_once_and_only_from_use(self):
        kb = KnowledgeBase.empty("sandbox")
        r = self._runner(kb)
        r.world.armed_code = "pocket_knife"
        self._queue(r, [{"verb": "Read", "target": {"kind": "block"}}])
        r.apply_intent_results([{"queue_id": "q1", "index": 0, "tick": 10, **OUT_OF_RANGE}])
        r._learn_items_from_tick(None)
        self.assertEqual(kb.items, {})

    def test_tick_round_trip_records_reach_and_prices(self):
        kb = KnowledgeBase.empty("sandbox")
        response = {
            "tick": 11,
            "queue_id": "q1",
            "intent_results": [{"queue_id": "q1", "index": 0, "tick": 11, **OUT_OF_RANGE}],
            "events_by_tick": [],
            "observation": {
                "version": 2,
                "complete": True,
                "snapshot": {
                    "inventory": self._inv("long_spear"),
                    "entities": {
                        "supplies": [{"id": 77, "x": 1, "y": 1, "supply_subtype_code": "potion", "gem_price": 10}]
                    },
                },
            },
        }
        client = FakeClient([response])
        r = self._runner(kb, client)
        r.world.armed_code = "pocket_knife"
        use = {"verb": "Use", "target": {"kind": "block", "x": 4, "y": 4}}

        def one_use(d):
            r.mem.pending_intents, r.mem.pending_queue, r.mem.pending = None, None, use
            return [use]

        r.intents_for = one_use
        with mock.patch("agentrealm_agent.runner.decide", return_value=Decision(use, "test")):
            r.tick()
        self.assertEqual(client.sent, [[use]])
        self.assertEqual(kb.items, {"long_spear": {"attack_range": 2}, "potion": {"gem_price": 10}})

    def test_entities_read_records_prices(self):
        kb = KnowledgeBase.empty("sandbox")
        r = self._runner(kb)
        r._learn_items_from_entities({"supplies": [{"id": 1, "supply_subtype_code": "potion", "gem_price": 5}]})
        self.assertEqual(kb.items, {"potion": {"gem_price": 5}})

    def test_no_knowledge_base_is_a_no_op(self):
        r = self._runner(None)
        r.world.armed_code = "pocket_knife"
        self._queue(r, [{"verb": "Use", "target": {"kind": "block"}}])
        r.apply_intent_results([{"queue_id": "q1", "index": 0, "tick": 10, **OUT_OF_RANGE}])
        r._learn_items_from_tick({"version": 2, "complete": True, "snapshot": {"entities": {"supplies": []}}})
        r._learn_items_from_entities({"supplies": [{"id": 1, "supply_subtype_code": "potion", "gem_price": 5}]})
        self.assertIsNone(r._reach_seen)

    def test_potion_heal_learned_from_arm_and_use_in_one_response(self):
        # Heal sends [Arm potion, Use self]; both results land before the
        # observation names the potion as armed (A24).
        kb = KnowledgeBase.empty("sandbox")
        r = self._runner(kb)
        r.world.character_id, r.world.health, r.world.max_health = 1, 5, 20
        r.world.armed_code = "pocket_knife"
        r.world.held_supplies = [it.InventorySupply(1, "pocket_knife"), it.InventorySupply(4, "small_potion")]
        use = {"verb": "Use", "target": {"kind": "character", "character_id": 1}}
        self._queue(r, [{"verb": "Arm", "supply_id": 4}, use])
        r.apply_intent_results(
            [
                {"queue_id": "q1", "index": 0, "tick": 10, "outcome": "applied"},
                {"queue_id": "q1", "index": 1, "tick": 11, "outcome": "applied"},
            ]
        )
        r.world.health = 15
        absorb_heal_pending(r.mem, r.world, kb, [])
        self.assertEqual(kb.items, {"small_potion": {"heal_amount": 10}})

    def test_heal_learned_from_use_of_item_already_armed(self):
        kb = KnowledgeBase.empty("sandbox")
        r = self._runner(kb)
        r.world.character_id, r.world.health, r.world.max_health = 1, 5, 20
        r.world.armed_code = "berry"
        self._queue(r, [{"verb": "Use", "target": {"kind": "character", "character_id": 1}}])
        r.apply_intent_results([{"queue_id": "q1", "index": 0, "tick": 10, "outcome": "applied"}])
        r.world.health = 7
        absorb_heal_pending(r.mem, r.world, kb, [])
        self.assertEqual(kb.items, {"berry": {"heal_amount": 2}})

    def test_weapon_damage_from_matched_npc_damaged(self):
        kb = KnowledgeBase.empty("sandbox")
        r = self._runner(kb)
        r.world.map_id = 1
        r.world.armed_code = "pocket_knife"
        r.world.entities = [Entity("npc", 5, (2, 1), "rat")]
        use = {"verb": "Use", "target": {"kind": "block", "x": 2, "y": 1}}
        self._queue(r, [use])
        r.apply_intent_results([{"queue_id": "q1", "index": 0, "tick": 11, "outcome": "applied"}])
        events = [
            {
                "tick": 11,
                "kind": "NPCDamaged",
                "npc_id": 5,
                "amount": 2,
                "map_id": 1,
                "x": 2,
                "y": 1,
            }
        ]
        inv = self._inv("pocket_knife")
        obs = {"version": 2, "delta": {"inventory": inv}}
        r.world.apply_observation(obs)
        r._learn_items_from_tick(obs, events)
        self.assertEqual(kb.items, {"pocket_knife": {"weapon_damage": {"rat": 2}}})

    @staticmethod
    def _wear(r, worn):
        inv = {"gems": 0, "armed": None, "worn": worn, "held": [], "chest": []}
        r.world.apply_observation({"version": 1, "complete": True, "snapshot": {"inventory": inv}})

    MAIL = {"body": {"id": 10, "supply_subtype_code": "bronze_mail"}}
    HELM = {"head": {"id": 11, "supply_subtype_code": "bronze_helm"}}

    @staticmethod
    def _rat_hit(amount, tick=11):
        return [{"tick": tick, "kind": "Damaged", "source_kind": "npc", "source_id": 4, "amount": amount}]

    def test_damage_taken_from_npc_with_one_worn_slot(self):
        kb = KnowledgeBase.empty("sandbox")
        r = self._runner(kb)
        self._wear(r, self.MAIL)
        r.world.entities = [Entity("npc", 4, (2, 1), "rat")]
        r._learn_items_from_tick(None, self._rat_hit(2), [], {"body": "bronze_mail"})
        self.assertEqual(kb.items, {"bronze_mail": {"damage_taken": {"rat": 2}}})

    def test_damage_taken_skipped_with_two_worn_slots(self):
        kb = KnowledgeBase.empty("sandbox")
        r = self._runner(kb)
        self._wear(r, {**self.MAIL, **self.HELM})
        r.world.entities = [Entity("npc", 4, (2, 1), "rat")]
        r._learn_items_from_tick(None, self._rat_hit(2), [])
        self.assertEqual(kb.items, {})

    def test_hits_skipped_when_loadout_changed_in_response(self):
        # The hit landed on an earlier tick than the Wear the end-of-response
        # loadout reflects, so it cannot be filed under the mail.
        kb = KnowledgeBase.empty("sandbox")
        r = self._runner(kb)
        self._wear(r, self.MAIL)
        r.world.entities = [Entity("npc", 4, (2, 1), "rat")]
        r._learn_items_from_tick(None, self._rat_hit(4), [], {})
        self.assertEqual(kb.items, {})

    def test_hits_skipped_after_applied_wear_with_stale_snapshot(self):
        kb = KnowledgeBase.empty("sandbox")
        r = self._runner(kb)
        self._wear(r, self.MAIL)
        r.world.entities = [Entity("npc", 4, (2, 1), "rat")]
        r._loadout_verbs = ["Wear"]
        r._learn_items_from_tick(None, self._rat_hit(4), [])
        self.assertEqual(kb.items, {})

    def test_damage_saved_after_removing_the_lone_item(self):
        kb = KnowledgeBase.empty("sandbox")
        r = self._runner(kb)
        kb.items["bronze_mail"] = {"damage_taken": {"rat": 2}}
        kb.items["bronze_helm"] = {"damage_taken": {"rat": 1}}
        self._wear(r, self.MAIL)
        r.world.entities = [Entity("npc", 4, (2, 1), "rat")]
        worn = dict(r.world.worn_codes)
        self._wear(r, {})
        r._loadout_verbs = ["Remove"]
        # The response that removed the mail: its hit is skipped.
        r._learn_items_from_tick(None, self._rat_hit(9, tick=11), [], worn)
        # The next response, bare the whole time: the baseline goes to the mail only.
        r._learn_items_from_tick(None, self._rat_hit(5, tick=12), [], {})
        self.assertEqual(
            kb.items,
            {
                "bronze_mail": {"damage_taken": {"rat": 2}, "damage_without": {"rat": 5}, "damage_saved": {"rat": 3}},
                "bronze_helm": {"damage_taken": {"rat": 1}},
            },
        )

    def _remove_mail(self, kb):
        r = self._runner(kb)
        kb.items["bronze_mail"] = {"damage_taken": {"rat": 2}}
        r.world.map_id = 1
        r.world.entities = [Entity("npc", 4, (2, 1), "rat")]
        self._wear(r, {})
        r._loadout_verbs = ["Remove"]
        r._learn_items_from_tick(None, [], [], {"body": "bronze_mail"})
        return r

    def test_no_baseline_when_the_item_was_lost_without_remove(self):
        kb = KnowledgeBase.empty("sandbox")
        r = self._runner(kb)
        kb.items["bronze_mail"] = {"damage_taken": {"rat": 2}}
        r.world.entities = [Entity("npc", 4, (2, 1), "rat")]
        self._wear(r, {})
        r._learn_items_from_tick(None, [], [], {"body": "bronze_mail"})
        r._learn_items_from_tick(None, self._rat_hit(5, tick=12), [], {})
        self.assertEqual(kb.items, {"bronze_mail": {"damage_taken": {"rat": 2}}})

    def test_baseline_ends_at_the_next_loadout_change(self):
        kb = KnowledgeBase.empty("sandbox")
        r = self._remove_mail(kb)
        self._wear(r, self.HELM)
        r._learn_items_from_tick(None, [], [], {})
        self._wear(r, {})
        r._learn_items_from_tick(None, [], [], {"head": "bronze_helm"})
        r._learn_items_from_tick(None, self._rat_hit(5, tick=12), [], {})
        self.assertEqual(kb.items, {"bronze_mail": {"damage_taken": {"rat": 2}}})

    def test_baseline_ends_at_death(self):
        kb = KnowledgeBase.empty("sandbox")
        r = self._remove_mail(kb)
        r.on_events([{"tick": 12, "kind": "Died"}])
        r._learn_items_from_tick(None, self._rat_hit(5, tick=13), [], {})
        self.assertEqual(kb.items, {"bronze_mail": {"damage_taken": {"rat": 2}}})

    def test_baseline_ends_at_a_map_change(self):
        kb = KnowledgeBase.empty("sandbox")
        r = self._remove_mail(kb)
        r.world.map_id = 2
        r._learn_items_from_tick(None, self._rat_hit(5, tick=12), [], {})
        r.world.map_id = 1
        r._learn_items_from_tick(None, self._rat_hit(5, tick=13), [], {})
        self.assertEqual(kb.items, {"bronze_mail": {"damage_taken": {"rat": 2}}})

    def test_no_baseline_after_two_items_were_removed(self):
        kb = KnowledgeBase.empty("sandbox")
        r = self._runner(kb)
        kb.items["bronze_mail"] = {"damage_taken": {"rat": 2}}
        r.world.entities = [Entity("npc", 4, (2, 1), "rat")]
        self._wear(r, {})
        r._learn_items_from_tick(None, [], [], {"body": "bronze_mail", "head": "bronze_helm"})
        r._learn_items_from_tick(None, self._rat_hit(5, tick=12), [], {})
        self.assertEqual(kb.items, {"bronze_mail": {"damage_taken": {"rat": 2}}})

    def test_trap_damaged_does_not_fill_damage_taken(self):
        kb = KnowledgeBase.empty("sandbox")
        r = self._runner(kb)
        inv = {
            "gems": 0,
            "armed": None,
            "worn": {"body": {"id": 10, "supply_subtype_code": "bronze_mail"}},
            "held": [],
            "chest": [],
        }
        r.world.apply_observation({"version": 1, "complete": True, "snapshot": {"inventory": inv}})
        events = [{"tick": 11, "kind": "Damaged", "source_kind": "trap", "source_id": 4, "amount": 5}]
        r._learn_items_from_tick(None, events, [])
        self.assertEqual(kb.items, {})

    def test_weapon_damage_skipped_with_another_character_in_sight(self):
        kb = KnowledgeBase.empty("sandbox")
        r = self._runner(kb)
        r.world.map_id = 1
        r.world.armed_code = "pocket_knife"
        r.world.entities = [Entity("npc", 5, (2, 1), "rat"), Entity("character", 99, (2, 2))]
        self._queue(r, [{"verb": "Use", "target": {"kind": "block", "x": 2, "y": 1}}])
        r.apply_intent_results([{"queue_id": "q1", "index": 0, "tick": 11, "outcome": "applied"}])
        r._learn_items_from_tick(None, [_npc_damaged(amount=2, x=2, y=1)])
        self.assertEqual(kb.items, {})

    def test_weapon_damage_skipped_when_a_character_left_sight_before_the_observation(self):
        kb = KnowledgeBase.empty("sandbox")
        r = self._runner(kb)
        r.world.map_id = 1
        r.world.armed_code = "pocket_knife"
        r.world.entities = [Entity("npc", 5, (2, 1), "rat"), Entity("character", 99, (2, 2))]
        self._queue(r, [{"verb": "Use", "target": {"kind": "block", "x": 2, "y": 1}}])
        r.apply_intent_results([{"queue_id": "q1", "index": 0, "tick": 11, "outcome": "applied"}])
        r.world.entities = [Entity("npc", 5, (2, 1), "rat")]
        r._learn_items_from_tick(None, [_npc_damaged(amount=2, x=2, y=1)])
        self.assertEqual(kb.items, {})


if __name__ == "__main__":
    unittest.main()
