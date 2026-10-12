"""A148: the planner takes x, y from State's lists, not from the map ruler.

The ruler gives every fifth column's whole x, State lists every door on the
map with its cell (the one under the character too), and an op whose cell is
off the map or a wall to stand on is refused with why before it reaches a
state.
"""

from __future__ import annotations

import json
import unittest

from agentrealm_agent.cells import refuse_bad_cells
from agentrealm_agent.directives import PARAM_DEFAULTS, Directives
from agentrealm_agent.plan import Plan
from agentrealm_agent.stalled import RetryBasis, remember as remember_stalled
from agentrealm_agent.strategist import build_prompt, door_lines, ground_lines, map_lines, ruler_lines
from agentrealm_agent.world import Entity, WorldModel
from tests.test_strategist import WAIT_ANSWER, FakeLLM, fake_runner, make, round_trip

# A 16 by 12 level room walled round, with a door in the top wall at x 7.
ROOM_W, ROOM_H, DOOR = 16, 12, (7, 0)


def level_room(at=(7, 1)) -> WorldModel:
    w = WorldModel(character_id=1, map_id=40, pos=at, perception=8)
    w.map_level = 1
    for y in range(ROOM_H):
        for x in range(ROOM_W):
            edge = x in (0, ROOM_W - 1) or y in (0, ROOM_H - 1)
            w.view.tiles[(x, y)] = "wall" if edge else "tile"
    w.view.tiles[DOOR] = "framed_door"
    w.view.size = (ROOM_W, ROOM_H)
    w.alive, w.health, w.max_health = True, 10, 10
    return w


def labelled(line: str) -> dict[int, int]:
    """Each ruler label by the column index it starts at."""
    out, i = {}, 0
    while i < len(line):
        if line[i] != " ":
            j = i
            while j < len(line) and line[j] != " ":
                j += 1
            out[i] = int(line[i:j])
            i = j
        else:
            i += 1
    return out


class RulerTest(unittest.TestCase):
    def check(self, xs: range) -> dict[int, int]:
        labels, ticks = (l[6:] for l in ruler_lines(xs))
        found = labelled(labels)
        self.assertTrue(found)
        for i, x in found.items():
            self.assertEqual(x, xs[i], "a label starts over its own column")
            self.assertEqual(x % 5, 0)
            self.assertEqual(ticks[i], "|")
        self.assertEqual(ticks.count("|"), len(found))
        return found

    def test_a_16_wide_window(self):
        self.assertEqual(sorted(self.check(range(0, 16)).values()), [0, 5, 10, 15])

    def test_an_800_wide_window(self):
        found = self.check(range(785, 816))
        self.assertEqual(sorted(found.values()), [785, 790, 795, 800, 805, 810, 815])

    def test_a_label_that_would_run_into_the_next_is_left_out(self):
        found = self.check(range(9995, 10026))
        self.assertNotIn(10005, found.values(), "10000 runs into 10005's column")

    def test_the_map_stops_at_the_level_edge(self):
        w = level_room()
        lines = map_lines(w)
        self.assertIn("x 0..15 left to right, y 0..11 top to bottom, the whole map is x 0..15, y 0..11", lines[0])
        rows = [l for l in lines[3:]]
        self.assertEqual(len(rows), ROOM_H)
        self.assertTrue(all(len(r) == 6 + ROOM_W for r in rows))
        self.assertEqual(labelled(lines[1][6:]), {0: 0, 5: 5, 10: 10, 15: 15})


