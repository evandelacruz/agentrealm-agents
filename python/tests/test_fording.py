"""A133: water is crossed with a fording supply armed, and never left without one."""

import random
import unittest
from unittest import mock

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

RAFT, SWORD, POTION, MALLET = 7, 8, 9, 10

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
    ids = {"raft": RAFT, "bronze_sword": SWORD, "small_potion": POTION, "bronze_mallet": MALLET}
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

    def test_a_would_strand_refusal_marks_the_cell_it_came_from(self):
        w = lake(at=(1, 1), armed="raft")  # the map shows dirt, the server says water
        m = Memory()
        note_would_strand(w, m, "would_strand")
        w.pos = (0, 1)  # the observation moves us on before the next decision
        note_would_strand(w, m, "would_strand")
        self.assertEqual(list(m.water_cells), [(1, (1, 1)), (1, (0, 1))], "each refused cell, kept")
        w.pos = (1, 1)
        out, why = ford_queue(w, m, [arm(SWORD)])
        self.assertEqual(out, [], "the same Arm is not sent again from there")
        self.assertIn("would strand", why)
        note_would_strand(w, m, "not_held")
        self.assertEqual(len(m.water_cells), 2, "another refusal marks nothing")

    def test_a_cut_arm_leaves_no_memory_of_it(self):
        from agentrealm_agent.states.base import StateOutcome

        w = lake(at=(3, 1), armed="raft")
        m = Memory()

        class ArmsForABreak:
            name = "ArmsForABreak"

            def guard(self, world, ctx):
                return True

            def done(self, world, ctx):
                return False

            def act(self, world, ctx):
                ctx.memory.break_rearm = world.armed_code  # as Break does before its Arm
                ctx.memory.break_pending = (1, (3, 0), "chop")
                return StateOutcome([arm(SWORD), {"verb": "Use", "target": {"kind": "block", "x": 3, "y": 0}}], "break", state=self.name)

        c = ctx_for(m, None)
        with mock.patch("agentrealm_agent.states.dispatch._states", return_value=(ArmsForABreak(),)):
            out = dispatch(w, c)
        self.assertIsNone(out.intents)
        self.assertIn("would strand", "; ".join(out.yielded))
        self.assertIsNone(m.break_rearm, "the Arm was never sent: no re-arm remembered for it")
        self.assertIsNone(m.break_pending)


class AshoreTest(unittest.TestCase):
    def test_never_in_front_of_a_reflex_or_attack_queue(self):
        w = lake(at=(5, 1), armed="raft")
        flee = [{"verb": "Step", "direction": "right"}]
        self.assertEqual(ford_queue(w, Memory(), flee, rearm=False), (flee, ""))
        self.assertEqual(ford_queue(w, Memory(), flee)[0][0], arm(SWORD), "a walk's queue is re-armed")

    def test_not_while_water_lies_on_the_walk_ahead(self):
        w = lake(at=(5, 1), armed="raft")
        m = Memory()
        m.path = [(6, 1), (6, 2), (5, 2), (4, 1)]  # an island hop: back over the water at (4, 1)
        queue = [{"verb": "Step", "direction": "right"}]
        self.assertEqual(ford_queue(w, m, queue), (queue, ""))

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


# A wide lake: land at x 0..1 and 9..10, water between, 10 rows tall, with a
# rock in the lake at (5, 0) a hostile pack may stand on.
WIDE = ["..~~~.~~~.."] + ["..~~~~~~~.."] * 9


def wide(at, armed="raft", rows=WIDE, held=("raft", "bronze_sword")) -> WorldModel:
    w = lake(at=at, armed=armed, held=held)
    w.view.tiles.clear()
    glyph = {".": "dirt", "#": "wall", "~": "water"}
    for y, row in enumerate(rows):
        for x, g in enumerate(row):
            w.view.tiles[(x, y)] = glyph[g]
    w.alive, w.health, w.max_health = True, 10, 10
    return w


def under_attack(w: WorldModel) -> PlayContext:
    from tests.test_hostile_reach import POLICY, pack

    pack(w, (10, 9), hit=6)
    w.attacker, w.attacked_tick = ("npc", 200), w.tick
    return PlayContext(Memory(), POLICY, random.Random(0), knowledge=KnowledgeBase.empty("sandbox"), plan=travel_to(10, 1))


class AttackedOnWaterTest(unittest.TestCase):
    def test_makes_for_the_nearest_land_without_arming_a_weapon(self):
        w = wide(at=(3, 5))
        out = dispatch(w, under_attack(w))
        self.assertEqual(out.state, "Ashore", out.reason)
        (step,) = out.intents
        self.assertEqual((step["verb"], step["x"]), ("SetPosition", 2), "toward the west shore at x 1, not a weapon")

    def test_not_with_the_fording_supply_worn(self):
        w = wide(at=(3, 5), armed="bronze_sword")
        w.worn_codes = {"accessory": "raft"}
        self.assertNotEqual(dispatch(w, under_attack(w)).state, "Ashore")

    def test_fights_or_flees_normally_once_ashore(self):
        w = wide(at=(1, 5))
        self.assertNotEqual(dispatch(w, under_attack(w)).state, "Ashore")


