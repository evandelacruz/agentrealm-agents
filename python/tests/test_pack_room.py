"""A102: one rule for making room in a full pack (``pack.make_room``).

Reserved items are never dropped, nothing is dropped on a shop cell, and
what to drop is always the planner's choice (``drop`` on ``buy`` and
``fetch_item``): no code ranks a held item as useless. Shop, Loot and the
Pickup reflex share it.
"""

import json
import random
import unittest

from agentrealm_adk.config import Policy
from agentrealm_adk.directives import PARAM_DEFAULTS, Directives
from agentrealm_adk.item_table import FragmentMeta, InventorySupply
from agentrealm_adk.knowledge_base import KnowledgeBase
from agentrealm_adk.loot import Pickup
from agentrealm_adk.memory import Memory
from agentrealm_adk.pack import ASK, DROP, MOVE, TAKE, drop_makes_stock, make_room, reserved_supplies
from agentrealm_agent.plan import Plan, validate_goal_op
from agentrealm_agent.states import dispatch
from agentrealm_agent.states.base import PlayContext
from agentrealm_agent.states.detour import valuable
from agentrealm_agent.strategist import build_prompt, upgrade_line
from agentrealm_adk.supplies import chest_capacity
from agentrealm_adk.travel import record_shop_cell
from agentrealm_adk.world import Entity, WorldModel

PRICES = {"bronze_mallet": 25, "matches": 5, "small_potion": 3, "bronze_sword": 15, "rope": 4}
MALLET, MATCHES_A, MATCHES_B = 1, 2, 3


def world(at=(2, 2)) -> WorldModel:
    w = WorldModel(character_id=1, map_id=1, pos=at, perception=5, gems=20)
    for x in range(6):
        for y in range(6):
            w.view.tiles[(x, y)] = "dirt"
    w.terrain_center, w.terrain_map = at, 1
    return w


def full_pack(w: WorldModel, *, spare: str = "rope") -> None:
    """Ten slots: knife armed; a mallet, two boxes of matches and six ``spare`` held."""
    w.armed_code = "pocket_knife"
    w.worn_codes = {}
    w.held_supplies = [
        InventorySupply(MALLET, "bronze_mallet"),
        InventorySupply(MATCHES_A, "matches"),
        InventorySupply(MATCHES_B, "matches"),
        *(InventorySupply(i, spare) for i in range(4, 10)),
    ]
    w.chest_supplies = []


def kb() -> KnowledgeBase:
    k = KnowledgeBase.empty("sandbox")
    for code, gems in PRICES.items():
        k.items[code] = {"gem_price": gems}
    return k


def plan(*ops) -> Plan:
    return Plan([dict(op) for op in ops], dict(PARAM_DEFAULTS))


def ctx(p: Plan | None, k: KnowledgeBase | None = None) -> PlayContext:
    return PlayContext(Memory(equip_not_wearable={"rope"}), Policy(kind="scripted", goals=[]), random.Random(0), knowledge=k or kb(), plan=p)


CLUE_BURNS = [{"op": "break_block", "x": 9, "y": 9, "capability": "burn"}, {"op": "break_block", "x": 12, "y": 9, "capability": "burn"}]
POTION = Entity("supply", 50, (2, 3), "small_potion", gem_price=3)


