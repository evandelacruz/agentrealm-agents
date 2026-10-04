"""A11: Recover walks to the death chest only when the spot is safe (A7)."""

import random
import unittest

from agentrealm_agent.brain import decide
from agentrealm_agent.config import Policy
from agentrealm_agent.item_table import InventorySupply
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.memory import Memory
from agentrealm_agent.states import PlayContext, dispatch
from agentrealm_agent.states.recover import recover_approach_target, recover_spot_safe
from agentrealm_agent.world import Entity, WorldModel
from agentrealm_agent.zone_discovery import apply_zone


def full_inventory(w: WorldModel, *, junk: str = "torch") -> None:
    w.armed_code = "pocket_knife"
    w.worn_codes = {}
    w.held_supplies = [InventorySupply(i, junk) for i in range(1, 10)]
    w.chest_supplies = []


def priced(**prices) -> KnowledgeBase:
    kb = KnowledgeBase("sandbox")
    for code, gems in prices.items():
        kb.items[code] = {"gem_price": gems}
    return kb


def world(rows: list[str], at=(0, 0), perception=5) -> WorldModel:
    glyph = {".": "dirt", "#": "wall"}
    w = WorldModel(character_id=1, map_id=7, pos=at, perception=perception)
    for y, row in enumerate(rows):
        for x, g in enumerate(row):
            w.view.tiles[(x, y)] = glyph[g]
    w.terrain_center, w.terrain_map = at, 7
    return w


def scripted(**kw) -> Policy:
    kw.setdefault("pickup", True)
    return Policy(kind="scripted", **kw)


def died_at(w: WorldModel, x: int, y: int, *, map_id=7, chest_id=80) -> None:
    """Die there, then stand back where the test placed us (respawned)."""
    here = w.pos
    w.apply_events(
        [{"tick": 5, "events": [{"kind": "Died", "cause": "killed", "chest_id": chest_id, "map_id": map_id, "x": x, "y": y}]}]
    )
    w.map_id, w.pos = 7, here


def ctx(policy: Policy, m: Memory | None = None, kb: KnowledgeBase | None = None) -> PlayContext:
    return PlayContext(m or Memory(), policy, random.Random(0), knowledge=kb)


class RecoverSafetyTest(unittest.TestCase):
    def test_approach_from_safe_neighbour_when_chest_tile_unsafe(self):
        w = world(["....."], at=(4, 0))
        apply_zone(w, 7, 0, 0, {"safe": False, "brightness": 1})
        apply_zone(w, 7, 1, 0, {"safe": True, "brightness": 1})
        self.assertTrue(recover_spot_safe(w, 7, (0, 0)))
        self.assertEqual(recover_approach_target(w, 7, (0, 0)), (1, 0))

    def test_unsafe_until_zone_known(self):
        w = world(["....."], at=(4, 0))
        self.assertFalse(recover_spot_safe(w, 7, (0, 0)))

    def test_no_walk_without_safe_tile(self):
        w = world(["....."], at=(4, 0))
        w.apply_events(
            [{"tick": 5, "events": [{"kind": "Died", "cause": "killed", "chest_id": 80, "map_id": 7, "x": 0, "y": 0}]}]
        )
        w.map_id, w.pos = 7, (4, 0)
        d = decide(w, Memory(), scripted(goals=["hold"]), random.Random(0))
        self.assertIsNone(d.intent)

    def test_recovers_when_adjacent_tile_safe(self):
        w = world(["....."], at=(4, 0))
        w.apply_events(
            [{"tick": 5, "events": [{"kind": "Died", "cause": "killed", "chest_id": 80, "map_id": 7, "x": 0, "y": 0}]}]
        )
        w.map_id, w.pos = 7, (4, 0)
        apply_zone(w, 7, 1, 0, {"safe": True, "brightness": 1})
        d = decide(w, Memory(), scripted(goals=["hold"]), random.Random(0))
        self.assertEqual((d.intent["verb"], d.intent["x"]), ("SetPosition", 3))


