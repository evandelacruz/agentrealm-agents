"""A145: levels are taken in order, the lowest uncleared first."""

import random
import unittest

from agentrealm_adk.config import Policy
from agentrealm_adk.directives import PARAM_DEFAULTS
from agentrealm_adk.knowledge_base import KnowledgeBase
from agentrealm_adk.level_order import (
    LEVEL,
    entrance_rank,
    record_entrance_level,
    refuse_barred,
    target_level,
)
from agentrealm_adk.knowledge_maps import record_warp
from agentrealm_adk.level_order import LEAVE_OP, sync_leave, way_out
from agentrealm_adk.memory import Memory
from agentrealm_adk.stalled import RetryBasis, remember
from agentrealm_adk.navigation import CostGridParams
from agentrealm_adk.navigation.door_graph import doors_goal_path
from agentrealm_agent.plan import Plan
from agentrealm_agent.states import PlayContext, dispatch
from agentrealm_agent.strategist import entrance_lines
from agentrealm_adk.travel.knowledge import entrance_key, sync_entrances
from agentrealm_adk.world import WorldModel


def grid(rows: list[str], at=(0, 0), map_id=1) -> WorldModel:
    glyph = {".": "dirt", "#": "wall", "D": "framed_door"}
    w = WorldModel(character_id=1, map_id=map_id, pos=at, perception=5)
    for y, row in enumerate(rows):
        for x, g in enumerate(row):
            w.view.tiles[(x, y)] = glyph.get(g, g)
    w.terrain_center, w.terrain_map = at, map_id
    return w


def kb_with(map_id: int, *cells: tuple[int, int], levels: dict | None = None) -> KnowledgeBase:
    kb = KnowledgeBase.empty("sandbox")
    sync_entrances(kb, {"maps": [{"map_id": map_id, "entrances": [{"x": x, "y": y} for x, y in cells]}]})
    for cell, level in (levels or {}).items():
        kb.entrances[entrance_key(map_id, cell)][LEVEL] = level
    return kb


def ctx_for(m: Memory, kb: KnowledgeBase, plan: Plan) -> PlayContext:
    return PlayContext(m, Policy(kind="scripted", goals=["explore"]), random.Random(0), knowledge=kb, plan=plan)


class TargetTest(unittest.TestCase):
    def test_lowest_uncleared_level(self):
        w = grid(["."])
        self.assertEqual(target_level(w), 1)
        w.levels_cleared = [1]
        self.assertEqual(target_level(w), 2, "once level 1 is cleared, level 2 is the target")
        w.levels_cleared = [1, 3]
        self.assertEqual(target_level(w), 2)
        w.levels_cleared, w.level_count = [1, 2], 2
        self.assertIsNone(target_level(w), "every level cleared")

    def test_clear_ceremony_moves_the_target_before_the_snapshot(self):
        w = grid(["."])
        w.note_level_clear({"level_number": 1, "max_health_gain": 5})
        self.assertEqual(w.levels_cleared, [1])
        self.assertEqual(target_level(w), 2)


class RecordTest(unittest.TestCase):
    def test_entering_a_level_records_the_entrance_number(self):
        kb = kb_with(1, (4, 0))
        record_entrance_level(kb, 1, (4, 0), 2)
        self.assertEqual(kb.entrances[entrance_key(1, (4, 0))][LEVEL], 2)

    def test_a_door_between_rooms_is_no_entrance(self):
        kb = KnowledgeBase.empty("sandbox")
        kb.maps["8"] = {"level": 1}
        record_entrance_level(kb, 8, (3, 3), 1)
        self.assertEqual(kb.entrances, {})


class OrderTest(unittest.TestCase):
    PARAMS = CostGridParams(allow_goal_door=True)

    def test_known_target_then_unknown_never_a_higher_one(self):
        w = grid(["."])
        self.assertEqual(entrance_rank({LEVEL: 1}, w), 0)
        self.assertEqual(entrance_rank({}, w), 1)
        self.assertIsNone(entrance_rank({LEVEL: 2}, w), "a higher uncleared level is barred")
        w.levels_cleared = [1]
        self.assertEqual(entrance_rank({LEVEL: 2}, w), 0, "level 2 is the target once 1 is cleared")
        self.assertEqual(entrance_rank({LEVEL: 1}, w), 2, "a cleared level comes after the unknown")

    def test_fallback_walks_to_the_known_target_past_a_nearer_unknown(self):
        w = grid(["D...D...D"], at=(2, 0))
        kb = kb_with(1, (0, 0), (4, 0), (8, 0), levels={(0, 0): 2, (8, 0): 1})
        path = doors_goal_path(w, kb, self.PARAMS)
        self.assertEqual(path[-1], (8, 0), "the known target goes first")

    def test_fallback_takes_an_unknown_before_a_nearer_higher_level(self):
        w = grid(["D.......D"], at=(1, 0))
        kb = kb_with(1, (0, 0), (8, 0), levels={(0, 0): 2})
        self.assertEqual(doors_goal_path(w, kb, self.PARAMS)[-1], (8, 0))

    def test_fallback_never_takes_a_higher_level(self):
        w = grid(["D...."], at=(3, 0))
        kb = kb_with(1, (0, 0), levels={(0, 0): 3})
        self.assertIsNone(doors_goal_path(w, kb, self.PARAMS))

    def test_state_shows_each_entrance_order(self):
        w = grid(["D...D...D"], at=(4, 1))
        kb = kb_with(1, (0, 0), (4, 0), (8, 0), levels={(0, 0): 2, (8, 0): 1})
        line = entrance_lines(w, kb)[0]
        for want in ('"order": "target"', '"order": "unknown"', '"order": "barred"', '"level": 2'):
            self.assertIn(want, line)


