"""A10: Heal state and healing helpers."""

import random
import unittest

from agentrealm_agent.config import Policy
from agentrealm_agent.healing import (
    HEAL_BACKOFF_TICKS,
    HEAL_MAX_TRIES,
    HEAL_WAIT_TICKS,
    REGEN_KEY,
    REGEN_MEASURE_TICKS,
    SURVIVAL_KEY,
    hurt,
    regen_known,
    save_regen_yes,
)
from agentrealm_agent.item_table import InventorySupply, carried_from_inventory
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.memory import Memory
from agentrealm_agent.states import dispatch
from agentrealm_agent.states.base import PlayContext
from agentrealm_agent.world import Entity, WorldModel, ZoneFact


def grid(at=(1, 1), size=5) -> WorldModel:
    """Map 7, all dirt, safe zone at (0, 0) beside the respawn anchor."""
    w = WorldModel(character_id=9, map_id=7, pos=at, perception=size, health=5, max_health=10)
    for x in range(size):
        for y in range(size):
            w.view.tiles[(x, y)] = "dirt"
    w.terrain_center, w.terrain_map = at, 7
    w.record_respawn_anchor(7, (0, 0))
    w.zones[7] = {(0, 0): ZoneFact(safe=True)}
    if at != (0, 0):
        w.zones[7][at] = ZoneFact(safe=False)
    return w


def ctx(m: Memory | None = None, kb: KnowledgeBase | None = None) -> PlayContext:
    return PlayContext(m or Memory(), Policy(kind="scripted"), random.Random(0), knowledge=kb)


def verbs(out) -> list[str]:
    return [i["verb"] for i in out.intents or []]


class HealingHelpersTest(unittest.TestCase):
    def test_hurt(self):
        self.assertTrue(hurt(grid()))

    def test_held_from_carried_inventory(self):
        inv = {"held": [{"id": 4, "supply_subtype_code": "small_potion"}, {"id": "x"}, "junk"]}
        self.assertEqual(carried_from_inventory(inv)[0], [InventorySupply(4, "small_potion")])

    def test_apply_observation_fills_held(self):
        w = WorldModel(character_id=9)
        inv = {"held": [{"id": 4, "supply_subtype_code": "apple"}], "armed": None}
        w.apply_observation({"complete": True, "snapshot": {"health": 5, "max_health": 10, "inventory": inv}})
        self.assertEqual(w.held_supplies, [InventorySupply(4, "apple")])

    def test_only_yes_is_read_from_knowledge_base(self):
        kb = KnowledgeBase.empty("sandbox")
        kb.extra[SURVIVAL_KEY] = {REGEN_KEY: "no"}
        self.assertIsNone(regen_known(kb, Memory()))
        save_regen_yes(kb)
        self.assertEqual(regen_known(kb, Memory()), "yes")


