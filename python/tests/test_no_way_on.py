"""A114: with the ground in reach all seen, the way on is the planner's.

The safe default never steps in place and never parks because a hostile is
next to it: it raises one ``no_way_on`` trigger. State ``map`` draws the
ground round the character cell by cell, so the planner can name the block
to break, and a ``break_block`` aimed at a cell that holds no block its
capability opens is dropped with why.
"""

from __future__ import annotations

import random
import unittest

from agentrealm_agent.config import Policy
from agentrealm_agent.directives import PARAM_DEFAULTS, Directives, default_directives
from agentrealm_agent.item_table import InventorySupply
from agentrealm_agent.memory import Memory
from agentrealm_agent.plan import Plan
from agentrealm_agent.states import PlayContext, dispatch
from agentrealm_agent.strategist import MAP_RADIUS, build_prompt, map_lines, system_prompt
from agentrealm_agent.world import VOID, Entity, WorldModel

GLYPHS = {".": "dirt", "#": "wall", "T": "tree", "_": VOID}

# A walled garden cut in two by a hedge (a tree) at x=4. Past the walls is
# the map's edge, which a terrain read shows as no ground.
GARDEN = [
    "_________",
    "_#######_",
    "_#..T..#_",
    "_#..T..#_",
    "_#######_",
    "_________",
]


def garden(at=(2, 2), held: tuple[str, ...] = ("matches",)) -> WorldModel:
    w = WorldModel(character_id=1, map_id=17, pos=at, perception=8)
    for y, row in enumerate(GARDEN):
        for x, ch in enumerate(row):
            w.view.tiles[(x, y)] = GLYPHS[ch]
    w.terrain_center, w.terrain_map = at, 17
    w.alive, w.health, w.max_health = True, 10, 10
    w.held_supplies = [InventorySupply(i + 1, code) for i, code in enumerate(held)]
    return w


def ctx(m: Memory, plan: Plan | None = None, **policy) -> PlayContext:
    return PlayContext(
        m, Policy(kind="scripted", goals=[], **policy), random.Random(1), plan=plan, directives=default_directives()
    )


class AskThePlannerTest(unittest.TestCase):
    def test_nothing_to_explore_asks_once_and_never_steps(self):
        w, m = garden(), Memory()
        c = ctx(m)
        for _ in range(5):
            out = dispatch(w, c)
            self.assertEqual((out.state, out.intents), ("Explore", None), out.reason)
            self.assertIn("asked the planner", out.reason)
            w.tick += 4
        asks = [s for s in m.strategist_signals if s["trigger"] == "no_way_on"]
        self.assertEqual(len(asks), 1, "one ask per cell")
        self.assertEqual((asks[0]["map_id"], asks[0]["x"], asks[0]["y"]), (17, 2, 2))

    def test_a_hostile_next_to_it_is_no_reason_to_park(self):
        # Before A114 a hostile beside the character shut every neighbour and it held "boxed in", asking no one.
        w, m = garden(), Memory()
        w.entities = [Entity("npc", 5, (3, 2), "gnawer")]
        w.hostile_types.add(("npc", "gnawer"))
        out = dispatch(w, ctx(m, hostile=["npc"], hostile_range=1, on_hostile="ignore"))
        self.assertNotIn("boxed in", out.reason)
        self.assertTrue(any(s["trigger"] == "no_way_on" for s in m.strategist_signals), out.reason)

    def test_ground_to_explore_again_makes_a_later_ask_new(self):
        w, m = garden(), Memory()
        c = ctx(m)
        dispatch(w, c)
        self.assertEqual(m.no_way_on, (17, (2, 2), "nothing left to explore"))
        w.view.tiles[(1, 3)] = "dirt"  # a gap in the wall onto ground not seen: frontier in reach again
        w.view.tiles.pop((0, 3))
        out = dispatch(w, c)
        self.assertTrue(out.intents, out.reason)
        self.assertIsNone(m.no_way_on)


class FrontierStillThereTest(unittest.TestCase):
    def test_a_frontier_whose_step_is_taken_holds_without_asking(self):
        # A gap in the wall onto unseen ground, a character standing in it: the frontier is there, only its step is taken.
        w, m = garden(), Memory()
        w.view.tiles[(1, 3)] = "dirt"
        w.view.tiles.pop((0, 3))
        w.entities = [Entity("character", 9, (1, 3), "peer")]
        out = dispatch(w, ctx(m))
        self.assertEqual((out.state, out.intents, out.wait), ("Explore", None, True), out.reason)
        self.assertNotIn("asked the planner", out.reason)
        self.assertFalse(any(s["trigger"] == "no_way_on" for s in m.strategist_signals))


