"""The planner's State: a finished buy is done and shown done (A106), and each
level entrance shows whether a known route reaches it (A107)."""

from __future__ import annotations

import json
import unittest
import unittest.mock
from types import SimpleNamespace

from agentrealm_agent.directives import PARAM_DEFAULTS, Directives
from agentrealm_agent.item_table import InventorySupply
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.memory import Memory, queue_signal
from agentrealm_agent.navigation import entrance_routes
from agentrealm_agent.navigation import planner as P
from agentrealm_agent.plan import Plan
from agentrealm_agent.states.shop import shop_outcome
from agentrealm_agent.strategist import PROGRESSION, SYSTEM_PROMPT, build_prompt, held_line
from agentrealm_agent.travel.knowledge import entrance_key
from agentrealm_agent.world import Entity, WorldModel

from .test_strategist import FakeLLM, fake_runner, make, round_trip

WAIT = {"goals": [{"op": "wait", "seconds": 5, "why": "look"}]}


def buy_plan(*codes: str) -> Plan:
    return Plan([{"op": "buy", "code": c} for c in codes], dict(PARAM_DEFAULTS))


def world(**kw) -> WorldModel:
    w = WorldModel(character_id=1, map_id=7, pos=(0, 0), tick=10, **kw)
    w.alive = True
    return w


class BuyDoneTest(unittest.TestCase):
    """A buy is done once it has paid: the gems held at its first Take spent,
    and the item owned (A106). What it paid from is the head op's own
    progress on the plan, like ``held_before``."""

    def paid_plan(self, *codes: str, gems: int) -> Plan:
        plan = buy_plan(*codes)
        plan.advance(world(gems=gems), Memory())
        plan.paid_from = gems  # as Shop sets it with the Take
        return plan

    def test_done_once_gems_are_spent_and_the_item_held(self):
        plan = self.paid_plan("middle_chest", gems=50)
        plan.held_before = 1  # a baseline that already counted it: paying still finishes the buy
        w, m = world(gems=10), Memory()
        w.held_supplies = [InventorySupply(1, "middle_chest")]
        plan.advance(w, m)
        self.assertIsNone(plan.current())
        self.assertEqual([t["trigger"] for t in m.strategist_signals], ["goal_done"])

    def test_a_chest_that_raised_the_pack_counts_as_owned(self):
        plan = self.paid_plan("middle_chest", gems=50)
        w = world(gems=10)
        w.carry_capacity = 30  # applied: the bought chest is the pack now, not held
        plan.advance(w, Memory())
        self.assertIsNone(plan.current())

    def test_not_done_while_the_gems_are_not_spent(self):
        plan = self.paid_plan("middle_chest", gems=50)
        plan.held_before = 1
        w = world(gems=50)
        w.held_supplies = [InventorySupply(1, "middle_chest")]
        plan.advance(w, Memory())
        self.assertEqual(plan.current()["code"], "middle_chest")

    def test_a_new_head_starts_unpaid(self):
        # Two buys of one code: the first ends by the held count before its
        # gem drop is seen; the second has paid nothing yet.
        plan = self.paid_plan("small_potion", "small_potion", gems=13)
        w = world(gems=13)
        w.held_supplies = [InventorySupply(1, "small_potion")]
        plan.advance(w, Memory())
        self.assertEqual(len(plan.ops_left()), 1)
        self.assertIsNone(plan.paid_from)
        w.gems = 10  # the first buy's gem drop lands now
        plan.advance(w, Memory())
        self.assertEqual(len(plan.ops_left()), 1, "the second buy still needs its own purchase")

    def test_paid_survives_a_probe_snapshot_and_restore(self):
        plan = self.paid_plan("middle_chest", gems=50)
        m = Memory()
        saved_plan, saved_mem = plan.snapshot(), m.snapshot()
        plan.paid_from = None  # whatever a probe did is undone
        plan.restore(saved_plan)
        m.restore(saved_mem)
        w = world(gems=10)
        w.carry_capacity = 30
        plan.advance(w, m)
        self.assertIsNone(plan.current())

    def test_shop_records_what_the_head_buy_paid_from(self):
        w = world(gems=50)
        w.entities = [Entity("supply", 5, (1, 0), "middle_chest", gem_price=40)]
        plan = buy_plan("middle_chest")
        plan.advance(w, Memory())
        ctx = SimpleNamespace(plan=plan, memory=Memory(), knowledge=None, policy=None)
        with unittest.mock.patch("agentrealm_agent.states.shop.planned_take") as take:
            take.return_value = SimpleNamespace(intents=[{"verb": "Take", "supply_id": 5}], reason="take")
            shop_outcome(w, ctx, "Shop")
        self.assertEqual(plan.paid_from, 50)