class HealStateTest(unittest.TestCase):
    def test_takes_adjacent_food(self):
        w = grid(at=(1, 1))
        w.entities = [Entity("supply", 8, (2, 1), "apple")]
        out = dispatch(w, ctx())
        self.assertEqual(out.state, "Heal")
        self.assertEqual(out.intents, [{"verb": "Take", "supply_id": 8}])

    def test_walks_one_step_toward_distant_food(self):
        w = grid(at=(1, 1))
        w.entities = [Entity("supply", 8, (4, 4), "golden_cap")]
        out = dispatch(w, ctx())
        self.assertEqual(out.intents, [{"verb": "SetPosition", "x": 2, "y": 2}])

    def test_rejected_take_is_not_retried_forever(self):
        w = grid(at=(1, 1))
        w.entities = [Entity("supply", 8, (2, 1), "apple")]
        m = Memory()
        for _ in range(HEAL_MAX_TRIES):
            self.assertEqual(verbs(dispatch(w, ctx(m))), ["Take"])
            w.tick += 7
        self.assertNotIn("Take", verbs(dispatch(w, ctx(m))))

    def test_carried_food_before_potion(self):
        w = grid()
        w.held_supplies = [InventorySupply(4, "small_potion"), InventorySupply(5, "berry")]
        out = dispatch(w, ctx())
        self.assertEqual(out.intents[0], {"verb": "Arm", "supply_id": 5})
        self.assertEqual(verbs(out), ["Arm", "Use"])

    def test_drinks_carried_potion_and_caps_rejected_use(self):
        w = grid()
        w.held_supplies = [InventorySupply(4, "small_potion")]
        m = Memory()
        for _ in range(HEAL_MAX_TRIES):
            self.assertEqual(verbs(dispatch(w, ctx(m))), ["Arm", "Use"])
            w.tick += 7
        self.assertNotIn("Use", verbs(dispatch(w, ctx(m))))

    def test_walks_to_known_safe_tile(self):
        w = grid(at=(2, 2))
        out = dispatch(w, ctx())
        self.assertEqual(out.state, "Heal")
        self.assertEqual(out.intents, [{"verb": "SetPosition", "x": 1, "y": 1}])

    def test_no_safe_tile_yields_to_explore(self):
        w = grid(at=(2, 2))
        w.zones[7] = {}
        m = Memory()
        out = dispatch(w, ctx(m))
        self.assertIsNone(out.intents)
        self.assertEqual(m.heal_backoff_until, w.tick + HEAL_BACKOFF_TICKS)
        w.tick += 7
        self.assertEqual(dispatch(w, ctx(m)).state, "Explore")
        w.tick = m.heal_backoff_until
        self.assertEqual(dispatch(w, ctx(m)).state, "Heal")

    def test_hostile_in_range_yields_to_explore(self):
        w = grid()
        w.entities = [Entity("npc", 3, (2, 2), "slime")]
        self.assertEqual(dispatch(w, ctx()).state, "Explore")

    def test_full_health_yields_to_explore(self):
        w = grid()
        w.health = 10
        self.assertEqual(dispatch(w, ctx()).state, "Explore")

    def test_measures_regen_then_saves_yes(self):
        w = grid(at=(0, 0))
        m, kb = Memory(), KnowledgeBase.empty("sandbox")
        out = dispatch(w, ctx(m, kb))
        self.assertIsNone(out.intents)
        self.assertEqual(out.reason, "measure safe-zone regen")
        w.tick, w.health = 7, 6
        out = dispatch(w, ctx(m, kb))
        self.assertEqual(out.reason, "rest in safe zone")
        self.assertEqual(regen_known(kb, Memory()), "yes")

    def test_regen_absent_is_kept_for_this_run_only(self):
        w = grid(at=(0, 0))
        m, kb = Memory(), KnowledgeBase.empty("sandbox")
        for t in range(0, REGEN_MEASURE_TICKS + 1, 10):
            w.tick = t
            out = dispatch(w, ctx(m, kb))
        self.assertEqual(out.reason, "wait in town, buy potion")
        self.assertTrue(m.heal_regen_absent)
        self.assertIsNone(regen_known(kb, Memory()))
        self.assertNotIn(SURVIVAL_KEY, kb.extra)

    def test_sample_restarts_after_leaving_zone(self):
        w = grid(at=(0, 0))
        m = Memory()
        dispatch(w, ctx(m))
        self.assertIsNotNone(m.heal_regen_sample)
        w.pos, w.tick = (1, 1), 10
        dispatch(w, ctx(m))
        self.assertIsNone(m.heal_regen_sample)
        w.pos, w.tick = (0, 0), REGEN_MEASURE_TICKS + 20
        out = dispatch(w, ctx(m))
        self.assertEqual(out.reason, "measure safe-zone regen")
        self.assertFalse(m.heal_regen_absent)

    def test_sample_restarts_when_health_falls(self):
        w = grid(at=(0, 0))
        m = Memory()
        dispatch(w, ctx(m))
        w.tick, w.health = 10, 4
        dispatch(w, ctx(m))
        self.assertEqual(m.heal_regen_sample, (10, 4, 10))

    def test_rests_when_regen_known(self):
        w = grid(at=(0, 0))
        kb = KnowledgeBase.empty("sandbox")
        save_regen_yes(kb)
        out = dispatch(w, ctx(kb=kb))
        self.assertEqual((out.state, out.intents, out.reason), ("Heal", None, "rest in safe zone"))

    def test_waits_in_town_sends_nothing_and_raises_buy(self):
        w = grid(at=(0, 0))
        m = Memory(heal_regen_absent=True)
        out = dispatch(w, ctx(m))
        self.assertEqual(out.state, "Heal")
        self.assertIsNone(out.intents)
        self.assertEqual(m.buy_signals, [{"op": "buy", "code": "small_potion", "why": "hurt in town, no food or potion"}])
        dispatch(w, ctx(m))
        self.assertEqual(len(m.buy_signals), 1)

    def test_wait_without_health_back_is_bounded(self):
        w = grid(at=(0, 0))
        m = Memory(heal_regen_absent=True)
        dispatch(w, ctx(m))
        w.tick = HEAL_WAIT_TICKS
        out = dispatch(w, ctx(m))
        self.assertEqual(out.reason, "no health back, yield to Explore")
        w.tick += 7
        self.assertEqual(dispatch(w, ctx(m)).state, "Explore")


if __name__ == "__main__":
    unittest.main()