class RecoverDispatchTest(unittest.TestCase):
    def test_recover_beats_explore(self):
        w = world(["....."], at=(4, 0))
        w.apply_events(
            [{"tick": 5, "events": [{"kind": "Died", "cause": "killed", "chest_id": 80, "map_id": 7, "x": 0, "y": 0}]}]
        )
        w.map_id, w.pos = 7, (4, 0)
        apply_zone(w, 7, 1, 0, {"safe": True, "brightness": 1})
        out = dispatch(w, PlayContext(Memory(), scripted(goals=["hold"]), random.Random(0)))
        self.assertEqual(out.state, "Recover")

    def test_pickup_off_does_not_enter_recover(self):
        w = world(["....."], at=(4, 0))
        died_at(w, 0, 0)
        apply_zone(w, 7, 1, 0, {"safe": True, "brightness": 1})
        out = dispatch(w, ctx(scripted(goals=["hold"], pickup=False)))
        self.assertEqual(out.state, "Explore")
        self.assertIsNone(out.intents)

    def test_chest_on_another_map_does_not_enter_recover(self):
        w = world(["....."], at=(4, 0))
        died_at(w, 0, 0, map_id=9)
        apply_zone(w, 9, 1, 0, {"safe": True, "brightness": 1})
        out = dispatch(w, ctx(scripted(goals=["hold"])))
        self.assertEqual(out.state, "Explore")

    def test_unreachable_chest_yields_to_explore_goals(self):
        # A wall cuts the chest off: Recover holds, but the round still moves.
        w = world(["######", "#.#..#", "######"], at=(3, 1))
        died_at(w, 1, 1)
        apply_zone(w, 7, 1, 1, {"safe": True, "brightness": 1})
        out = dispatch(w, ctx(scripted(goals=["goto"], goto=(4, 1))))
        self.assertEqual(out.state, "Recover")
        self.assertIn("chest not reachable", out.reason)
        self.assertEqual((out.intents[0]["verb"], out.intents[0]["x"]), ("SetPosition", 4))

    def test_adjacent_withdraws_when_contents_known(self):
        w = world(["....."], at=(1, 0))
        died_at(w, 0, 0)
        apply_zone(w, 7, 1, 0, {"safe": True, "brightness": 1})
        w.chest_contents[80] = [5, 6]
        out = dispatch(w, ctx(scripted(goals=["hold"])))
        self.assertEqual(out.state, "Recover")
        self.assertEqual(out.intents, [{"verb": "WithdrawFromChest", "chest_id": 80}])

    def test_full_pack_drops_junk_before_death_chest_withdraw(self):
        w = world(["....."], at=(1, 0))
        died_at(w, 0, 0)
        apply_zone(w, 7, 1, 0, {"safe": True, "brightness": 1})
        full_inventory(w)
        w.chest_contents[80] = [InventorySupply(71, "bronze_sword")]
        out = dispatch(w, ctx(scripted(goals=["hold"]), kb=priced(bronze_sword=15)))
        self.assertEqual(out.state, "Recover")
        self.assertEqual(out.intents, [{"verb": "Drop", "supply_id": 1}])

    def test_full_pack_skips_death_chest_when_only_junk(self):
        w = world(["....."], at=(1, 0))
        died_at(w, 0, 0)
        apply_zone(w, 7, 1, 0, {"safe": True, "brightness": 1})
        full_inventory(w)
        w.chest_contents[80] = [InventorySupply(71, "torch")]
        out = dispatch(w, ctx(scripted(goals=["hold"])))
        self.assertEqual(out.state, "Recover")
        self.assertIn("chest not reachable", out.reason)
        self.assertIsNone(out.intents)

    def test_adjacent_waits_to_see_unopened_chest(self):
        w = world(["....."], at=(1, 0))
        died_at(w, 0, 0)
        apply_zone(w, 7, 1, 0, {"safe": True, "brightness": 1})
        out = dispatch(w, ctx(scripted(goals=["hold"])))
        self.assertEqual(out.state, "Recover")
        self.assertIsNone(out.intents)
        self.assertEqual(out.reason, "open chest 80")

    def test_flee_beats_recover(self):
        w = world(["....."], at=(2, 0))
        died_at(w, 0, 0)
        apply_zone(w, 7, 1, 0, {"safe": True, "brightness": 1})
        w.entities = [Entity(id=9, kind="npc", pos=(1, 0))]
        out = dispatch(w, ctx(scripted(goals=["hold"], hostile=["npc"], hostile_range=2, on_hostile="flee")))
        self.assertEqual(out.state, "Flee")
        self.assertTrue(out.reflex)
        self.assertEqual(out.intents[0]["x"], 3)

    def test_emptied_chest_hands_back_to_explore(self):
        w = world(["....."], at=(1, 0))
        died_at(w, 0, 0)
        apply_zone(w, 7, 1, 0, {"safe": True, "brightness": 1})
        m = Memory()
        w.chest_contents[80] = [5]
        self.assertEqual(dispatch(w, ctx(scripted(goals=["hold"]), m)).state, "Recover")
        w.chest_contents[80] = []
        w._refresh_death_chest()
        self.assertIsNone(w.death_chest)
        out = dispatch(w, ctx(scripted(goals=["hold"]), m))
        self.assertEqual((out.state, m.state), ("Explore", "Explore"))


if __name__ == "__main__":
    unittest.main()
