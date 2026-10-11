"""A114: the planner can choose a fight, and a cornered character never stands taking hits.

- The ``fight`` op names an NPC; Fight closes on it and swings. No attack is
  made from a safe zone, so on a safe tile Fight's move is the step off it.
- Cornered (no step opens distance), Flee drinks a heal that is due, else
  swings at a hostile in reach, else asks the planner once.
- State ``nearby_npcs`` gives each NPC's cell and its swing next to ours.
"""

from __future__ import annotations

import random
import unittest

from agentrealm_agent.config import Policy
from agentrealm_agent.directives import PARAM_DEFAULTS, Directives
from agentrealm_agent.item_table import InventorySupply
from agentrealm_agent.memory import Memory
from agentrealm_agent.plan import OP_FIELDS, Plan, collect_rejections, parse_plan_payload
from agentrealm_agent.states import PlayContext, dispatch
from agentrealm_agent.strategist import build_prompt, npc_lines
from agentrealm_agent.world import Entity, WorldModel
from agentrealm_agent.zone_discovery import apply_zone


def world(rows: list[str], at=(1, 1), health=10) -> WorldModel:
    glyph = {".": "dirt", "#": "wall"}
    w = WorldModel(character_id=1, map_id=17, pos=at, perception=5, health=health, max_health=10)
    for y, row in enumerate(rows):
        for x, g in enumerate(row):
            w.view.tiles[(x, y)] = glyph[g]
    w.terrain_center, w.terrain_map = at, 17
    w.alive, w.lives = True, 5
    w.hostile_types.add(("npc", "snotling"))
    return w


def ctx(w: WorldModel, plan: Plan | None = None, m: Memory | None = None, never_attack=None, **policy) -> PlayContext:
    pol = {"kind": "scripted", "on_hostile": "fight", "hostile": ["npc"], "hostile_range": 3, **policy}
    return PlayContext(m or Memory(), Policy(**pol), random.Random(0), plan=plan, never_attack=never_attack or [])


def fight(**target) -> Plan:
    return Plan([{"op": "fight", **target}, {"op": "wait", "seconds": 5, "why": "t"}], dict(PARAM_DEFAULTS))


def verbs(out) -> list[str]:
    return [i["verb"] for i in out.intents or []]


