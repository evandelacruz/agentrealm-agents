"""A10: Heal state and healing helpers."""

import random
import unittest

from agentrealm_agent.healing import (
    REGEN_MEASURE_TICKS,
    flush_regen_measurement,
    food_in_sight,
    hurt,
    note_regen_sample,
    raise_buy_potion,
    regen_measurement,
    set_regen_measurement,
)
from agentrealm_agent.item_table import held_from_inventory
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.memory import Memory
from agentrealm_agent.states import dispatch
from agentrealm_agent.states.base import PlayContext
from agentrealm_agent.config import Policy
from agentrealm_agent.world import Entity, WorldModel, ZoneFact


def grid(at=(1, 1)) -> WorldModel:
    w = WorldModel(character_id=9, map_id=7, pos=at, perception=5, health=5, max_health=10)
    for x in range(5):
        for y in range(5):
            w.view.tiles[(x, y)] = "dirt"
    w.terrain_center, w.terrain_map = at, 7
    w.record_respawn_anchor(7, (0, 0))
    w.zones[7] = {(0, 0): ZoneFact(safe=True), at: ZoneFact(safe=False)}
    return w


def ctx(w: WorldModel, m: Memory | None = None, kb: KnowledgeBase | None = None) -> PlayContext:
    return PlayContext(m or Memory(), Policy(kind="scripted"), random.Random(0), knowledge=kb)


class HealingHelpersTest(unittest.TestCase):
    def test_hurt_and_food(self):
        w = grid()
        self.assertTrue(hurt(w))
        w.entities = [Entity("supply", 3, (1, 1), "apple")]
        self.assertEqual(food_in_sight(w)[0].code, "apple")

    def test_held_from_inventory(self):
        inv = {"held": [{"id": 4, "supply_subtype_code": "small_potion"}]}
        held = held_from_inventory(inv)
        self.assertEqual(held[0].code, "small_potion")

    def test_regen_measurement_round_trip(self):
        kb = KnowledgeBase.empty("sandbox")
        set_regen_measurement(kb, "yes")
        self.assertEqual(regen_measurement(kb), "yes")


class HealStateTest(unittest.TestCase):
    def test_heal_takes_adjacent_food(self):
        w = grid(at=(1, 1))
        w.entities = [Entity("supply", 8, (2, 1), "apple")]
        out = dispatch(w, ctx(w))
        self.assertEqual(out.state, "Heal")
        self.assertEqual(out.intents[0]["verb"], "Take")

    def test_heal_drinks_carried_potion(self):
        w = grid()
        w.held = held_from_inventory({"held": [{"id": 4, "supply_subtype_code": "small_potion"}]})
        out = dispatch(w, ctx(w))
        self.assertEqual(out.state, "Heal")
        self.assertEqual([i["verb"] for i in out.intents or []], ["Arm", "Use"])

    def test_heal_waits_in_town_and_raises_buy(self):
        w = grid(at=(0, 0))
        w.zones[7][(0, 0)] = ZoneFact(safe=True)
        m = Memory()
        kb = KnowledgeBase.empty("sandbox")
        set_regen_measurement(kb, "no")
        dispatch(w, ctx(w, m, kb))
        self.assertEqual(len(m.buy_signals), 1)
        self.assertEqual(m.buy_signals[0]["op"], "buy")

    def test_heal_yields_to_explore_at_full_health(self):
        w = grid()
        w.health = 10
        out = dispatch(w, ctx(w))
        self.assertEqual(out.state, "Explore")

    def test_regen_observed_in_safe_zone(self):
        w = grid(at=(0, 0))
        w.zones[7][(0, 0)] = ZoneFact(safe=True)
        m = Memory()
        kb = KnowledgeBase.empty("sandbox")
        m.heal_regen_start_tick = w.tick
        m.heal_regen_start_health = 5
        w.health = 6
        note_regen_sample(m, w)
        self.assertEqual(m.heal_regen_measured, "yes")
        flush_regen_measurement(m, kb)
        self.assertEqual(regen_measurement(kb), "yes")

    def test_regen_absence_after_window(self):
        w = grid(at=(0, 0))
        w.zones[7][(0, 0)] = ZoneFact(safe=True)
        m = Memory()
        m.heal_regen_start_tick = w.tick - REGEN_MEASURE_TICKS
        m.heal_regen_start_health = 5
        note_regen_sample(m, w)
        self.assertEqual(m.heal_regen_measured, "no")


if __name__ == "__main__":
    unittest.main()
