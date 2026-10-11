"""A114: with the ground in reach all seen, the safe default opens a way on.

A breakable on the edge of the ground a walk reaches, with something past
it, is a way on (``openings``). The one a held item is known to open at
the lowest price (walk plus break) is walked to and broken; with none, the
safe default never steps in place: it asks the planner once, and State
``ways_to_open`` lists the blocks and what held opens each.
"""

from __future__ import annotations

import random
import threading
import unittest
from types import SimpleNamespace

from agentrealm_agent.break_memory import record_attempt
from agentrealm_agent.config import Policy
from agentrealm_agent.directives import default_directives
from agentrealm_agent.item_table import InventorySupply
from agentrealm_agent.memory import Memory, queue_signal
from agentrealm_agent.openings import cheapest_opening, openings
from agentrealm_agent.states import PlayContext, dispatch
from agentrealm_agent.states.intents import arm, use_block
from agentrealm_agent.strategist import ways_to_open_line
from agentrealm_agent.world import VOID, WorldModel, chebyshev

GLYPHS = {".": "dirt", "#": "wall", "T": "tree", "o": "rock"}

# A walled garden cut in two by a hedge (a tree) at x=4: the west half is
# reached, the east half lies past the hedge. Past the walls is the map's
# edge, which a terrain read shows as void.
GARDEN = [
    "#########",
    "#...T...#",
    "#...T...#",
    "#...T...#",
    "#########",
]


def garden(rows: list[str] = GARDEN, at=(1, 2), held: tuple[str, ...] = ("matches",)) -> WorldModel:
    w = WorldModel(character_id=1, map_id=17, pos=at, perception=8)
    for y in range(-1, len(rows) + 1):
        for x in range(-1, len(rows[0]) + 1):
            w.view.tiles[(x, y)] = VOID
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            w.view.tiles[(x, y)] = GLYPHS[ch]
    w.terrain_center, w.terrain_map = at, 17
    w.alive, w.health, w.max_health = True, 10, 10
    w.held_supplies = [InventorySupply(i + 1, code) for i, code in enumerate(held)]
    return w


def kb() -> SimpleNamespace:
    """The parts of the knowledge base break memory reads."""
    return SimpleNamespace(lock=threading.Lock(), breaks={}, items={})


def ctx(m: Memory, knowledge=None) -> PlayContext:
    return PlayContext(
        m, Policy(kind="scripted", goals=[]), random.Random(1), knowledge=knowledge, directives=default_directives()
    )


def walk(w: WorldModel, m: Memory, c: PlayContext, decisions: int = 10):
    """Dispatch and apply each step, until a decision sends something else."""
    for _ in range(decisions):
        out = dispatch(w, c)
        steps = [i for i in out.intents or [] if i["verb"] == "SetPosition"]
        if not steps:
            return out
        w.pos = (steps[0]["x"], steps[0]["y"])
        w.tick += 4
    return out


class OpeningsTest(unittest.TestCase):
    def test_the_hedge_past_reached_ground_is_a_way_on(self):
        found = openings(garden(), None, set())
        self.assertEqual({o.pos for o in found if o.choice is not None}, {(4, 1), (4, 2), (4, 3)})
        # The walls at the hedge's ends touch the east half too; nothing held blasts.
        self.assertEqual({o.pos for o in found if o.choice is None}, {(4, 0), (4, 4)})
        best = found[0]
        self.assertEqual((best.pos, best.block, best.walk), ((4, 2), "tree", 2))
        self.assertEqual((best.choice.capability, best.choice.supply.code), ("burn", "matches"))

    def test_a_block_with_reached_ground_all_round_is_no_way_on(self):
        # A rock in the middle of the reached half leads nowhere new.
        rows = ["#########", "#.o.T...#", "#...T...#", "#...T...#", "#########"]
        self.assertNotIn((2, 1), {o.pos for o in openings(garden(rows), None, set())})

    def test_a_block_the_walk_cannot_stand_by_is_left_out(self):
        rows = ["#########", "#.#.T...#", "#.#.T...#", "#.#.T...#", "#########"]
        # Walled off in the west column: only the walls next to it can be reached.
        found = openings(garden(rows, at=(1, 2)), None, set())
        self.assertFalse(any(o.pos[0] == 4 for o in found), [o.pos for o in found])

    def test_nothing_held_lists_what_would_open_it(self):
        found = openings(garden(held=()), None, set())
        hedge = next(o for o in found if o.pos == (4, 2))
        self.assertIsNone(hedge.choice)
        self.assertEqual(hedge.opened_by, frozenset({"chop", "burn"}))
        self.assertIsNone(cheapest_opening(garden(held=()), None, set()))

    def test_a_given_up_block_is_left_out(self):
        found = openings(garden(), None, set(), skip=frozenset({(4, 2)}))
        self.assertNotIn((4, 2), {o.pos for o in found})

    def test_a_capability_that_failed_there_is_not_tried_again(self):
        k = kb()
        record_attempt(k, map_id=17, pos=(4, 2), capability="burn", result="applied_no_effect")
        best = cheapest_opening(garden(), k, set())
        self.assertNotEqual(best.pos, (4, 2))
        self.assertEqual(best.choice.capability, "burn")

    def test_a_weapon_that_opens_it_comes_before_a_tool_used_up(self):
        w = garden(held=("matches",))
        w.armed_code = "bronze_sword"  # chops trees, and is kept
        best = cheapest_opening(w, None, set())
        self.assertEqual((best.choice.capability, best.choice.supply.code), ("chop", "bronze_sword"))

    def test_the_kept_opening_stays_while_a_held_item_opens_it(self):
        best = cheapest_opening(garden(), None, set(), kept=(4, 1))
        self.assertEqual(best.pos, (4, 1))