class RefuseTest(unittest.TestCase):
    def test_planner_ops_at_a_higher_level_are_refused_with_why(self):
        w = grid([".D..D"])
        kb = kb_with(1, (1, 0), (4, 0), levels={(1, 0): 2})
        goals = [
            {"op": "enter_level", "x": 1, "y": 0},
            {"op": "travel", "to": "entrance", "x": 1, "y": 0, "map_id": 1},
            {"op": "enter_level", "x": 4, "y": 0},
        ]
        kept, refused = refuse_barred(goals, w, kb)
        self.assertEqual(kept, [{"op": "enter_level", "x": 4, "y": 0}])
        self.assertEqual(len(refused), 2)
        self.assertIn("level 2", refused[0])
        self.assertIn("level 1 is not cleared", refused[0])


class LeaveTest(unittest.TestCase):
    def test_inside_a_higher_level_he_leaves_the_way_he_came(self):
        w = grid(["D....", "....."], at=(1, 1), map_id=8)
        w.apply_minimap({"maps": [{"map_id": 8, "level": 2}]})
        w.apply_position({"map_id": 8, "x": 1, "y": 1})
        kb = kb_with(1, (5, 5))
        record_warp(kb, 1, (5, 5), "framed_door", 8, (1, 1))
        record_entrance_level(kb, 1, (5, 5), 2)
        m = Memory()
        plan = Plan([{"op": "fight_boss", "x": 4, "y": 0}], dict(PARAM_DEFAULTS))
        out = dispatch(w, ctx_for(m, kb, plan))
        self.assertEqual(plan.current(), {"op": "leave_level"}, "leaving goes on top of the stack")
        self.assertEqual(out.state, "Level")
        self.assertEqual(out.intents, [{"verb": "SetPosition", "x": 0, "y": 0}], "onto the door it came in by")
        self.assertEqual(kb.entrances[entrance_key(1, (5, 5))][LEVEL], 2)

    def test_level_4_entered_with_nothing_cleared_is_left(self):
        w = grid(["D....", "....."], at=(1, 1), map_id=44)
        w.apply_position({"map_id": 44, "x": 1, "y": 1})
        w.apply_minimap({"maps": [{"map_id": 44, "level": 4}]})
        self.assertEqual(w.map_level, 4, "the minimap's level for the current map")
        kb = kb_with(1, (5, 5))
        record_warp(kb, 1, (5, 5), "framed_door", 44, (1, 1))
        plan = Plan([{"op": "explore_area", "x": 0, "y": 0, "radius": 3}], dict(PARAM_DEFAULTS))
        out = dispatch(w, ctx_for(Memory(), kb, plan))
        self.assertEqual(plan.current(), {"op": "leave_level"})
        self.assertEqual(out.state, "Level")

    def test_leave_is_done_once_outside(self):
        w = grid(["....."], at=(1, 0), map_id=8)
        w.apply_minimap({"maps": [{"map_id": 8, "level": 2}]})
        w.apply_position({"map_id": 8, "x": 1, "y": 0})
        plan = Plan([{"op": "explore_area", "x": 0, "y": 0, "radius": 3}], dict(PARAM_DEFAULTS))
        dispatch(w, ctx_for(Memory(), KnowledgeBase.empty("sandbox"), plan))
        self.assertEqual(plan.current(), {"op": "leave_level"})
        w.apply_position({"map_id": 1, "x": 5, "y": 6})
        dispatch(w, ctx_for(Memory(), KnowledgeBase.empty("sandbox"), plan))
        self.assertEqual(plan.current(), {"op": "leave_level"}, "an unread level is unknown, not out")
        w.apply_minimap({"maps": [{"map_id": 1}, {"map_id": 8, "level": 2}]})
        dispatch(w, ctx_for(Memory(), KnowledgeBase.empty("sandbox"), plan))
        self.assertEqual(plan.current()["op"], "explore_area")

    def test_a_leave_on_the_stack_is_not_pushed_again(self):
        w = grid(["....."], at=(1, 0), map_id=8)
        w.apply_minimap({"maps": [{"map_id": 8, "level": 2}]})
        w.apply_position({"map_id": 8, "x": 1, "y": 0})
        plan = Plan([{"op": "wait", "seconds": 5, "why": "t"}, dict(LEAVE_OP)], dict(PARAM_DEFAULTS))
        sync_leave(plan, w, Memory())
        self.assertEqual([op["op"] for op in plan.ops_left()], ["wait", "leave_level"], "demoted below: it stays there")

    def test_a_stalled_leave_waits_for_a_reason_to_retry(self):
        # A104: a leave_level that stalled is moved or dropped like any op, and not pushed back.
        w = grid(["....."], at=(1, 0), map_id=8)
        w.apply_minimap({"maps": [{"map_id": 8, "level": 2}]})
        w.apply_position({"map_id": 8, "x": 1, "y": 0})
        m = Memory()
        remember(m.stalled_ops, dict(LEAVE_OP), "no progress", 0, RetryBasis(frozenset(), 0, 0), 8)
        plan = Plan([{"op": "wait", "seconds": 5, "why": "t"}], dict(PARAM_DEFAULTS))
        sync_leave(plan, w, m)
        self.assertEqual(plan.current()["op"], "wait")

    def test_the_target_level_is_not_left(self):
        w = grid(["....D"], at=(0, 0), map_id=8)
        w.apply_minimap({"maps": [{"map_id": 8, "level": 2}]})
        w.apply_position({"map_id": 8, "x": 0, "y": 0})
        w.levels_cleared = [1]
        plan = Plan([{"op": "enter_level", "x": 0, "y": 0}], dict(PARAM_DEFAULTS))
        out = dispatch(w, ctx_for(Memory(), KnowledgeBase.empty("sandbox"), plan))
        self.assertEqual(plan.current()["op"], "enter_level")
        self.assertEqual(out.state, "Level")



