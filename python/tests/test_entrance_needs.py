"""A147: a level's clue names the item it needs; he gets it first, and a worn ford beats an armed one."""

import random
import unittest
from unittest import mock

from agentrealm_agent import supplies
from agentrealm_agent.clues import record_clue
from agentrealm_agent.config import Policy
from agentrealm_agent.directives import PARAM_DEFAULTS
from agentrealm_agent.entrance_needs import refuse_unmet, unmet_need
from agentrealm_agent.fording import ford_queue
from agentrealm_agent.item_table import InventorySupply
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.memory import Memory
from agentrealm_agent.plan import Plan
from agentrealm_agent.states import PlayContext, dispatch
from agentrealm_agent.strategist import build_prompt
from agentrealm_agent.travel.knowledge import entrance_key
from agentrealm_agent.world import Entity, WorldModel
from tests.test_fording import RAFT, SWORD, lake
from tests.test_strategist import FakeLLM, fake_runner, make, round_trip

SANDALS = 11
# Sandals that walk on water, worn on the feet (the bundled reference does not list them).
SANDALS_ROW = {
    "code": "sea_sandals",
    "class": "armor",
    "slot": "feet",
    "capabilities": [{"capability": "fords", "trigger": "equipped"}],
    "gem_prices": {"sandbox": 40},
}
LAKE_DOOR = (12, 3)
CLUE = "The lake door wants dry feet or wet ones. A raft gets you there. Sandals get you through."


def with_sandals():
    table = dict(supplies._table)
    table.update(supplies.parse([SANDALS_ROW]))
    return mock.patch.object(supplies, "_table", table)


def kb_with_door() -> KnowledgeBase:
    kb = KnowledgeBase.empty("sandbox")
    kb.entrances[entrance_key(7, LAKE_DOOR)] = {"map_id": 7, "x": LAKE_DOOR[0], "y": LAKE_DOOR[1]}
    return kb


def needs_sandals() -> KnowledgeBase:
    kb = kb_with_door()
    kb.entrances[entrance_key(7, LAKE_DOOR)]["needs"] = "sea_sandals"
    return kb


def ground(at=(2, 3)) -> WorldModel:
    w = WorldModel(character_id=1, map_id=7, pos=at, perception=5, tick=10)
    for x in range(16):
        for y in range(7):
            w.view.tiles[(x, y)] = "dirt"
    w.view.tiles[LAKE_DOOR] = "framed_door"
    w.terrain_center, w.terrain_map = at, 7
    w.alive = w.self_read = w.inventory_read = True
    return w


class ClueSetsTheNeedTest(unittest.TestCase):
    def test_a_clue_naming_a_need_sets_it_on_the_entrance(self):
        runner = fake_runner()
        runner.knowledge = kb_with_door()
        record_clue(runner.knowledge, runner.mem, kind="sign", text=CLUE, map_id=7, x=10, y=3, tick=5)
        reply = {
            "entrance_needs": [{"x": 12, "y": 3, "map_id": 7, "item": "sea_sandals", "why": "Sandals get you through"}],
            "goals": [{"op": "explore_area", "x": 0, "y": 0, "radius": 30}],
        }
        llm = FakeLLM(reply)
        s = make(client=llm)
        round_trip(s, runner)
        sent = llm.messages[0][1]["content"]
        self.assertIn(CLUE, sent, "the clue reaches the planner")
        self.assertIn("entrance_needs", llm.messages[0][0]["content"], "the contract tells it how to record one")
        row = runner.knowledge.entrances[entrance_key(7, LAKE_DOOR)]
        self.assertEqual((row["needs"], row["clue"]), ("sea_sandals", "Sandals get you through"))
        prompt = build_prompt(
            triggers=[], w=runner.world, plan=runner.plan, directives=runner.directives.directives, knowledge=runner.knowledge
        )[1]["content"]
        self.assertIn('"need_met": false', prompt)
        self.assertIn('"needs": "sea_sandals"', prompt)

    def test_a_need_on_no_known_entrance_is_refused_with_why(self):
        runner = fake_runner()
        runner.knowledge = kb_with_door()
        s = make(client=FakeLLM({"entrance_needs": [{"x": 1, "y": 1, "map_id": 7, "item": "sea_sandals"}]}))
        round_trip(s, runner)
        self.assertTrue(any("no known entrance" in r for r in s.rejected), s.rejected)

    def test_null_clears_a_need(self):
        runner = fake_runner()
        runner.knowledge = needs_sandals()
        s = make(client=FakeLLM({"entrance_needs": [{"x": 12, "y": 3, "map_id": 7, "item": None}]}))
        round_trip(s, runner)
        self.assertNotIn("needs", runner.knowledge.entrances[entrance_key(7, LAKE_DOOR)])