class SafeDefaultOpensAWayTest(unittest.TestCase):
    def test_walks_to_the_cheapest_hedge_and_burns_it(self):
        w, m = garden(), Memory()
        c = ctx(m)
        first = dispatch(w, c)
        self.assertEqual(first.state, "Explore")
        self.assertIn("open the tree at (4, 2)", first.reason)
        out = walk(w, m, c)
        self.assertEqual(chebyshev(w.pos, (4, 2)), 1)  # in the matches' reach
        self.assertEqual(out.intents, [arm(1), use_block((4, 2))])
        self.assertEqual(m.opening, (17, (4, 2)))
        self.assertFalse(any(s["trigger"] == "no_way_on" for s in m.strategist_signals))

    def test_with_nothing_to_open_it_asks_the_planner_once_and_never_steps(self):
        w, m = garden(held=()), Memory()
        c = ctx(m)
        for _ in range(5):
            out = dispatch(w, c)
            self.assertEqual((out.state, out.intents, out.wait), ("Explore", None, True), out.reason)
            w.tick += 4
        asks = [s for s in m.strategist_signals if s["trigger"] == "no_way_on"]
        self.assertEqual(len(asks), 1)
        self.assertEqual((asks[0]["map_id"], asks[0]["x"], asks[0]["y"]), (17, 1, 2))

    def test_a_held_opener_ends_the_ask(self):
        w, m = garden(held=()), Memory()
        c = ctx(m)
        dispatch(w, c)
        self.assertEqual(m.no_way_on, (17, (1, 2)))
        w.held_supplies = [InventorySupply(1, "matches")]
        out = dispatch(w, c)
        self.assertTrue(out.intents, out.reason)
        self.assertIsNone(m.no_way_on)

    def test_unexplored_ground_comes_first(self):
        # The east half unseen: there is frontier in reach, so nothing is broken.
        w = garden()
        for y in range(1, 4):
            del w.view.tiles[(2, y)]
        out = dispatch(w, ctx(Memory()))
        self.assertNotIn("open the", out.reason)


class WaysToOpenLineTest(unittest.TestCase):
    def test_lists_each_block_with_what_held_opens_it(self):
        w = garden()
        line = ways_to_open_line(w, openings(w, None, set()))
        self.assertTrue(line.startswith("ways_to_open="), line)
        self.assertIn('"opens_with": "matches"', line)
        self.assertIn('"capability": "burn"', line)
        self.assertIn('"used_up": true', line)
        self.assertIn('"x": 4, "y": 2', line)

    def test_nothing_held_names_the_capabilities_that_would(self):
        w = garden(held=())
        line = ways_to_open_line(w, openings(w, None, set()))
        self.assertIn('"opens_with": null', line)
        self.assertIn('"opened_by": ["burn", "chop"]', line)

    def test_none(self):
        self.assertEqual(ways_to_open_line(garden(), []), "ways_to_open=none")

    def test_the_planner_call_carries_it(self):
        from tests.test_strategist import FakeLLM, fake_runner, make, round_trip

        llm = FakeLLM({"goals": []})
        s, r = make(llm), fake_runner()
        r.world = garden()
        r.world.self_read = r.world.inventory_read = True
        queue_signal(r.mem, {"trigger": "no_way_on", "map_id": 17, "x": 1, "y": 2, "tick": 0})
        round_trip(s, r)
        prompt = llm.messages[0][-1]["content"]
        self.assertIn('"trigger": "no_way_on"', prompt)
        self.assertIn('ways_to_open=[{"beyond": 3, "block": "tree"', prompt)


if __name__ == "__main__":
    unittest.main()