class DoorListTest(unittest.TestCase):
    def test_the_level_door_is_listed_with_its_cell(self):
        w = level_room()
        (line,) = door_lines(w)
        rows = json.loads(line.split("=", 1)[1])
        self.assertEqual((rows[0]["x"], rows[0]["y"]), DOOR)
        self.assertEqual(rows[0]["step_from"], [{"x": 7, "y": 1}])
        self.assertFalse(rows[0]["here"])

    def test_the_door_under_the_character_is_listed(self):
        w = level_room(at=DOOR)
        (line,) = door_lines(w)
        rows = json.loads(line.split("=", 1)[1])
        self.assertEqual((rows[0]["x"], rows[0]["y"], rows[0]["here"]), (7, 0, True))

    def test_state_carries_doors_and_what_lies_on_the_ground(self):
        w = level_room()
        w.entities = [Entity("supply", 9, (3, 4), "potion", gem_price=6), Entity("chest", 10, (12, 9))]
        prompt = build_prompt(
            triggers=[], w=w, plan=Plan([], dict(PARAM_DEFAULTS)), directives=Directives(params=dict(PARAM_DEFAULTS)), knowledge=None
        )[1]["content"]
        self.assertIn('doors_seen=[{"block": "framed_door"', prompt)
        self.assertIn('"x": 7, "y": 0', prompt)
        (ground,) = ground_lines(w)
        rows = json.loads(ground.split("=", 1)[1].rsplit(" of=", 1)[0])
        self.assertEqual([(r["kind"], r["x"], r["y"]) for r in rows], [("supply", 3, 4), ("chest", 12, 9)])
        self.assertEqual(rows[0]["gem_price"], 6)
        self.assertIn("on_the_ground=", prompt)


    def test_every_door_is_listed(self):
        w = level_room()
        for x in range(1, ROOM_W - 1):
            w.view.tiles[(x, ROOM_H - 1)] = "framed_door"
        (line,) = door_lines(w)
        self.assertEqual(len(json.loads(line.split("=", 1)[1])), ROOM_W - 1, "doors are few: none is left out")


def terrain_read(map_id: int, **size: int) -> dict:
    """A 3 by 1 terrain read of tile at 1..3, 1, with ``size`` (map_width, map_height) when given."""
    return {"map_id": map_id, "x0": 1, "y0": 1, "width": 3, "height": 1, "rows": ["ttt"], "legend": {"t": {"block_type": "tile"}}, **size}


class MapSizeTest(unittest.TestCase):
    def test_a_terrain_read_gives_the_map_size_and_one_without_it_keeps_it(self):
        w = WorldModel(character_id=1, map_id=40, pos=(2, 1))
        w.apply_terrain(terrain_read(40))
        self.assertIsNone(w.view.size, "no read has given it yet")
        w.apply_terrain(terrain_read(40, map_width=ROOM_W, map_height=ROOM_H))
        self.assertEqual(w.view.size, (ROOM_W, ROOM_H))
        w.apply_terrain(terrain_read(40))
        self.assertEqual(w.view.size, (ROOM_W, ROOM_H), "a read without the fields leaves it")
        kept, refused = refuse_bad_cells([{"op": "travel", "to": "point", "x": 17, "y": 1}], w)
        self.assertEqual(kept, [])
        self.assertIn("17,1 is off map 40, which is x 0..15, y 0..11", refused[0])