class GetTheNeedFirstTest(unittest.TestCase):
    def test_refused_while_unmet_and_allowed_once_held(self):
        enter = {"op": "enter_level", "x": 12, "y": 3}
        travel = {"op": "travel", "to": "entrance", "x": 12, "y": 3, "map_id": 7}
        with with_sandals():
            w, kb = ground(), needs_sandals()
            w.held_supplies = [InventorySupply(RAFT, "raft")]
            self.assertEqual(unmet_need(w, kb, 7, LAKE_DOOR), "sea_sandals", "a raft is no pair of sandals")
            kept, refused = refuse_unmet([enter, travel], w, kb)
            self.assertEqual(kept, [])
            self.assertEqual(len(refused), 2)
            self.assertIn("needs sea_sandals", refused[0])
            self.assertIn("get it first", refused[0])
            w.held_supplies.append(InventorySupply(SANDALS, "sea_sandals"))
            self.assertIsNone(unmet_need(w, kb, 7, LAKE_DOOR))
            self.assertEqual(refuse_unmet([enter, travel], w, kb), ([enter, travel], []))

    def test_the_reply_is_refused_and_the_planner_told(self):
        runner = fake_runner()
        runner.knowledge = needs_sandals()
        s = make(client=FakeLLM({"goals": [{"op": "enter_level", "x": 12, "y": 3}]}))
        round_trip(s, runner)
        self.assertEqual([g["op"] for g in runner.plan.goals], [])
        self.assertTrue(any("needs sea_sandals" in r for r in s.rejected), s.rejected)

    def test_a_need_the_reply_records_refuses_its_own_enter_level(self):
        runner = fake_runner()
        runner.knowledge = kb_with_door()
        reply = {
            "entrance_needs": [{"x": 12, "y": 3, "map_id": 7, "item": "sea_sandals"}],
            "goals": [{"op": "enter_level", "x": 12, "y": 3}],
        }
        s = make(client=FakeLLM(reply))
        round_trip(s, runner)
        self.assertNotIn("enter_level", [g["op"] for g in runner.plan.goals])

    def test_level_drops_an_enter_level_whose_need_is_unmet(self):
        w, kb, m = ground(), needs_sandals(), Memory()
        plan = Plan([{"op": "enter_level", "x": 12, "y": 3}], dict(PARAM_DEFAULTS))
        ctx = PlayContext(m, Policy(kind="scripted"), random.Random(0), knowledge=kb, plan=plan)
        dispatch(w, ctx)
        self.assertIsNone(plan.current())
        failed = [t for t in m.strategist_signals if t["trigger"] == "goal_failed"]
        self.assertIn("needs sea_sandals", failed[-1]["reason"])

    def test_level_walks_once_the_need_is_held(self):
        with with_sandals():
            w, kb, m = ground(), needs_sandals(), Memory()
            w.held_supplies = [InventorySupply(SANDALS, "sea_sandals")]
            plan = Plan([{"op": "enter_level", "x": 12, "y": 3}], dict(PARAM_DEFAULTS))
            ctx = PlayContext(m, Policy(kind="scripted"), random.Random(0), knowledge=kb, plan=plan)
            out = dispatch(w, ctx)
            self.assertEqual(out.state, "Level", out.reason)
            self.assertIsNotNone(out.intents)

    def test_the_item_in_sight_is_taken_on_the_way(self):
        with with_sandals():
            w, kb, m = ground(), needs_sandals(), Memory()
            w.entities = [Entity("supply", SANDALS, (2, 5), "sea_sandals")]
            plan = Plan([{"op": "enter_level", "x": 12, "y": 3}], dict(PARAM_DEFAULTS))
            self.assertEqual(refuse_unmet(plan.goals, w, kb)[1], [], "in sight: Level takes it first")
            ctx = PlayContext(m, Policy(kind="scripted"), random.Random(0), knowledge=kb, plan=plan)
            out = dispatch(w, ctx)
            self.assertEqual(out.state, "Level", out.reason)
            self.assertIn("sea_sandals", out.reason)


class WornFordTest(unittest.TestCase):
    def test_with_sandals_and_a_raft_the_sandals_are_worn_and_the_sword_stays_armed(self):
        with with_sandals():
            w = lake(at=(1, 1), held=("raft", "bronze_sword"), armed="bronze_sword")
            w.held_supplies.append(InventorySupply(SANDALS, "sea_sandals"))
            out, why = ford_queue(w, Memory(), [{"verb": "Step", "direction": "right"}])
            self.assertEqual(why, "")
            self.assertEqual(out, [{"verb": "Wear", "supply_id": SANDALS}, {"verb": "Step", "direction": "right"}])
            self.assertNotIn(RAFT, [i.get("supply_id") for i in out if i["verb"] == "Arm"])

    def test_the_equip_op_wears_the_sandals_and_keeps_the_sword(self):
        with with_sandals():
            w = lake(at=(0, 1), held=("raft", "bronze_sword"), armed="bronze_sword")
            w.held_supplies.append(InventorySupply(SANDALS, "sea_sandals"))
            w.alive = True
            for code in ("raft", None):
                op = {"op": "equip", "code": code} if code else {"op": "equip"}
                plan = Plan([op], dict(PARAM_DEFAULTS))
                ctx = PlayContext(Memory(), Policy(kind="scripted"), random.Random(0), knowledge=KnowledgeBase.empty("sandbox"), plan=plan)
                out = dispatch(w, ctx)
                self.assertEqual((out.state, out.intents), ("Equip", [{"verb": "Wear", "supply_id": SANDALS}]), op)
            self.assertEqual(w.armed_code, "bronze_sword")

    def test_on_water_with_the_sandals_worn_the_sword_may_be_armed(self):
        with with_sandals():
            w = lake(at=(3, 1), held=("raft", "bronze_sword"))
            w.worn_codes = {"feet": "sea_sandals"}
            arm = {"verb": "Arm", "supply_id": SWORD}
            self.assertEqual(ford_queue(w, Memory(), [arm]), ([arm], ""), "Fight runs as on land")

    def test_with_only_the_raft_it_is_armed_as_before(self):
        with with_sandals():
            w = lake(at=(1, 1), held=("raft", "bronze_sword"), armed="bronze_sword")
            out, _ = ford_queue(w, Memory(), [{"verb": "Step", "direction": "right"}])
            self.assertEqual(out[0], {"verb": "Arm", "supply_id": RAFT})


if __name__ == "__main__":
    unittest.main()
