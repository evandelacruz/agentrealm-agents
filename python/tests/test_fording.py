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


class CutMemoryTest(unittest.TestCase):
    def test_a_kept_arm_before_a_cut_step_keeps_its_memory(self):
        from agentrealm_agent.fording import dropped_loadout
        from agentrealm_agent.states.base import StateOutcome

        queue = [arm(SWORD), {"verb": "Step", "direction": "right"}]
        self.assertFalse(dropped_loadout(queue, queue[:1]), "only the Step was cut")
        self.assertTrue(dropped_loadout(queue, []), "the Arm was cut")

        class ArmsThenSteps:
            name = "ArmsThenSteps"

            def guard(self, world, ctx):
                return True

            def done(self, world, ctx):
                return False

            def act(self, world, ctx):
                ctx.memory.break_rearm = "raft"  # as Break does before its Arm
                ctx.memory.break_pending = (1, (2, 0), "chop")
                return StateOutcome(list(queue), "break", state=self.name)

        w, m = lake(at=(1, 1), armed="raft"), Memory()
        keep_the_arm = lambda w_, m_, q, kb=None, rearm=True: (q[:1], "Step cut")  # noqa: E731
        with mock.patch("agentrealm_agent.fording.ford_queue", side_effect=keep_the_arm):
            with mock.patch("agentrealm_agent.states.dispatch._states", return_value=(ArmsThenSteps(),)):
                out = dispatch(w, ctx_for(m, None))
        self.assertEqual(out.intents, [arm(SWORD)])
        self.assertEqual(m.break_rearm, "raft", "the Arm went out: its re-arm is kept")
        self.assertEqual(m.break_pending, (1, (2, 0), "chop"))


class UnderAttackStepTest(unittest.TestCase):
    def test_a_cornered_drink_on_water_leaves_no_memory_of_it(self):
        import tempfile
        import threading
        from pathlib import Path

        from agentrealm_agent.brain import Decision
        from agentrealm_agent.config import CharacterConfig
        from agentrealm_agent.runner import Runner
        from agentrealm_agent.world import Entity
        from tests.test_hostile_reach import POLICY
        from tests.test_runner import FakeClient

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        (Path(tmp.name) / "T.directives.toml").write_text("")
        r = Runner(CharacterConfig("T", "sandbox", POLICY, Path(tmp.name) / "T.toml"), FakeClient([]), 1, threading.Event(), out=lambda _: None)
        self.addCleanup(r.trace.close)
        r.world = lake(at=(3, 1), held=("raft", "bronze_sword", "small_potion"), armed="raft")
        r.mem = m = Memory()
        drink = [arm(POTION), {"verb": "Use", "target": {"kind": "self"}}]

        def cornered(w, ctx, hostiles):
            ctx.memory.self_use, ctx.memory.self_use_rearm = POTION, "raft"  # as use_on_self does
            return Decision(drink[0], "cornered: drink", True, list(drink), "Flee")

        hitter = Entity("npc", 200, (4, 0), code="bogmaw")
        walk = [{"verb": "Step", "direction": "right"}]
        with mock.patch("agentrealm_agent.runner.cornered_decision", side_effect=cornered), mock.patch(
            "agentrealm_agent.runner.open_step", return_value=None
        ), mock.patch("agentrealm_agent.runner.flee_from", return_value=[hitter]):
            d, sent, doubt = r.move_under_attack(Decision(walk[0], "walk", True, state="Travel"), walk, [(3, 1)])
        self.assertNotIn(arm(POTION), sent or [], "no drink on water: it would strand")
        self.assertIsNone(m.self_use, "the drink was never sent: nothing is filed against it")
        self.assertIsNone(m.self_use_rearm)

    def test_cornered_on_water_skips_the_drink_and_still_acts(self):
        from agentrealm_agent.states.flee import cornered
        from agentrealm_agent.world import Entity
        from tests.test_hostile_reach import POLICY

        w = lake(at=(3, 1), held=("raft", "bronze_sword", "small_potion"), armed="raft")
        w.alive, w.health, w.max_health = True, 2, 10  # a drink is due
        hitter = Entity("npc", 200, (4, 1), code="bogmaw")
        w.threat.record(("npc", "bogmaw"), 3)
        w.entities = [hitter]
        w.attacker, w.attacked_tick = ("npc", 200), w.tick
        m = Memory()
        c = PlayContext(m, POLICY, random.Random(0), knowledge=KnowledgeBase.empty("sandbox"), plan=None)
        out = cornered(w, c, [hitter])
        self.assertNotIn(arm(POTION), out.intents or [], "the drink's Arm would strand us")
        self.assertIsNone(m.self_use)
        self.assertTrue(out.intents or (out.wait and any(s["trigger"] == "cornered" for s in m.strategist_signals)), out.reason)

    def test_a_lone_step_under_attack_is_open_on_water_only_afloat(self):
        from agentrealm_agent.executor.held_queue import _lands_open

        held = lake(at=(1, 1), armed="bronze_sword")
        self.assertTrue(held.walkable((2, 1)), "a walk plans over it with the raft held")
        self.assertFalse(_lands_open(held, set(), (2, 1)), "but a lone Step has no Arm before it")
        armed = lake(at=(1, 1), armed="raft")
        self.assertTrue(_lands_open(armed, set(), (2, 1)))


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
        self.assertFalse(view.walkable((3, 1), fords=False), "the view keeps no fording flag")
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
        self.assertFalse(any(w.view.tiles[p] == "water" for p in w.view.frontier(fords=False)), "kept apart per fords")


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
        w = lake(at=(5, 1), armed="raft")
        w.view.tiles[(5, 1)] = "lava"  # Escape steps off it
        out = dispatch(w, ctx_for(Memory(), None))
        self.assertEqual(out.state, "Escape", out.reason)
        self.assertNotEqual(out.intents[0].get("verb"), "Arm", out.intents)

    def test_a_reflex_that_is_no_escape_gets_the_rearm(self):
        from agentrealm_agent.world import Entity

        w = lake(at=(5, 1), armed="raft")
        w.entities = [Entity("supply", 77, (6, 1), code="small_potion")]
        out = dispatch(w, ctx_for(Memory(), None))
        self.assertEqual(out.state, "Pickup", out.reason)
        self.assertEqual(out.intents[0], arm(SWORD), out.intents)


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