class FightOpTest(unittest.TestCase):
    def test_the_contract_lists_it(self):
        self.assertIn("fight", OP_FIELDS)
        self.assertIn("no attack is made from a safe zone", OP_FIELDS["fight"])

    def test_it_needs_one_npc(self):
        for op in ({"op": "fight"}, {"op": "fight", "npc_id": 5, "npc_type": "snotling"}, {"op": "fight", "npc_id": "5"}):
            (goals, _, _), dropped = collect_rejections(
                lambda: parse_plan_payload({"goals": [op]}, floor_params=dict(PARAM_DEFAULTS))
            )
            self.assertEqual(goals, [], op)
            self.assertTrue(dropped, op)

    def test_from_a_safe_tile_it_steps_off_toward_the_npc(self):
        # Safe ground on the west column; the hostile beside it stands just outside.
        w = world(["#####", "#...#", "#...#", "#####"], at=(1, 1))
        for y in (1, 2):
            apply_zone(w, 17, 1, y, {"safe": True})
        w.entities = [Entity("npc", 5, (2, 1), "snotling")]
        out = dispatch(w, ctx(w, fight(npc_id=5)))
        self.assertEqual(out.state, "Fight", out.reason)
        self.assertEqual(verbs(out), ["SetPosition"])
        cell = (out.intents[0]["x"], out.intents[0]["y"])
        self.assertNotIn(cell, ((1, 1), (1, 2)), "off the safe zone")
        self.assertLessEqual(max(abs(cell[0] - 2), abs(cell[1] - 1)), 1, "within reach of it")

    def test_at_the_health_floor_it_stays_on_safe_ground_and_hands_back_the_op(self):
        # Below the floor Retreat is off on a safe tile; stepping off would loop with it until death.
        w = world(["#####", "#...#", "#...#", "#####"], at=(1, 1), health=2)
        for y in (1, 2):
            apply_zone(w, 17, 1, y, {"safe": True})
        w.entities = [Entity("npc", 5, (2, 1), "snotling")]
        w.threat.record(("npc", "snotling"), 3)
        plan, m = fight(npc_id=5), Memory()
        with self.assertLogs("agentrealm_agent.plan", "WARNING") as logs:
            out = dispatch(w, ctx(w, plan, m))
        self.assertNotIn("SetPosition", verbs(out), out.reason)
        self.assertEqual(plan.current()["op"], "wait")
        self.assertIn("at the health floor on safe ground", "\n".join(logs.output))

    def test_engage_never_steps_off_safe_ground_at_the_floor(self):
        from agentrealm_agent.states.fight import engage

        w = world(["#####", "#...#", "#...#", "#####"], at=(1, 1), health=2)
        for y in (1, 2):
            apply_zone(w, 17, 1, y, {"safe": True})
        target = Entity("npc", 5, (2, 1), "snotling")
        w.entities = [target]
        w.threat.record(("npc", "snotling"), 3)
        out = engage(w, ctx(w), target, "Flee")
        self.assertIsNone(out.intents)
        self.assertIn("health floor", out.reason)

    def test_an_npc_no_step_reaches_drops_it_with_why(self):
        # Against the wall between us: no swing and no open step closes on it, so the planner makes the call.
        w = world(["#######", "#..#..#", "#######"], at=(2, 1))
        w.entities = [Entity("npc", 5, (5, 1), "snotling")]
        plan, m = fight(npc_id=5), Memory()
        with self.assertLogs("agentrealm_agent.plan", "WARNING") as logs:
            out = dispatch(w, ctx(w, plan, m, on_hostile="flee"))
        self.assertEqual(plan.current()["op"], "wait", out.reason)
        self.assertIn("no swing or open step reaches snotling 5", "\n".join(logs.output))
        self.assertFalse(any(s["trigger"] == "goal_done" for s in m.strategist_signals))

    def test_off_safe_ground_it_swings(self):
        w = world(["#####", "#...#", "#...#", "#####"], at=(1, 1))
        w.entities = [Entity("npc", 5, (2, 1), "snotling")]
        out = dispatch(w, ctx(w, fight(npc_type="snotling")))
        self.assertEqual(out.state, "Fight", out.reason)
        self.assertIn("Use", verbs(out))

    def test_it_goes_for_the_npc_even_when_the_engagement_would_not(self):
        # The planner's call: a fight_margin no reflex fight would meet does not stop the op.
        w = world(["#####", "#...#", "#...#", "#####"], at=(1, 1))
        w.entities = [Entity("npc", 5, (2, 1), "snotling")]
        c = ctx(w, fight(npc_id=5))
        c.params = {**PARAM_DEFAULTS, "fight_margin": 1000}
        out = dispatch(w, c)
        self.assertEqual(out.state, "Fight", out.reason)
        self.assertIn("Use", verbs(out))

    def test_an_npc_never_seen_is_dropped_not_done(self):
        w = world(["#####", "#...#", "#####"], at=(1, 1))
        plan, m = fight(npc_id=5), Memory()
        with self.assertLogs("agentrealm_agent.plan", "WARNING") as logs:
            dispatch(w, ctx(w, plan, m))
        self.assertEqual(plan.current()["op"], "wait")
        self.assertIn("no npc 5 in view", "\n".join(logs.output))
        self.assertFalse(any(s["trigger"] == "goal_done" for s in m.strategist_signals))

    def test_done_once_the_npc_seen_is_gone(self):
        w = world(["#####", "#...#", "#####"], at=(1, 1))
        w.entities = [Entity("npc", 5, (2, 1), "snotling")]
        plan, m = fight(npc_id=5), Memory()
        dispatch(w, ctx(w, plan, m))
        w.entities = []  # dead, or out of sight
        w.tick += 5
        dispatch(w, ctx(w, plan, m))
        self.assertEqual(plan.current()["op"], "wait")
        self.assertTrue(any(s["trigger"] == "goal_done" for s in m.strategist_signals))

    def test_an_npc_type_fight_is_one_fight_with_the_npc_it_first_saw(self):
        w = world(["######", "#....#", "#....#", "######"], at=(1, 1))
        w.entities = [Entity("npc", 5, (2, 1), "snotling"), Entity("npc", 6, (4, 2), "snotling")]
        plan, m = fight(npc_type="snotling"), Memory()
        out = dispatch(w, ctx(w, plan, m))
        self.assertEqual(plan.fight_npc, 5)
        self.assertIn("npc 5", out.reason)
        # The other one comes nearer: the fight stays on the first.
        w.entities = [Entity("npc", 5, (3, 1), "snotling"), Entity("npc", 6, (1, 2), "snotling")]
        w.tick += 5
        out = dispatch(w, ctx(w, plan, m))
        self.assertIn("npc 5", out.reason)
        # The first is dead: the op is done, not carried on to the next of its type.
        w.entities = [Entity("npc", 6, (1, 2), "snotling")]
        w.tick += 5
        dispatch(w, ctx(w, plan, m))
        self.assertEqual(plan.current()["op"], "wait")
        self.assertTrue(any(s["trigger"] == "goal_done" for s in m.strategist_signals))

    def test_the_guard_changes_nothing(self):
        from agentrealm_agent.states.fight import FightState

        w = world(["#####", "#...#", "#####"], at=(1, 1))
        w.entities = [Entity("npc", 5, (2, 1), "snotling")]
        plan = fight(npc_id=5)
        c = ctx(w, plan, never_attack=["snotling"])
        FightState().guard(w, c)
        FightState().done(w, c)
        self.assertEqual(plan.current()["op"], "fight", "only act drops the op")

    def test_a_forbidden_npc_or_a_boss_drops_it(self):
        for npc, never in ((Entity("npc", 5, (2, 1), "snotling"), ["snotling"]),
                           (Entity("npc", 5, (2, 1), "snotling", health=30, max_health=30), [])):
            w = world(["#####", "#...#", "#####"], at=(1, 1))
            w.entities = [npc]
            plan = fight(npc_id=5)
            with self.assertLogs("agentrealm_agent.plan", "WARNING"):
                dispatch(w, ctx(w, plan, never_attack=never))
            self.assertEqual(plan.current()["op"], "wait")