class WaterIsGroundEverywhereTest(unittest.TestCase):
    def test_reading_the_view_changes_nothing(self):
        w = lake(armed="raft")
        view = w.view
        self.assertTrue(w.walkable((3, 1)))
        self.assertFalse(view.walkable((3, 1)), "the view keeps no fording flag")
        self.assertTrue(view.walkable((3, 1), fords=True))

    def test_a_route_through_a_door_crosses_water(self):
        from agentrealm_agent.knowledge_maps import record_warp
        from agentrealm_agent.navigation.door_graph import route_first_leg

        kb = KnowledgeBase.empty("sandbox")
        record_warp(kb, 1, (1, 1), "framed_door", 2, (0, 1))
        known = CostGridParams(fog_cost=None)

        def world(held):
            w = lake(at=(0, 1), held=held)
            w.maps[2] = w.maps.pop(1)  # the lake is on map 2
            w.map_id = 1
            w.view.tiles.update({(0, 1): "dirt", (1, 1): "framed_door"})
            return w

        self.assertIsNone(route_first_leg(world(("bronze_sword",)), kb, 2, (6, 1), known))
        self.assertEqual(route_first_leg(world(("raft", "bronze_sword")), kb, 2, (6, 1), known), [(1, 1)])

    def test_the_waters_edge_is_a_frontier_with_a_fording_supply(self):
        w = lake(held=("bronze_sword",))
        self.assertNotIn((2, 1), w.frontier())
        w = lake()
        frontier = w.frontier()
        self.assertTrue(any(w.view.tiles[p] == "water" for p in frontier), frontier)
        self.assertFalse(any(w.view.tiles[p] == "water" for p in w.view.frontier()), "kept apart per fords")


class FightAshoreTest(unittest.TestCase):
    def test_landed_with_the_raft_in_hand_fight_arms_the_weapon_first(self):
        from agentrealm_agent.executor import attack_cooldown_ticks
        from tests.test_hostile_reach import POLICY, pack

        w = wide(at=(9, 5), held=("raft", "bronze_mallet"))  # a weapon slower than the 1 s default
        (rat,) = pack(w, (10, 5), hit=1)
        w.attacker, w.attacked_tick = ("npc", rat.id), w.tick
        m = Memory()
        m.calm_poll_interval = 100  # room for two swings in the queue
        c = PlayContext(m, POLICY, random.Random(0), knowledge=KnowledgeBase.empty("sandbox"), plan=travel_to(10, 1))
        out = dispatch(w, c)
        self.assertEqual(out.state, "Fight", out.reason)
        self.assertEqual(out.intents[0], arm(MALLET), "the raft swings nothing")
        uses = [i for i, x in enumerate(out.intents) if x.get("verb") == "Use"]
        self.assertGreaterEqual(len(uses), 2, out.intents)
        gap = attack_cooldown_ticks("bronze_mallet", c.tick_hz)
        self.assertNotEqual(gap, attack_cooldown_ticks("raft", c.tick_hz), "the test tells the two apart")
        self.assertEqual(uses[1] - uses[0], gap, "swings paced by the weapon's cooldown, not the raft's")

    def test_on_water_no_weapon_is_armed_for_a_fight(self):
        from tests.test_hostile_reach import POLICY, pack

        w = wide(at=(8, 5))
        (rat,) = pack(w, (9, 5), hit=1)
        w.attacker, w.attacked_tick = ("npc", rat.id), w.tick
        c = PlayContext(Memory(), POLICY, random.Random(0), knowledge=KnowledgeBase.empty("sandbox"), plan=None)
        out = dispatch(w, c)
        self.assertNotIn("Arm", [i.get("verb") for i in out.intents or []], out.reason)

    def test_an_escape_is_never_slowed_by_the_rearm(self):
        from agentrealm_agent.fording import ESCAPES

        self.assertEqual(set(ESCAPES), {"Flee", "Retreat", "Escape"})


class CrossingKeepsOffReachTest(unittest.TestCase):
    def test_a_crossing_keeps_off_water_in_a_seen_hostiles_reach(self):
        from agentrealm_agent.pathing import op_walk_sets
        from tests.test_hostile_reach import BAR, POLICY, pack

        w = wide(at=(0, 3))
        (rock,) = pack(w, (5, 0), hit=6)  # one the engagement would not take on
        avoid, costly, _ = op_walk_sets(w, Memory(), POLICY, None, dict(PARAM_DEFAULTS), [])
        path = cost_path(w, (10, 3), CostGridParams(avoid=avoid, costly=costly, fog_cost=None))
        self.assertIsNotNone(path)
        water = [p for p in path if w.view.tiles[p] == "water"]
        self.assertTrue(water)
        self.assertTrue(all(max(abs(p[0] - rock.pos[0]), abs(p[1] - rock.pos[1])) > BAR for p in water), path)


class EquipRaftTest(unittest.TestCase):
    def test_the_planners_equip_op_arms_the_raft(self):
        w = lake(at=(1, 1), armed="bronze_sword")
        plan = Plan([{"op": "equip", "code": "raft"}], dict(PARAM_DEFAULTS))
        out = dispatch(w, ctx_for(Memory(), plan))
        self.assertEqual((out.state, out.intents), ("Equip", [arm(RAFT)]), out.reason)
        self.assertIsNotNone(plan.current(), "not dropped as not armed or worn")