class ReservedTest(unittest.TestCase):
    def test_each_burn_op_reserves_its_own_box_of_matches(self):
        w = world()
        full_pack(w)
        self.assertEqual([s.id for s in reserved_supplies(w, CLUE_BURNS, kb())], [MATCHES_A, MATCHES_B])

    def test_a_tool_kept_on_break_covers_every_op(self):
        w = world()
        full_pack(w)
        ops = [{"op": "break_block", "x": 9, "y": 9, "capability": "smash"}] * 2
        self.assertEqual([s.id for s in reserved_supplies(w, ops, kb())], [MALLET])

    def test_use_block_equip_and_boss_name_codes(self):
        w = world()
        full_pack(w)
        ops = [
            {"op": "use_block", "x": 1, "y": 1, "code": "matches"},
            {"op": "equip", "code": "bronze_mallet"},
            {"op": "fight_boss", "x": 1, "y": 1, "worn": ["rope"]},
        ]
        self.assertEqual(sorted(s.id for s in reserved_supplies(w, ops, kb())), [MALLET, MATCHES_A, 4])

    def test_compose_reserves_one_fragment_per_slot(self):
        w = world()
        full_pack(w)
        w.held_supplies[3:6] = [
            InventorySupply(4, "key_half", FragmentMeta(composes_into="gate_key", piece_count=2, slot=1, missing_slots=(2,))),
            InventorySupply(5, "key_half", FragmentMeta(composes_into="gate_key", piece_count=2, slot=2, missing_slots=(1,))),
            InventorySupply(6, "key_half", FragmentMeta(composes_into="gate_key", piece_count=2, slot=1, missing_slots=(2,))),
        ]
        ops = [{"op": "compose", "composes_into": "gate_key"}]
        self.assertEqual(sorted(s.id for s in reserved_supplies(w, ops, kb())), [4, 5])
        room = make_room(w, Pickup(POTION.id, POTION.code, POTION.pos, None, 503), plan_ops=ops, knowledge=kb(), named="key_half")
        self.assertEqual((room.kind, room.drop.id), (DROP, 6))


    def test_compose_reserves_held_fragments_as_solve_composes_them(self):
        w = world()
        full_pack(w)
        w.held_supplies[3:5] = [
            InventorySupply(14, "key_half", FragmentMeta(composes_into="gate_key", piece_count=2, slot=1, missing_slots=(2,))),
            InventorySupply(15, "key_half", FragmentMeta(composes_into="gate_key", piece_count=2, slot=2, missing_slots=(1,))),
        ]
        w.chest_supplies = [InventorySupply(4, "key_half", FragmentMeta(composes_into="gate_key", piece_count=2, slot=1, missing_slots=(2,)))]
        ops = [{"op": "compose", "composes_into": "gate_key"}]
        self.assertEqual(sorted(s.id for s in reserved_supplies(w, ops, kb())), [14, 15])

class MakeRoomTest(unittest.TestCase):
    def potion(self) -> Pickup:
        return Pickup(POTION.id, POTION.code, POTION.pos, None, 503)

    def test_room_left_takes(self):
        w = world()
        full_pack(w)
        w.held_supplies.pop()
        self.assertEqual(make_room(w, self.potion(), knowledge=kb()).kind, TAKE)

    def test_even_an_unpriced_unknown_item_is_not_dropped_unasked(self):
        w = world()
        full_pack(w, spare="bent_spoon")
        self.assertEqual(make_room(w, self.potion(), knowledge=kb()).kind, ASK)

    def test_what_to_drop_is_the_planners_call(self):
        w = world()
        full_pack(w)
        room = make_room(w, self.potion(), plan_ops=CLUE_BURNS, knowledge=kb())
        self.assertEqual(room.kind, ASK)
        self.assertIn("bronze_mallet", room.why)
        self.assertIn("10/10", room.why)

    def test_a_named_drop_is_used(self):
        w = world()
        full_pack(w)
        room = make_room(w, self.potion(), plan_ops=CLUE_BURNS, knowledge=kb(), named="bronze_mallet")
        self.assertEqual((room.kind, room.drop.id), (DROP, MALLET))

    def test_a_reserved_item_is_never_dropped_even_named(self):
        w = world()
        full_pack(w)
        room = make_room(w, self.potion(), plan_ops=CLUE_BURNS, knowledge=kb(), named="matches")
        self.assertEqual(room.kind, ASK)

    def test_a_named_code_skips_its_reserved_copies(self):
        w = world()
        full_pack(w)
        ops = [{"op": "use_block", "x": 1, "y": 1, "code": "rope"}]
        self.assertEqual(make_room(w, self.potion(), plan_ops=ops, knowledge=kb(), named="rope").drop.id, 5)

    def test_no_drop_on_a_shop_cell(self):
        w = world()
        full_pack(w)
        k = kb()
        record_shop_cell(k, 1, (2, 2))
        self.assertTrue(drop_makes_stock(w, k))
        room = make_room(w, self.potion(), knowledge=k, named="bronze_mallet")
        self.assertEqual((room.kind, room.drop.id), (MOVE, MALLET))


