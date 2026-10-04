"""A20: Loot state — Take, WithdrawFromChest, Drop junk; hearts first."""

import random
import unittest

from agentrealm_agent.brain import Memory, decide
from agentrealm_agent.config import Policy
from agentrealm_agent.item_table import InventorySupply
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.states import dispatch
from agentrealm_agent.states.base import PlayContext
from agentrealm_agent.world import Entity, WorldModel


def world(rows: list[str], at=(0, 0), perception=5) -> WorldModel:
    glyph = {".": "dirt", "#": "wall"}
    w = WorldModel(character_id=1, map_id=7, pos=at, perception=perception)
    for y, row in enumerate(rows):
        for x, g in enumerate(row):
            w.view.tiles[(x, y)] = glyph[g]
    w.terrain_center, w.terrain_map = at, 7
    return w


def scripted(**kw) -> Policy:
    return Policy(kind="scripted", **kw)


def full_inventory(w: WorldModel, *, junk: str = "torch") -> None:
    """Ten slots: knife armed, nine junk held."""
    w.armed_code = "pocket_knife"
    w.worn_codes = {}
    w.held_supplies = [InventorySupply(i, junk) for i in range(1, 10)]
    w.chest_supplies = []
    w.carry_capacity = 10


class LootPriorityTest(unittest.TestCase):
    def test_heart_beats_sword_when_both_adjacent(self):
        w = world(["..."], at=(1, 1))
        w.entities = [
            Entity("supply", 2, (1, 2), "bronze_sword"),
            Entity("supply", 3, (2, 1), "heart"),
        ]
        d = decide(w, Memory(), scripted(), random.Random(0))
        self.assertEqual(d.intent, {"verb": "Take", "supply_id": 3})

    def test_drop_junk_before_take_when_full(self):
        w = world(["..."], at=(1, 1))
        full_inventory(w, junk="torch")
        w.entities = [Entity("supply", 99, (1, 2), "bronze_sword")]
        kb = KnowledgeBase("sandbox")
        kb.items["bronze_sword"] = {"gem_price": 5}
        ctx = PlayContext(Memory(), scripted(), random.Random(0), knowledge=kb)
        out = dispatch(w, ctx)
        self.assertEqual(out.state, "Loot")
        self.assertEqual(out.intents[0]["verb"], "Drop")
        self.assertEqual(out.intents[0]["supply_id"], 1)

    def test_priced_supply_is_shop_not_loot(self):
        w = world(["..."], at=(1, 1))
        w.entities = [Entity("supply", 5, (1, 2), "potion", gem_price=2)]
        d = decide(w, Memory(), scripted(goals=["hold"]), random.Random(0))
        self.assertIsNone(d.intent, d.reason)

    def test_loot_beats_explore_when_supply_in_sight(self):
        w = world(["....", "...."], at=(0, 0))
        w.entities = [Entity("supply", 8, (3, 0), "gem")]
        out = dispatch(w, PlayContext(Memory(), scripted(goals=["explore"]), random.Random(0)))
        self.assertEqual(out.state, "Loot")
        self.assertEqual(out.intents[0]["verb"], "SetPosition")


class LootHostileTest(unittest.TestCase):
    def test_hostile_blocks_loot_so_explore_flees(self):
        w = world(["...", "...", "..."], at=(1, 1))
        w.entities = [
            Entity("supply", 8, (1, 2), "gem"),
            Entity("npc", 5, (2, 1)),
        ]
        out = dispatch(w, PlayContext(Memory(), scripted(), random.Random(0)))
        self.assertEqual(out.state, "Explore")
        self.assertEqual(out.intents[0]["verb"], "SetPosition")


if __name__ == "__main__":
    unittest.main()