class HeldChestTest(unittest.TestCase):
    def test_a_held_chest_shows_its_carry_slots_not_a_consumable(self):
        w = world()
        w.held_supplies = [InventorySupply(1, "middle_chest")]
        w.carry_capacity = 30
        held = json.loads(held_line(w).split("=", 1)[1])
        self.assertEqual(held["middle_chest"]["class"], "chest")
        self.assertEqual((held["middle_chest"]["carry_slots"], held["middle_chest"]["applied"]), (30, True))
        self.assertNotIn("chest_capacity", held["middle_chest"])

    def test_the_starting_chest_raises_nothing(self):
        w = world()
        w.held_supplies = [InventorySupply(1, "blue_chest")]
        held = json.loads(held_line(w).split("=", 1)[1])
        self.assertFalse(held["blue_chest"]["applied"])  # the same rule plan.goal_done reads

    def test_not_applied_while_the_pack_is_smaller(self):
        w = world()
        w.held_supplies = [InventorySupply(1, "middle_chest")]
        held = json.loads(held_line(w).split("=", 1)[1])
        self.assertFalse(held["middle_chest"]["applied"])


def state_line(messages: list[dict], key: str) -> str:
    state = messages[1]["content"].split("State:\n", 1)[1].split("\n\n", 1)[0]
    return next(line for line in state.splitlines() if line.startswith(key + "="))


class OpsEndedRecentlyTest(unittest.TestCase):
    """State lists the ops that ended lately, with outcome, and keeps them
    after the reply that saw their trigger (A106)."""

    def test_listed_newest_first_and_kept_after_the_reply(self):
        llm = FakeLLM(WAIT, WAIT, WAIT)
        s, r = make(llm, replan_s=15), fake_runner()
        queue_signal(r.mem, {"trigger": "goal_done", "op": {"op": "buy", "code": "middle_chest"}, "reason": "goal_done"})
        queue_signal(r.mem, {"trigger": "goal_failed", "op": {"op": "buy", "code": "iron_mail"}, "reason": "cannot afford iron_mail"})
        round_trip(s, r)
        expected = [
            {"op": {"op": "buy", "code": "iron_mail"}, "outcome": "dropped", "why": "cannot afford iron_mail", "seconds_ago": 0},
            {"op": {"op": "buy", "code": "middle_chest"}, "outcome": "done", "seconds_ago": 0},
        ]
        self.assertEqual(json.loads(state_line(llm.messages[0], "ops_ended_recently").split("=", 1)[1]), expected)
        # The next call no longer carries their triggers, but State still says they ended.
        s.clock.now += 1
        r.world.tick += 50
        queue_signal(r.mem, {"trigger": "death", "tick": 60})
        round_trip(s, r)
        rows = json.loads(state_line(llm.messages[1], "ops_ended_recently").split("=", 1)[1])
        self.assertNotIn("goal_done", llm.messages[1][1]["content"].split("State:", 1)[0])
        self.assertEqual([(row["op"]["code"], row["seconds_ago"]) for row in rows], [("iron_mail", 5), ("middle_chest", 5)])

    def test_old_ones_drop_out(self):
        s, r = make(), fake_runner()
        queue_signal(r.mem, {"trigger": "goal_done", "op": {"op": "buy", "code": "small_potion"}, "reason": "goal_done"})
        s._collect(r)
        self.assertEqual(len(s.ended.rows(r.world.tick + 300 * 10, 10)), 1)
        self.assertEqual(s.ended.rows(r.world.tick + 301 * 10, 10), [])

    def test_the_planner_is_told_what_done_means(self):
        self.assertIn("ops_ended_recently", SYSTEM_PROMPT)
        self.assertIn("carry_slots", SYSTEM_PROMPT)


def corridor_world() -> WorldModel:
    """Known dirt along y=0 from x=0 to x=5; everything else unexplored."""
    w = world()
    for x in range(6):
        w.view.tiles[(x, 0)] = "dirt"
    return w


def known_detour_world() -> WorldModel:
    """A door at (10, 0) past unexplored ground along y=0; known ground
    below reaches it too, round a wall at x=5 open only at y=15."""
    w = world()
    for x in range(12):
        for y in range(1, 16):
            w.view.tiles[(x, y)] = "wall" if x == 5 and y < 15 else "dirt"
    w.view.tiles[(0, 0)] = "dirt"
    w.view.tiles[(10, 0)] = "framed_door"
    return w