# A146: a level map that is almost all water, with dirt islands at its corners.
def level_lake(at, armed: str | None = "raft", held=("raft", "bronze_sword")) -> WorldModel:
    rows = ["~" * 24] * 24
    w = wide(at=at, armed=armed, held=held, rows=rows)
    for p in [(1, 1), (2, 1), (1, 2), (21, 21), (22, 21), (21, 22)]:
        w.view.tiles[p] = "dirt"
    w.map_id, w.map_level = 9, 2  # an interior map: a level
    w.maps = {9: w.maps[1]}
    return w


class LevelWalkTest(unittest.TestCase):
    def test_a_walk_inside_a_level_crosses_water_with_the_raft_armed(self):
        w = level_lake(at=(10, 10))
        out = dispatch(w, ctx_for(Memory(), travel_to(21, 21)))
        self.assertEqual(out.state, "Travel", out.reason)
        self.assertEqual(out.intents, [{"verb": "SetPosition", "x": 11, "y": 11}], out.reason)

    def test_the_same_walk_with_no_ford_finds_no_way(self):
        w = level_lake(at=(1, 1), armed="bronze_sword", held=("bronze_sword",))
        m = Memory()
        out = dispatch(w, ctx_for(m, travel_to(21, 21)))
        self.assertFalse(out.intents, out.reason)
        self.assertIn("no step toward it", out.reason)
        self.assertEqual([s["trigger"] for s in m.strategist_signals], ["no_way_on"])


