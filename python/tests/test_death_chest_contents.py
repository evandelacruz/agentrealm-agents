"""A167: a death chest holds what he carried when he died, and is a source ahead of buying."""

import json
import unittest

from agentrealm_adk.chest_sources import FIELD_GEMS_PER_MIN, chest_items, refuse_chest_held, source_for
from agentrealm_adk.item_table import InventorySupply
from agentrealm_adk.knowledge_base import KnowledgeBase
from agentrealm_adk.knowledge_base.death_chests import load_death_chests, save_death_chests
from agentrealm_adk.travel.knowledge import entrance_key, record_shop_cell
from agentrealm_adk.world import DeathChest, WorldModel
from agentrealm_agent.strategist import death_chests_line
from ladder import goals
from ladder.act import Ctx, Started, act
from ladder.goals import walk

from tests.test_death_chests import MAP, reloaded, world

CARRIED = {
    "gems": 15,
    "armed": {"id": 1, "supply_subtype_code": "pocket_knife"},
    "held": [{"id": 2, "supply_subtype_code": "bronze_sword"}, {"id": 3, "supply_subtype_code": "small_potion"}],
}


def carrying(w: WorldModel, inventory=None) -> WorldModel:
    w.apply_observation({"complete": True, "snapshot": {"inventory": inventory or CARRIED}})
    return w


def die(w: WorldModel, chest_id: int, x: int, dropped=None) -> None:
    """Die with what is carried, the chest landing at ``x``; then stand back where we were, empty-handed."""
    here = w.pos
    ev = {"kind": "Died", "cause": "killed", "chest_id": chest_id, "map_id": MAP, "x": x, "y": 0}
    if dropped is not None:
        ev["dropped_supply_ids"] = dropped
    w.apply_events([{"tick": w.tick, "events": [ev]}])
    w.apply_observation({"complete": True, "snapshot": {"inventory": {"gems": 15, "armed": CARRIED["armed"], "held": []}}})
    w.map_id, w.pos = MAP, here


def chest_seen(w: WorldModel, chest_id: int, x: int, codes: list[str]) -> None:
    contents = [{"id": 50 + i, "supply_subtype_code": c} for i, c in enumerate(codes)]
    w.apply_observation({"complete": True, "snapshot": {"entities": {"chests": [{"id": chest_id, "x": x, "y": 0, "contents": contents}]}}})


class ExpectedContentsTest(unittest.TestCase):
    def test_died_records_what_was_carried_but_the_starting_kit(self):
        w = carrying(world(at=(0, 0), perception=3))
        die(w, 80, 29)
        chest = w.death_chests[80]
        self.assertEqual(chest.contents, ["bronze_sword", "small_potion"])
        self.assertFalse(chest.seen)

    def test_dropped_supply_ids_name_what_dropped(self):
        w = carrying(world(at=(0, 0), perception=3))
        die(w, 80, 29, dropped=[2])
        self.assertEqual(w.death_chests[80].contents, ["bronze_sword"])

    def test_nothing_known_carried_leaves_it_unknown(self):
        w = world(at=(0, 0), perception=3)
        die(w, 80, 29)
        self.assertIsNone(w.death_chests[80].contents)

    def test_seen_contents_replace_expected(self):
        w = carrying(world(at=(0, 0), perception=3))
        die(w, 80, 1)
        chest_seen(w, 80, 1, ["small_potion"])
        self.assertEqual((w.death_chests[80].contents, w.death_chests[80].seen), (["small_potion"], True))

    def test_expected_and_seen_are_kept_across_runs(self):
        w = carrying(world(at=(0, 0), perception=3))
        die(w, 80, 29)
        kb = KnowledgeBase.empty("sandbox")
        save_death_chests(kb, w)
        w2 = world()
        load_death_chests(reloaded(kb), w2)
        self.assertEqual((w2.death_chests[80].contents, w2.death_chests[80].seen), (["bronze_sword", "small_potion"], False))

    def test_a_row_saved_before_seen_was_kept_was_seen(self):
        kb = KnowledgeBase.empty("sandbox")
        kb.extra["death_chests"] = {"1": {"80": {"map_id": MAP, "x": 3, "y": 0, "died": 5, "contents": ["torch"]}}}
        w = world()
        load_death_chests(kb, w)
        self.assertTrue(w.death_chests[80].seen)


class StateLineTest(unittest.TestCase):
    def test_the_line_shows_expected_contents_with_price_and_need(self):
        w = carrying(world(at=(0, 0), perception=3))
        die(w, 80, 29)
        kb = KnowledgeBase.empty("sandbox")
        record_shop_cell(kb, MAP, (5, 5), "bronze_sword", 15)
        rows = json.loads(death_chests_line(w, kb, [{"op": "buy", "code": "bronze_sword"}]).removeprefix("death_chests="))
        self.assertEqual(rows[0]["contents_are"], "expected")
        sword, potion = rows[0]["contents"]
        self.assertEqual((sword["code"], sword["price"], sword["needed"]), ("bronze_sword", 15, "a buy on the stack wants it"))
        self.assertEqual(potion["code"], "small_potion")
        self.assertIn("potions held 0", potion["needed"])


