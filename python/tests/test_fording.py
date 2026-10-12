"""A133: water is crossed with a fording supply armed, and never left without one."""

import random
import unittest

from agentrealm_agent.config import Policy
from agentrealm_agent.directives import PARAM_DEFAULTS
from agentrealm_agent.fording import ford_queue, note_would_strand
from agentrealm_agent.item_table import InventorySupply
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.memory import Memory
from agentrealm_agent.navigation.planner import CostGridParams, cost_path
from agentrealm_agent.plan import Plan
from agentrealm_agent.states import PlayContext, dispatch
from agentrealm_agent.supplies import fords, what_it_does
from agentrealm_agent.strategist import held_line
from agentrealm_agent.world import WorldModel

RAFT, SWORD, POTION = 7, 8, 9

# A lake between the shore at x 0..1 and the far shore at x 5..6.
LAKE = [
    "##~~~##",
    "..~~~..",
    "##~~~##",
]


def lake(at=(0, 1), held=("raft", "bronze_sword"), armed: str | None = None) -> WorldModel:
    glyph = {".": "dirt", "#": "wall", "~": "water"}
    w = WorldModel(character_id=1, map_id=1, pos=at, perception=5)
    for y, row in enumerate(LAKE):
        for x, g in enumerate(row):
            w.view.tiles[(x, y)] = glyph[g]
    w.terrain_center, w.terrain_map = at, 1
    ids = {"raft": RAFT, "bronze_sword": SWORD, "small_potion": POTION}
    w.held_supplies = [InventorySupply(ids[c], c) for c in held if c != armed]
    if armed is not None:
        w.armed_code, w.armed_id = armed, ids[armed]
    w.self_read = w.inventory_read = True
    return w


def travel_to(x: int, y: int) -> Plan:
    return Plan([{"op": "travel", "to": "point", "x": x, "y": y}], dict(PARAM_DEFAULTS))


def ctx_for(m: Memory, plan: Plan | None) -> PlayContext:
    return PlayContext(m, Policy(kind="scripted"), random.Random(0), knowledge=KnowledgeBase.empty("sandbox"), plan=plan)


def arm(sid: int) -> dict:
    return {"verb": "Arm", "supply_id": sid}


class FordsTest(unittest.TestCase):
    def test_the_raft_fords_and_the_planner_is_told(self):
        self.assertTrue(fords("raft"))
        self.assertFalse(fords("bronze_sword"))
        self.assertTrue(what_it_does("raft")["fords"])
        self.assertIn('"fords": true', held_line(lake()))


class PathTest(unittest.TestCase):
    def test_a_path_across_water_only_with_a_fording_supply_held(self):
        known = CostGridParams(fog_cost=None)  # no way round through unseen ground
        self.assertIsNone(cost_path(lake(held=("bronze_sword",)), (6, 1), known), "water is no ground without one")
        path = cost_path(lake(), (6, 1), known)
        self.assertIsNotNone(path, "holding a raft is enough to plan through")
        self.assertTrue(any(lake().view.tiles[p] == "water" for p in path), path)
        self.assertIsNotNone(cost_path(lake(held=(), armed="raft"), (6, 1), known), "or arming it")


class WalkTest(unittest.TestCase):
    def test_the_walk_arms_the_raft_before_the_first_water_step(self):
        w = lake(at=(1, 1), armed="bronze_sword")
        out = dispatch(w, ctx_for(Memory(), travel_to(6, 1)))
        self.assertEqual(out.state, "Travel", out.reason)
        self.assertEqual(out.intents[0], arm(RAFT), "armed before stepping onto (2, 1)")
        self.assertEqual(len([i for i in out.intents if i.get("verb") == "Arm"]), 1)

    def test_no_arm_when_the_raft_is_armed(self):
        w = lake(at=(1, 1), armed="raft")
        out = dispatch(w, ctx_for(Memory(), travel_to(6, 1)))
        self.assertEqual(out.state, "Travel", out.reason)
        self.assertNotIn("Arm", [i.get("verb") for i in out.intents])

    def test_without_a_raft_the_queue_stops_short_of_the_water(self):
        w = lake(at=(1, 1), held=("bronze_sword",))
        queue, why = ford_queue(w, Memory(), [{"verb": "Step", "direction": "right"}])
        # Nothing fords: water is not ground, so no walk plans onto it.
        self.assertEqual((queue, why), ([{"verb": "Step", "direction": "right"}], ""))


class OnWaterTest(unittest.TestCase):
    def test_no_rearm_while_on_water(self):
        w = lake(at=(3, 1), held=("raft", "bronze_sword", "small_potion"), armed="raft")
        m = Memory()
        for queue in ([arm(SWORD)], [arm(POTION), {"verb": "Use", "target": {"kind": "self"}}], [{"verb": "Drop", "supply_id": RAFT}]):
            out, why = ford_queue(w, m, queue)
            self.assertEqual(out, [], queue)
            self.assertIn("would strand", why)

    def test_a_step_then_an_arm_ashore_is_kept(self):
        w = lake(at=(4, 1), armed="raft")
        queue = [{"verb": "Step", "direction": "right"}, arm(SWORD)]
        self.assertEqual(ford_queue(w, Memory(), queue), (queue, ""))

    def test_a_worn_fording_supply_leaves_the_armed_slot_free(self):
        w = lake(at=(3, 1))
        w.worn_codes = {"accessory": "raft"}  # a worn fording supply (the rule is by capability, not slot)
        self.assertEqual(ford_queue(w, Memory(), [arm(SWORD)]), ([arm(SWORD)], ""))

    def test_equip_on_water_falls_through_without_a_loop(self):
        w = lake(at=(3, 1), armed="raft")
        m = Memory()
        plan = Plan([{"op": "equip", "code": "bronze_sword"}], dict(PARAM_DEFAULTS))
        for _ in range(3):
            out = dispatch(w, ctx_for(m, plan))
            self.assertNotIn("Arm", [i.get("verb") for i in out.intents or []], out.reason)

    def test_a_would_strand_refusal_marks_the_cell_as_water(self):
        w = lake(at=(1, 1), armed="raft")  # the map shows dirt, the server says water
        m = Memory()
        note_would_strand(m, "would_strand")
        dispatch(w, ctx_for(m, None))
        self.assertEqual(m.water_at, (1, (1, 1)))
        out, why = ford_queue(w, m, [arm(SWORD)])
        self.assertEqual(out, [], "the same Arm is not sent again from there")
        self.assertIn("would strand", why)


class AshoreTest(unittest.TestCase):
    def test_the_weapon_is_armed_again_after_landing(self):
        w = lake(at=(5, 1), armed="raft")
        out = dispatch(w, ctx_for(Memory(), travel_to(6, 1)))
        self.assertEqual(out.intents[0], arm(SWORD), out.reason)

    def test_not_while_the_next_step_is_water(self):
        w = lake(at=(1, 1), armed="raft")
        out = dispatch(w, ctx_for(Memory(), travel_to(6, 1)))
        self.assertNotIn("Arm", [i.get("verb") for i in out.intents], out.reason)


if __name__ == "__main__":
    unittest.main()