class WayOutTest(unittest.TestCase):
    def level_map(self) -> WorldModel:
        w = grid(["D...D", ".....", "D...."], at=(2, 1), map_id=8)
        w.apply_minimap({"maps": [{"map_id": 8, "level": 2}]})
        w.apply_position({"map_id": 8, "x": 2, "y": 1})
        return w

    def test_a_door_known_to_lead_out_comes_first(self):
        w = self.level_map()
        kb = kb_with(1, (5, 5))
        record_warp(kb, 1, (5, 5), "framed_door", 8, (4, 1))  # came in beside (4, 0)
        record_warp(kb, 8, (0, 2), "framed_door", 1, (6, 6))
        w.apply_minimap({"maps": [{"map_id": 1}]})  # map 1 is read as outside every level
        self.assertEqual(way_out(kb, w), (0, 2))

    def test_a_warp_to_a_map_whose_level_is_unread_is_unknown_not_out(self):
        w = self.level_map()
        kb = kb_with(1, (5, 5))
        record_warp(kb, 1, (5, 5), "framed_door", 8, (4, 1))
        record_warp(kb, 8, (0, 2), "framed_door", 3, (6, 6))  # map 3's level never read
        self.assertEqual(way_out(kb, w), (4, 0), "back the way he came, not the unknown warp")
        kb.maps["3"] = {"level": 0}  # read since: outside every level
        self.assertEqual(way_out(kb, w), (0, 2))

    def test_back_the_way_he_came_never_deeper_in(self):
        w = self.level_map()
        kb = kb_with(1, (5, 5))
        record_warp(kb, 1, (5, 5), "framed_door", 8, (4, 1))
        record_warp(kb, 8, (4, 0), "framed_door", 9, (0, 0))  # a door between the level's rooms
        kb.maps["9"] = {"level": 2}
        self.assertEqual(way_out(kb, w), (0, 0), "the door by the landing leads deeper: the next nearest")

    def test_no_known_way_in_or_out_asks_the_planner(self):
        w = self.level_map()
        kb = KnowledgeBase.empty("sandbox")
        self.assertIsNone(way_out(kb, w), "no guess on proximity alone")
        plan = Plan([dict(LEAVE_OP)], dict(PARAM_DEFAULTS))
        m = Memory()
        out = dispatch(w, ctx_for(m, kb, plan))
        self.assertNotIn("SetPosition", [i.get("verb") for i in out.intents or []])
        self.assertIn("no known way out", str(m.no_way_on) + out.reason)


class BarredEnterLevelTest(unittest.TestCase):
    def test_a_barred_enter_level_on_top_is_dropped_with_why(self):
        w = grid(["....D"], at=(0, 0))
        w.apply_position({"map_id": 1, "x": 0, "y": 0})
        kb = kb_with(1, (4, 0), levels={(4, 0): 3})
        plan = Plan([{"op": "enter_level", "x": 4, "y": 0}], dict(PARAM_DEFAULTS))
        out = dispatch(w, ctx_for(Memory(), kb, plan))
        self.assertIsNone(plan.current())
        self.assertNotEqual(out.state, "Level")


if __name__ == "__main__":
    unittest.main()