class ShopRoomTest(unittest.TestCase):
    def test_full_pack_buy_with_no_drop_named_goes_back_to_the_planner(self):
        w = world()
        full_pack(w)
        w.entities = [POTION]
        c = ctx(plan({"op": "buy", "code": "small_potion"}, *CLUE_BURNS))
        out = dispatch(w, c)
        self.assertFalse(any(i.get("verb") == "Drop" for i in out.intents or []), out.intents)
        failed = [s for s in c.memory.strategist_signals if s["trigger"] == "goal_failed"]
        self.assertEqual(failed[0]["op"]["op"], "buy")
        self.assertIn("droppable", failed[0]["reason"])
        self.assertEqual(c.plan.current()["op"], "break_block")

    def test_buy_drops_what_it_names_then_takes(self):
        w = world()
        full_pack(w)
        w.entities = [POTION]
        out = dispatch(w, ctx(plan({"op": "buy", "code": "small_potion", "drop": "bronze_mallet"}, *CLUE_BURNS)))
        self.assertEqual(out.state, "Shop")
        self.assertEqual(out.intents, [{"verb": "Drop", "supply_id": MALLET}, {"verb": "Take", "supply_id": POTION.id}])

    def test_buy_steps_off_a_shop_cell_before_dropping(self):
        w = world()
        full_pack(w)
        w.entities = [POTION]
        k = kb()
        record_shop_cell(k, 1, (2, 2))
        record_shop_cell(k, 1, (2, 3))
        out = dispatch(w, ctx(plan({"op": "buy", "code": "small_potion", "drop": "bronze_mallet"}), k))
        self.assertEqual(out.state, "Shop")
        self.assertEqual(len(out.intents), 1)
        step = out.intents[0]
        self.assertEqual(step["verb"], "SetPosition")
        spot = (step["x"], step["y"])
        self.assertNotIn(spot, {(2, 2), (2, 3)})
        self.assertLessEqual(max(abs(spot[0] - 2), abs(spot[1] - 3)), 1)

    def test_no_cell_off_the_shop_hands_the_buy_back(self):
        w = world()
        full_pack(w)
        w.entities = [POTION]
        k = kb()
        for x in range(1, 4):
            for y in range(1, 5):
                record_shop_cell(k, 1, (x, y))
        c = ctx(plan({"op": "buy", "code": "small_potion", "drop": "bronze_mallet"}), k)
        out = dispatch(w, c)
        self.assertFalse(any(i.get("verb") in ("Drop", "Take") for i in out.intents or []), out.intents)
        failed = [s for s in c.memory.strategist_signals if s["trigger"] == "goal_failed"]
        self.assertIn("off the shop", failed[0]["reason"])
        self.assertIsNone(c.plan.current())


class LootRoomTest(unittest.TestCase):
    def test_fetch_item_drops_what_it_names(self):
        w = world()
        full_pack(w)
        w.entities = [Entity("supply", 60, (2, 3), "bronze_sword")]
        out = dispatch(w, ctx(plan({"op": "fetch_item", "code": "bronze_sword", "drop": "rope"})))
        self.assertEqual(out.state, "Loot")
        self.assertEqual(out.intents, [{"verb": "Drop", "supply_id": 4}, {"verb": "Take", "supply_id": 60}])