class EntranceRoutesTest(unittest.TestCase):
    """Each entrance on this map: a known path, frontier only, known blocked (A107)."""

    def test_a_door_on_known_ground_has_a_known_path(self):
        w = corridor_world()
        w.view.tiles[(6, 0)] = "framed_door"
        route = entrance_routes(w, [(6, 0)])[(6, 0)]
        self.assertEqual((route.kind, route.steps, route.unexplored), ("path_known", 6, 0))

    def test_a_door_past_unexplored_ground_is_frontier_only(self):
        w = corridor_world()
        w.view.tiles[(20, 0)] = "framed_door"
        route = entrance_routes(w, [(20, 0)])[(20, 0)]
        self.assertEqual(route.kind, "frontier_only")
        self.assertEqual(route.steps, 20)
        self.assertEqual(route.unexplored, 14)

    def test_a_door_walled_in_is_known_blocked(self):
        w = corridor_world()
        for x in range(-40, 41):
            for y in range(1, 41):
                w.view.tiles[(x, y)] = "dirt"  # a known field far larger than the budget floods
        door = (10, 10)
        w.view.tiles[door] = "framed_door"
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if (dx, dy) != (0, 0):
                    w.view.tiles[(door[0] + dx, door[1] + dy)] = "wall"
        route = entrance_routes(w, [door])[door]
        self.assertEqual((route.kind, route.steps), ("known_blocked", 10))

    def test_a_known_detour_beats_a_fog_shortcut(self):
        # The cheapest route cuts across unexplored ground; known ground also
        # reaches the door, the long way round a wall, so it is a known path.
        w = known_detour_world()
        route = entrance_routes(w, [(10, 0)])[(10, 0)]
        self.assertEqual((route.kind, route.unexplored), ("path_known", 0))
        self.assertGreater(route.steps, 20)

    def test_a_known_retry_cut_short_is_not_searched(self):
        w = known_detour_world()
        first = entrance_routes(w, [(10, 0)])[(10, 0)]
        self.assertEqual(first.kind, "path_known")
        budget = P.POCKET_CELLS + 15  # room for the fog route, not for the known-ground retry
        route = entrance_routes(w, [(10, 0)], budget=budget)[(10, 0)]
        self.assertEqual(route.kind, "not_searched")
        self.assertGreater(route.unexplored, 0)
        self.assertEqual(route.steps, 10)

    def test_steps_are_a_route_found_not_more_than_three_times_the_shortest(self):
        w = known_detour_world()
        route = entrance_routes(w, [(10, 0)])[(10, 0)]
        known = P._astar(P._Grid(w, {(10, 0)}, P.CostGridParams(allow_goal_door=True, fog_cost=None)), 10**6)
        assert known.path is not None
        self.assertGreaterEqual(route.steps, len(known.path))
        self.assertLessEqual(route.steps, P.ROUTE_WEIGHT * len(known.path))

    def test_out_of_budget_is_not_searched(self):
        w = corridor_world()
        w.view.tiles[(6, 0)] = "framed_door"
        self.assertEqual(entrance_routes(w, [(6, 0)], budget=0)[(6, 0)].kind, "not_searched")

    def test_the_budget_is_shared_nearest_first(self):
        w = corridor_world()
        w.view.tiles[(3, 1)] = "framed_door"
        w.view.tiles[(40, 0)] = "framed_door"
        routes = entrance_routes(w, [(40, 0), (3, 1)], budget=40)
        self.assertEqual(routes[(3, 1)].kind, "path_known")
        self.assertEqual(routes[(40, 0)].kind, "not_searched")


class EntranceLineTest(unittest.TestCase):
    def test_state_shows_each_entrance_route(self):
        w = corridor_world()
        kb = KnowledgeBase.empty("sandbox")
        w.view.tiles[(6, 0)] = "framed_door"
        w.view.tiles[(20, 0)] = "framed_door"
        for mid, pos in ((7, (6, 0)), (7, (20, 0)), (9, (1, 1))):
            kb.entrances[entrance_key(mid, pos)] = {"map_id": mid, "x": pos[0], "y": pos[1]}
        messages = build_prompt(
            triggers=[], w=w, plan=Plan([], dict(PARAM_DEFAULTS)), directives=Directives(params=dict(PARAM_DEFAULTS)), knowledge=kb
        )
        rows = json.loads(state_line(messages, "level_entrances").split("=", 1)[1])
        self.assertEqual([(r["route"], r.get("steps"), r.get("unexplored")) for r in rows], [
            ("path_known", 6, None),
            ("frontier_only", 20, 14),
            ("other_map", None, None),
        ])

    def test_the_arc_says_to_pick_by_route_not_distance(self):
        self.assertIn("not by distance alone", PROGRESSION)
        self.assertIn("explore toward it", PROGRESSION)
        self.assertIn("known_blocked", SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
