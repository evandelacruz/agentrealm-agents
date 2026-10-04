"""A27: Travel state, targets, and strength bracketing."""

import random
import unittest

from agentrealm_agent.config import Policy
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.memory import Memory
from agentrealm_agent.states import PlayContext, dispatch
from agentrealm_agent.travel import (
    StrengthBracket,
    TravelOp,
    at_destination,
    note_over_strength,
    parse_travel_goals,
    refresh_travel_stack,
    resolve_travel,
    sync_entrances,
    sync_town,
)
from agentrealm_agent.travel.knowledge import record_hunting_zone, record_shop_cell
from agentrealm_agent.world import WorldModel, ZoneFact


def grid(rows: list[str], at=(0, 0), map_id=1, perception=5) -> WorldModel:
    glyph = {".": "dirt", "#": "wall"}
    w = WorldModel(character_id=1, map_id=map_id, pos=at, perception=perception)
    for y, row in enumerate(rows):
        for x, g in enumerate(row):
            w.view.tiles[(x, y)] = glyph[g]
    w.terrain_center, w.terrain_map = at, map_id
    return w


class TravelParseTest(unittest.TestCase):
    def test_parse_travel_goals(self):
        ops = parse_travel_goals(["travel:town", "travel:entrance:120:40", "explore"])
        self.assertEqual(len(ops), 2)
        self.assertEqual(ops[0].to, "town")
        self.assertEqual((ops[1].x, ops[1].y), (120, 40))

    def test_refresh_travel_stack_resets_index(self):
        m = Memory(travel_ops=[TravelOp("town")], travel_index=1)
        refresh_travel_stack(m, ["travel:point:3:4"])
        self.assertEqual(m.travel_ops[0].to, "point")
        self.assertEqual(m.travel_index, 0)


class StrengthBracketTest(unittest.TestCase):
    def test_over_strength_closes_higher_tiers(self):
        b = StrengthBracket()
        note_over_strength(b, 12)
        self.assertFalse(b.can_enter_ceiling(12))
        self.assertTrue(b.can_enter_ceiling(13))

    def test_hunting_picks_highest_eligible_ceiling(self):
        kb = KnowledgeBase.empty("sandbox")
        record_hunting_zone(kb, 1, (5, 0), 10)
        record_hunting_zone(kb, 1, (6, 0), 15)
        record_hunting_zone(kb, 1, (7, 0), 12, closed=True)
        w = grid(["........"], at=(0, 0))
        bracket = StrengthBracket(above=11)
        dest = resolve_travel(TravelOp("hunting_ground"), w, kb, bracket)
        self.assertIsNotNone(dest)
        assert dest is not None
        self.assertEqual(dest.pos, (6, 0))


class ResolveTravelTest(unittest.TestCase):
    def test_town_from_knowledge_base(self):
        kb = KnowledgeBase.empty("sandbox")
        sync_town(kb, {"map_id": 2, "x": 9, "y": 8})
        w = grid(["..."], map_id=1)
        dest = resolve_travel(TravelOp("town"), w, kb, StrengthBracket())
        self.assertEqual((dest.map_id, dest.pos), (2, (9, 8)))

    def test_shop_from_seen_prices(self):
        kb = KnowledgeBase.empty("sandbox")
        record_shop_cell(kb, 1, (4, 0))
        w = grid(["....."])
        dest = resolve_travel(TravelOp("shop"), w, kb, StrengthBracket())
        self.assertEqual(dest.pos, (4, 0))


class TravelStateTest(unittest.TestCase):
    def test_travel_beats_explore(self):
        w = grid([".........."], at=(0, 0))
        m = Memory()
        refresh_travel_stack(m, ["travel:point:3:0"])
        ctx = PlayContext(m, Policy(kind="scripted"), random.Random(0), knowledge=KnowledgeBase.empty("sandbox"))
        out = dispatch(w, ctx)
        self.assertEqual(out.state, "Travel")
        self.assertIsNotNone(out.intents)

    def test_arrival_advances_the_stack(self):
        w = grid(["..."], at=(1, 0))
        m = Memory()
        refresh_travel_stack(m, ["travel:point:1:0", "travel:point:2:0"])
        ctx = PlayContext(m, Policy(kind="scripted"), random.Random(0))
        from agentrealm_agent.states.travel import TravelState

        state = TravelState()
        op = m.travel_ops[0]
        dest = resolve_travel(op, w, None, m.strength)
        assert dest is not None
        self.assertTrue(at_destination(w, dest))
        self.assertFalse(state.done(w, ctx))
        self.assertEqual(m.travel_index, 1)
        w.pos = (2, 0)
        self.assertTrue(state.done(w, ctx))


class MinimapSyncTest(unittest.TestCase):
    def test_entrances_merge_map_id(self):
        kb = KnowledgeBase.empty("sandbox")
        sync_entrances(kb, {"maps": [{"map_id": 5, "entrances": [{"x": 10, "y": 20}]}]})
        row = kb.entrances["10,20"]
        self.assertEqual(row["map_id"], 5)


if __name__ == "__main__":
    unittest.main()