class PickupRoomTest(unittest.TestCase):
    def test_reflex_never_drops_gear_for_a_free_find(self):
        w = world()
        full_pack(w)
        w.entities = [Entity("supply", 60, (2, 3), "bronze_sword")]
        out = dispatch(w, ctx(plan()))
        self.assertNotEqual(out.state, "Pickup")
        self.assertFalse(any(i.get("verb") in ("Drop", "Take") for i in out.intents or []), out.intents)

    def test_reflex_takes_what_fits_on_a_shop_cell(self):
        w = world()
        full_pack(w)
        w.held_supplies.pop()
        k = kb()
        record_shop_cell(k, 1, (2, 2))
        w.entities = [Entity("supply", 60, (2, 3), "bronze_sword")]
        out = dispatch(w, ctx(plan(), k))
        self.assertEqual(out.state, "Pickup")
        self.assertEqual(out.intents, [{"verb": "Take", "supply_id": 60}])

    def test_reflex_never_drops_on_a_shop_cell_either(self):
        w = world()
        full_pack(w)
        k = kb()
        record_shop_cell(k, 1, (2, 2))
        w.entities = [Entity("supply", 60, (2, 3), "bronze_sword")]
        out = dispatch(w, ctx(plan(), k))
        self.assertFalse(any(i.get("verb") in ("Drop", "Take") for i in out.intents or []), out.intents)


class DetourRoomTest(unittest.TestCase):
    def test_food_is_worth_a_detour_only_when_it_fits(self):
        w = world()
        full_pack(w)
        w.health, w.max_health = 3, 10
        apple = Entity("supply", 70, (5, 5), "apple")
        self.assertFalse(valuable(w, apple, {}, ctx(plan())))
        w.held_supplies.pop()
        self.assertTrue(valuable(w, apple, {}, ctx(plan())))
        gem = Entity("supply", 71, (5, 4), "gem")
        w.held_supplies.append(InventorySupply(9, "rope"))
        self.assertTrue(valuable(w, gem, {}, ctx(plan())))


class PlanTest(unittest.TestCase):
    def test_drop_must_be_a_code(self):
        self.assertIsNotNone(validate_goal_op({"op": "buy", "code": "small_potion", "drop": "rope"}))
        self.assertIsNone(validate_goal_op({"op": "buy", "code": "small_potion", "drop": 4}))
        self.assertIsNone(validate_goal_op({"op": "fetch_item", "code": "rope", "drop": ""}))

    def test_state_shows_pack_slots_and_reserved(self):
        w = world()
        full_pack(w)
        messages = build_prompt(
            triggers=[],
            w=w,
            plan=plan({"op": "buy", "code": "small_potion"}, *CLUE_BURNS),
            directives=Directives(params=dict(PARAM_DEFAULTS)),
            knowledge=kb(),
        )
        row = {"reserved": {"matches": 2}, "slots_total": 10, "slots_used": 10}
        self.assertIn(f"pack={json.dumps(row, sort_keys=True)}", messages[1]["content"])
        self.assertIn("Pack room:", messages[0]["content"])
        held = json.loads(messages[1]["content"].split("held=", 1)[1].split("\n", 1)[0])
        self.assertIn("burn", held["matches"]["use"])
        self.assertIn("smash", held["bronze_mallet"]["use"])

    def test_a_bigger_chest_for_sale_is_a_room_option(self):
        w = world()
        full_pack(w)
        k = kb()
        k.items["middle_chest"] = {"gem_price": 50}
        k.items["blue_chest"] = {"gem_price": 5}
        rows = json.loads(upgrade_line(w, k).split("=", 1)[1])
        chests = {r["code"]: r["gains"] for r in rows if "chest" in r["code"]}
        self.assertEqual(chests, {"middle_chest": f"carry slots 10 -> {chest_capacity('middle_chest')}"})


if __name__ == "__main__":
    unittest.main()