class BadCellTest(unittest.TestCase):
    def test_off_the_map_and_wall_cells_are_refused_with_why(self):
        w = level_room()
        off = {"op": "travel", "to": "point", "x": 17, "y": 1}
        wall = {"op": "travel", "to": "point", "x": 6, "y": 0}
        door = {"op": "travel", "to": "point", "x": 7, "y": 0}
        elsewhere = {"op": "travel", "to": "point", "x": 17, "y": 1, "map_id": 3}
        smash = {"op": "break_block", "x": 6, "y": 0, "capability": "smash"}
        kept, refused = refuse_bad_cells([off, wall, door, elsewhere, smash], w)
        self.assertEqual(kept, [door, elsewhere, smash], "another map's cell, and a block to act on, are not judged here")
        self.assertIn("17,1 is off map 40, which is x 0..15, y 0..11", refused[0])
        self.assertIn("6,0 on map 40 is a wall", refused[1])
        self.assertIn("never by counting map columns", refused[0])

    def test_a_symbolic_travel_names_no_cell(self):
        w = level_room()
        self.assertEqual(w.view.tiles[(0, 0)], "wall")
        nearest_shop = {"op": "travel", "to": "shop", "x": 0, "y": 0}
        nearest_entrance = {"op": "travel", "to": "entrance", "x": 0, "y": 0, "map_id": 40}
        point = {"op": "travel", "to": "point", "x": 0, "y": 0}
        for symbolic in (nearest_shop, nearest_entrance):
            self.assertEqual(refuse_bad_cells([symbolic], w), ([symbolic], []), "0, 0 there is the agent's to find")
        kept, refused = refuse_bad_cells([point], w)
        self.assertEqual(kept, [])
        self.assertIn("0,0 on map 40 is a wall", refused[0])

    def test_ops_after_one_that_leaves_the_map_are_not_judged_here(self):
        w = level_room()
        off = {"op": "travel", "to": "point", "x": 17, "y": 1}
        fetch = {"op": "fetch_item", "code": "rope", "x": 300, "y": 200}
        for leave in (
            {"op": "fight_boss", "x": 5, "y": 5},
            {"op": "enter_level", "x": 3, "y": 3},
            {"op": "travel", "to": "point", "x": 7, "y": 0},  # the door: it warps
            {"op": "travel", "to": "point", "x": 40, "y": 40, "map_id": 3},
            {"op": "travel", "to": "town", "x": 0, "y": 0},
        ):
            kept, refused = refuse_bad_cells([off, leave, fetch, dict(off)], w)
            self.assertEqual(kept, [leave, fetch, off], leave)
            self.assertEqual(len(refused), 1, "only the op before the map changes is judged")

    def test_a_pinned_op_that_leaves_the_map_leaves_the_whole_reply_unjudged(self):
        w = level_room()
        off = {"op": "travel", "to": "point", "x": 17, "y": 1}
        self.assertEqual(refuse_bad_cells([off], w, ahead=[{"op": "enter_level", "x": 3, "y": 3}]), ([off], []))
        self.assertEqual(refuse_bad_cells([off], w, ahead=[{"op": "wait", "seconds": 1}])[0], [])

    def test_inside_a_level_above_the_target_nothing_is_judged(self):
        w = level_room()  # level 1, the target: the reply runs here
        off = {"op": "travel", "to": "point", "x": 17, "y": 1}
        self.assertEqual(refuse_bad_cells([off], w)[0], [])
        self.assertEqual(refuse_bad_cells([{"op": "leave_level"}, off], w), ([{"op": "leave_level"}, off], []))
        w.map_level = 3  # above the target: leave_level goes on top first, so the reply runs outside
        self.assertEqual(refuse_bad_cells([off], w), ([off], []))
        stalled: list = []
        remember_stalled(stalled, {"op": "leave_level"}, "no way out", 1, RetryBasis(frozenset(), 0, 0), w.map_id)
        self.assertEqual(refuse_bad_cells([off], w, stalled_ops=stalled)[0], [], "a stalled leave_level is not put back: the reply runs here")

    def test_a_map_of_unknown_size_refuses_nothing_off_it(self):
        w = level_room()
        w.view.size = None
        self.assertEqual(refuse_bad_cells([{"op": "travel", "to": "point", "x": 17, "y": 1}], w)[1], [])

    def test_the_planner_hears_of_it_in_the_next_state(self):
        bad = {"op": "travel", "to": "point", "x": 17, "y": 1}
        s = make(FakeLLM({"goals": [bad]}, WAIT_ANSWER))
        r = fake_runner()
        r.world.view.size = (16, 12)
        round_trip(s, r)
        self.assertNotIn(bad, r.plan.goals, "it never reaches a state")
        s.clock.now += 60
        round_trip(s, r)
        state = s.client.messages[1][1]["content"]
        self.assertIn("last_reply_rejected=", state)
        self.assertIn("17,1 is off map 7", state)


if __name__ == "__main__":
    unittest.main()