class ChestBeforeBuyTest(unittest.TestCase):
    def chest_at(self, x: int) -> WorldModel:
        w = world(at=(0, 0))
        w.death_chests[80] = DeathChest(MAP, (x, 0), 5, ["bronze_sword"])
        return w

    def test_a_buy_is_left_out_while_the_chest_is_nearer_than_earning_the_gems(self):
        w = self.chest_at(30)  # 12 s walk; 15 gems take 150 s at the field rate
        kept, refused = refuse_chest_held([{"op": "buy", "code": "bronze_sword"}], w, {"bronze_sword": 15})
        self.assertEqual(kept, [])
        self.assertIn("death chest 80", refused[0])
        self.assertIn("expected to hold", refused[0])

    def test_a_buy_is_kept_when_earning_the_gems_is_quicker(self):
        w = self.chest_at(30)
        kept, refused = refuse_chest_held([{"op": "buy", "code": "bronze_sword"}], w, {"bronze_sword": 15}, gems_per_min=600)
        self.assertEqual((len(kept), refused), (1, []))

    def test_a_buy_is_kept_with_the_chest_in_reach_or_on_another_map(self):
        for w in (self.chest_at(1), world(at=(0, 0))):
            w.death_chests.setdefault(80, DeathChest(9, (3, 3), 5, ["bronze_sword"]))
            kept, _ = refuse_chest_held([{"op": "buy", "code": "bronze_sword"}], w, {"bronze_sword": 15})
            self.assertEqual(len(kept), 1)

    def test_the_field_rate_prices_the_gems_until_one_is_measured(self):
        w = self.chest_at(30 * 60 // 60 * 15)  # 450 steps: 180 s, longer than 150 s
        self.assertIsNone(source_for("bronze_sword", w, 15))
        self.assertIsNotNone(source_for("bronze_sword", w, 15, gems_per_min=FIELD_GEMS_PER_MIN / 2))
        self.assertEqual([i.code for i in chest_items(w)], ["bronze_sword"])


class GaryReplyTest(unittest.TestCase):
    def test_a_reply_buying_what_a_near_death_chest_holds_is_refused_with_why(self):
        from tests.test_strategist import FakeLLM, fake_runner, make, round_trip

        buy = {"op": "buy", "code": "small_potion", "why": "restock"}
        s, r = make(FakeLLM({"goals": [buy]})), fake_runner()
        r.world.death_chests[80] = DeathChest(7, (20, 0), 5, ["small_potion"])
        round_trip(s, r)
        self.assertNotIn("buy", [g["op"] for g in r.plan.ops_left()])
        self.assertTrue(any("death chest 80" in why for why in s.rejected))


class LorraineTest(unittest.TestCase):
    MAP, DOOR = 1, (10, 4)

    def world(self, **kw) -> WorldModel:
        w = WorldModel(character_id=1)
        w.map_id, w.pos, w.map_level = self.MAP, (5, 5), 0
        w.health = w.max_health = 10
        w.gems, w.armed_code, w.level_count = 0, "sword", 3
        held = [InventorySupply(i, "large_potion") for i in (1, 2, 3)]
        w.armed_code, w.worn_codes, w.held_supplies = "adamant_sword", {"body": "adamant_mail"}, held
        for k, v in kw.items():
            setattr(w, k, v)
        return w

    def kb(self) -> KnowledgeBase:
        kb = KnowledgeBase.empty("sandbox")
        kb.entrances[entrance_key(self.MAP, self.DOOR)] = {"level": 1, "clue_needs": "fords"}
        record_shop_cell(kb, self.MAP, (20, 20), "raft", 30)
        return kb

    def test_her_requires_node_takes_the_item_from_a_death_chest_ahead_of_buying(self):
        w = self.world(gems=40)
        w.death_chests[80] = DeathChest(self.MAP, (15, 5), 5, ["raft"])
        turn = walk(w, self.kb())
        self.assertEqual(turn.goal.key, "requires")
        self.assertEqual(turn.op(), {"op": "fetch_item", "code": "raft", "x": 15, "y": 5, "chest_id": 80})
        self.assertEqual(turn.reading.missing(goals.find("level", goals.tree())).gems, 0)

    def test_without_the_chest_she_buys(self):
        self.assertEqual(walk(self.world(gems=40), self.kb()).op(), {"op": "buy", "code": "raft"})

    def test_fetch_from_a_chest_withdraws_once_in_reach(self):
        w = self.world()
        w.death_chests[80] = DeathChest(self.MAP, (6, 5), 5, ["raft"])
        w.chest_contents[80] = [InventorySupply(9, "raft")]
        ctx = Ctx(w, None, None, None, Started.now(w))
        out = act({"op": "fetch_item", "code": "raft", "x": 6, "y": 5, "chest_id": 80}, ctx)
        self.assertEqual(out.intents, [{"verb": "WithdrawFromChest", "chest_id": 80}])
        again = act({"op": "fetch_item", "code": "raft", "x": 6, "y": 5, "chest_id": 80}, ctx)
        self.assertTrue(again.over)


if __name__ == "__main__":
    unittest.main()