class AfloatEngagementTest(unittest.TestCase):
    def engaged(self, w: WorldModel, m: Memory | None = None):
        from agentrealm_agent.engagement import sync_engagement
        from tests.test_hostile_reach import POLICY, pack

        if not w.entities:
            (rat,) = pack(w, (w.pos[0] + 1, w.pos[1]), hit=1)  # one the estimate beats with a weapon
            w.attacker, w.attacked_tick = ("npc", rat.id), w.tick
        m = m or Memory()
        return m, sync_engagement(w, m, POLICY, dict(PARAM_DEFAULTS))

    def test_afloat_with_the_raft_and_a_hostile_adjacent_it_is_not_fight(self):
        _, e = self.engaged(level_lake(at=(10, 10)))
        self.assertIsNotNone(e)
        self.assertFalse(e.fight, "the raft swings nothing, and no weapon can be armed on water")

    def test_ashore_the_same_hostile_is_fought(self):
        _, e = self.engaged(level_lake(at=(1, 1)))
        self.assertTrue(e.fight, "on land the weapon is armed for the fight (A133)")

    def test_a_fight_decided_ashore_does_not_hold_once_afloat(self):
        w = level_lake(at=(2, 1))
        m, e = self.engaged(w)
        self.assertTrue(e.fight)
        w.pos = (3, 1)  # stepped onto water, the hostile follows
        w.entities[0].pos = (4, 1)
        _, e = self.engaged(w, m)
        self.assertFalse(e.fight)

    def test_the_decision_afloat_is_never_fight(self):
        from tests.test_hostile_reach import POLICY

        w = level_lake(at=(10, 10))
        self.engaged(w)
        c = PlayContext(Memory(), POLICY, random.Random(0), knowledge=KnowledgeBase.empty("sandbox"), plan=None)
        out = dispatch(w, c)
        self.assertNotEqual(out.state, "Fight", out.reason)
        self.assertFalse(c.memory.engagement.fight)
        self.assertNotIn("Use", [i.get("verb") for i in out.intents or []], "no swing from the water")


class FightClosesOnlyOnLandTest(unittest.TestCase):
    def test_no_raft_is_armed_to_close_on_a_hostile_across_water(self):
        """A146: Fight closes only over ground it can swing from, so with the
        raft held, the sword armed and a hostile across water with no way
        round by land, there is no Arm of the raft and no Fight."""
        from tests.test_hostile_reach import POLICY, pack

        w = level_lake(at=(1, 1), armed="bronze_sword")
        w.view.tiles.update({(2, 1): "water", (1, 2): "water", (3, 1): "dirt"})  # our islet and its
        (rat,) = pack(w, (3, 1), hit=1)  # one the estimate would fight, on its own islet
        plan = Plan([{"op": "wait", "seconds": 30, "why": "hold here"}], dict(PARAM_DEFAULTS))  # no walk of its own
        c = PlayContext(Memory(), POLICY, random.Random(0), knowledge=KnowledgeBase.empty("sandbox"), plan=plan)
        out = dispatch(w, c)
        self.assertTrue(c.memory.engagement.fight, "the estimate would take it on, from land")
        from agentrealm_agent.fording import ESCAPES
        from agentrealm_agent.states.fight import close_step

        self.assertIsNone(close_step(w, rat, set()), "no walk in over ground it can swing from")
        self.assertNotEqual(out.state, "Fight", out.reason)
        if arm(RAFT) in (out.intents or []):
            self.assertIn(out.state, ESCAPES, "only an escape takes to the water, never a walk in to swing")


class WaterCellsOnlyTest(unittest.TestCase):
    def test_a_cell_known_as_water_only_from_a_refusal_is_afloat_to_the_engagement(self):
        """A146: the engagement's afloat test is Ashore's (``on_water``), so a
        cell a ``would_strand`` marked counts though the map shows ground."""
        from agentrealm_agent.engagement import sync_engagement
        from tests.test_hostile_reach import POLICY, pack

        w = level_lake(at=(1, 1))  # dirt on the map
        (rat,) = pack(w, (2, 2), hit=1)
        w.attacker, w.attacked_tick = ("npc", rat.id), w.tick
        m = Memory()
        self.assertTrue(sync_engagement(w, m, POLICY, dict(PARAM_DEFAULTS)).fight, "ground: fought")
        m.engagement = None
        m.water_cells[(w.map_id, (1, 1))] = None  # a would_strand came from it
        self.assertFalse(sync_engagement(w, m, POLICY, dict(PARAM_DEFAULTS)).fight)