class ContractTest(unittest.TestCase):
    def test_the_planner_hears_what_cornered_means(self):
        from agentrealm_agent.strategist import system_prompt

        text = system_prompt()
        self.assertIn("A cornered trigger", text)
        self.assertIn("a fight op", text)


class CorneredTest(unittest.TestCase):
    """No step opens distance from the hostiles."""

    def cornered(self, health=10, held=(), npc_at=(2, 1)) -> tuple[WorldModel, PlayContext]:
        w = world(["###", "#.#", "###"], at=(1, 1), health=health)
        w.entities = [Entity("npc", 5, npc_at, "snotling")]
        w.held_supplies = [InventorySupply(i + 1, code) for i, code in enumerate(held)]
        return w, ctx(w, on_hostile="flee")

    def test_low_with_a_potion_it_drinks(self):
        w, c = self.cornered(health=3, held=("small_potion",))
        out = dispatch(w, c)
        self.assertIn("Use", verbs(out), out.reason)
        self.assertIn({"verb": "Use", "target": {"kind": "self"}}, out.intents)

    def test_with_no_heal_due_it_swings_at_the_hostile_in_reach(self):
        w, c = self.cornered(health=10, held=("small_potion",))  # healthy: the potion is kept
        out = dispatch(w, c)
        self.assertEqual(out.state, "Flee")
        self.assertIn("cornered: fight npc 5", out.reason)

    def test_with_nothing_in_reach_it_asks_the_planner_once(self):
        w, c = self.cornered(npc_at=(3, 1))
        for _ in range(3):
            out = dispatch(w, c)
            self.assertEqual(out.reason, "nowhere to flee: asked the planner")
            w.tick += 5
        asks = [s for s in c.memory.strategist_signals if s["trigger"] == "cornered"]
        self.assertEqual(len(asks), 1)
        self.assertEqual(asks[0]["hostiles"], [{"id": 5, "type": "snotling"}])


class NpcStrengthTest(unittest.TestCase):
    def test_each_npc_shows_its_cell_and_swing_next_to_ours(self):
        w = world(["#####", "#...#", "#####"], at=(1, 1))
        w.armed_code = "bronze_sword"
        w.entities = [Entity("npc", 5, (3, 1), "snotling")]
        w.threat.record(("npc", "snotling"), 3)
        head, rows = npc_lines(w, None)
        self.assertIn('our_swing={"every_ticks": 10, "expected_damage":', head)
        self.assertIn('"x": 3, "y": 1', rows)
        self.assertIn('"swing": {"every_ticks": 15, "expected_damage":', rows)
        self.assertIn('"measured": true', rows)

    def test_state_carries_them(self):
        w = world(["#####", "#...#", "#####"], at=(1, 1))
        w.entities = [Entity("npc", 5, (3, 1), "snotling")]
        prompt = build_prompt(
            triggers=[], w=w, plan=Plan([], dict(PARAM_DEFAULTS)), directives=Directives(params=dict(PARAM_DEFAULTS)), knowledge=None
        )[1]["content"]
        self.assertIn("our_swing=", prompt)
        self.assertIn('"swing": {', prompt)


if __name__ == "__main__":
    unittest.main()