class BreakTargetTest(unittest.TestCase):
    def break_op(self, x: int, y: int) -> tuple[Plan, dict]:
        op = {"op": "break_block", "x": x, "y": y, "capability": "burn"}
        return Plan([op, {"op": "wait", "seconds": 5, "why": "t"}], dict(PARAM_DEFAULTS)), op

    def test_a_cell_with_no_block_is_never_aimed_at(self):
        w, m = garden(), Memory()
        plan, _ = self.break_op(3, 3)  # ground, where a hostile might stand
        with self.assertLogs("agentrealm_agent.plan", "WARNING") as logs:
            dispatch(w, ctx(m, plan))
        self.assertEqual(plan.current()["op"], "wait")
        self.assertIn("holds no block to break", "\n".join(logs.output))

    def test_a_block_its_capability_does_not_open_is_dropped(self):
        w, m = garden(), Memory()
        plan, _ = self.break_op(1, 2)  # a wall: only a blast opens it
        with self.assertLogs("agentrealm_agent.plan", "WARNING") as logs:
            dispatch(w, ctx(m, plan))
        self.assertIn("burn is not known to open wall", "\n".join(logs.output))

    def test_the_named_hedge_is_walked_to_and_burned(self):
        w, m = garden(at=(3, 2)), Memory()  # in the matches' reach
        plan, op = self.break_op(4, 3)
        out = dispatch(w, ctx(m, plan))
        self.assertEqual(out.state, "Break", out.reason)
        self.assertEqual(plan.current(), op)
        self.assertIn({"verb": "Use", "target": {"kind": "block", "x": 4, "y": 3}}, out.intents)

    def test_a_block_of_a_type_with_no_known_opener_may_be_tried(self):
        # Its opener is learned only by trying: a door a bomb may blast, or a type the rules never name.
        for block, cap in (("framed_door", "blast"), ("pedestal", "burn")):
            w, m = garden(at=(3, 2), held=("matches", "bomb")), Memory()
            w.view.tiles[(4, 3)] = block
            op = {"op": "break_block", "x": 4, "y": 3, "capability": cap}
            plan = Plan([op], dict(PARAM_DEFAULTS))
            out = dispatch(w, ctx(m, plan))
            self.assertEqual(plan.current(), op, block)
            self.assertEqual(out.state, "Break", out.reason)

    def test_a_cell_not_seen_yet_is_walked_to_first(self):
        w, m = garden(), Memory()
        plan, op = self.break_op(20, 20)
        dispatch(w, ctx(m, plan))
        self.assertEqual(plan.current(), op)


class MapTest(unittest.TestCase):
    def test_draws_each_cell_round_the_character(self):
        w = garden()
        w.view.readable[(1, 1)] = True
        w.entities = [Entity("npc", 5, (6, 2), "gnawer"), Entity("npc", 6, (3, 3), "scarecrow"), Entity("supply", 7, (6, 3), "gem")]
        w.hostile_types.add(("npc", "gnawer"))
        lines = map_lines(w)
        head, rows = lines[0], {int(l[:5]): l[6:] for l in lines if l[:5].strip().lstrip("-").isdigit()}
        self.assertEqual(len(rows), 2 * MAP_RADIUS + 1)
        x0 = 2 - MAP_RADIUS

        def at(x: int, y: int) -> str:
            return rows[y][x - x0]

        self.assertEqual(at(2, 2), "@")
        self.assertEqual((at(4, 2), at(4, 3)), ("T", "T"))
        self.assertEqual((at(1, 2), at(0, 2), at(3, 2)), ("#", "_", "."))
        self.assertEqual((at(6, 2), at(3, 3), at(6, 3)), ("H", "N", "$"))
        self.assertEqual((at(1, 1), at(17, 2)), ("S", "?"))
        self.assertIn("x -13..17", head)
        self.assertIn("T tree (hedges are trees)", head)
        self.assertNotIn("~ water", head, "only the marks on the map are in the legend")

    def test_the_ruler_reads_x_top_to_bottom(self):
        w = garden(at=(120, 40))
        w.view.tiles.clear()
        ruler = [l[6:] for l in map_lines(w)[1:4]]
        column = "".join(r[0] for r in ruler)
        self.assertEqual(column, "105")
        self.assertEqual("".join(r[MAP_RADIUS] for r in ruler), "120")

    def test_a_locked_door_shows_apart(self):
        w = garden()
        w.view.tiles[(7, 3)] = "framed_door"
        w.view.locked[(7, 3)] = True
        rows = {int(l[:5]): l[6:] for l in map_lines(w)[2:] if l[:5].strip().lstrip("-").isdigit()}
        self.assertEqual(rows[3][7 - (2 - MAP_RADIUS)], "L")

    def test_state_carries_it_on_any_map(self):
        w = garden()
        w.map_level = None  # the overworld as much as a level
        prompt = build_prompt(
            triggers=[], w=w, plan=Plan([], dict(PARAM_DEFAULTS)), directives=Directives(params=dict(PARAM_DEFAULTS)), knowledge=None
        )[1]["content"]
        self.assertIn("map (map 17,", prompt)
        self.assertIn("@", prompt)

    def test_the_contract_states_the_minimal_rules(self):
        text = system_prompt()
        self.assertIn("State map draws the ground round the character", text)
        self.assertIn("A block is destroyed only by a Use with an item whose capability opens its type", text)
        self.assertIn("No attack can be made from a safe zone", text)
        self.assertIn("no_way_on", text)


if __name__ == "__main__":
    unittest.main()
