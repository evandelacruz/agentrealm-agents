"""A27: Travel state, targets, and strength bracketing."""

import random
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agentrealm_agent import config
from agentrealm_agent import knowledge_base as kb_mod
from agentrealm_agent.client import ApiError
from agentrealm_agent.config import CharacterConfig, Policy
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.knowledge_maps import record_warp, sync_map_from_view
from agentrealm_agent.memory import Memory
from agentrealm_agent.navigation.rejection import learn_step_rejection, navigation_avoid_costly
from agentrealm_agent.runner import Runner
from agentrealm_agent.states import PlayContext, dispatch
from agentrealm_agent.travel import (
    StrengthBracket,
    TravelOp,
    parse_travel_goals,
    record_hunting_zone,
    record_shop_cell,
    refresh_travel_stack,
    resolve_travel,
    sync_town,
)
from agentrealm_agent.travel.knowledge import iter_shop_cells
from agentrealm_agent.world import WorldModel, ZoneFact


def grid(rows: list[str], at=(0, 0), map_id=1, perception=5) -> WorldModel:
    glyph = {".": "dirt", "#": "wall"}
    w = WorldModel(character_id=1, map_id=map_id, pos=at, perception=perception)
    for y, row in enumerate(rows):
        for x, g in enumerate(row):
            w.view.tiles[(x, y)] = glyph[g]
    w.terrain_center, w.terrain_map = at, map_id
    return w


def ctx_for(m: Memory, kb: KnowledgeBase | None = None) -> PlayContext:
    return PlayContext(m, Policy(kind="scripted"), random.Random(0), knowledge=kb)


class TravelParseTest(unittest.TestCase):
    def test_parse_travel_goals(self):
        ops = parse_travel_goals(["travel:town", "travel:entrance:120:40", "travel:point:3:4:5", "explore"])
        self.assertEqual([o.to for o in ops], ["town", "entrance", "point"])
        self.assertEqual((ops[1].x, ops[1].y, ops[1].map_id), (120, 40, None))
        self.assertEqual((ops[2].map_id, ops[2].x, ops[2].y), (3, 4, 5))

    def test_malformed_goals_are_ignored(self):
        for g in ("travel:point:a:5:6", "travel:entrance", "travel:town:1:2", "travel:point:1", "travel:moon", "x:travel:town"):
            self.assertEqual(parse_travel_goals([g]), [], g)

    def test_refresh_travel_stack_resets_index(self):
        m = Memory(travel_ops=[TravelOp("town")], travel_index=1)
        refresh_travel_stack(m, ["travel:point:3:4"])
        self.assertEqual(m.travel_ops[0].to, "point")
        self.assertEqual(m.travel_index, 0)


class StrengthBracketTest(unittest.TestCase):
    def test_over_strength_closes_lower_tiers(self):
        b = StrengthBracket()
        b.note_over((1, (0, 0)), 12)
        self.assertFalse(b.can_enter_ceiling(12))
        self.assertTrue(b.can_enter_ceiling(13))

    def test_rejection_brackets_strength(self):
        w = grid(["..."])
        w.zones[1] = {(1, 0): ZoneFact(safe=False, strength_ceiling=12)}
        m = Memory()
        learn_step_rejection(m, w, None, (1, 0), "over_strength_ceiling", 20)
        self.assertEqual(m.strength.above, 12)
        self.assertEqual(m.strength.closed, {(1, (1, 0))})

    def test_hunting_picks_highest_eligible_ceiling(self):
        kb = KnowledgeBase.empty("sandbox")
        record_hunting_zone(kb, 1, (5, 0), 10)
        record_hunting_zone(kb, 1, (6, 0), 15)
        w = grid(["........"], at=(0, 0))
        dest = resolve_travel(TravelOp("hunting_ground"), w, kb, StrengthBracket(above=11))
        self.assertEqual(dest.pos, (6, 0))

    def test_closed_ground_reopens_after_reset(self):
        kb = KnowledgeBase.empty("sandbox")
        record_hunting_zone(kb, 1, (5, 0), 10, closed=True)
        w = grid(["......"])
        b = StrengthBracket()
        b.note_over((1, (5, 0)), 10)
        self.assertIsNone(resolve_travel(TravelOp("hunting_ground"), w, kb, b))
        self.assertEqual(b.reset(), {(1, (5, 0))})
        self.assertEqual(resolve_travel(TravelOp("hunting_ground"), w, kb, b).pos, (5, 0))


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
        out = dispatch(w, ctx_for(m, KnowledgeBase.empty("sandbox")))
        self.assertEqual(out.state, "Travel")
        self.assertEqual(out.intents, [{"verb": "SetPosition", "x": 1, "y": 0}])

    def test_unresolved_shop_yields_to_explore(self):
        w = grid(["....."], at=(0, 0))
        m = Memory()
        refresh_travel_stack(m, ["travel:shop"])
        ctx = ctx_for(m, KnowledgeBase.empty("sandbox"))
        out = dispatch(w, ctx)
        self.assertEqual(out.state, "Explore")
        self.assertEqual(m.travel_index, 0, "kept until the knowledge base can resolve it")
        record_shop_cell(ctx.knowledge, 1, (3, 0))
        self.assertEqual(dispatch(w, ctx).state, "Travel")

    def test_unresolved_op_is_dropped_for_a_later_one(self):
        w = grid(["....."], at=(0, 0))
        m = Memory()
        refresh_travel_stack(m, ["travel:shop", "travel:point:3:0"])
        out = dispatch(w, ctx_for(m, KnowledgeBase.empty("sandbox")))
        self.assertEqual(out.state, "Travel")
        self.assertEqual(m.travel_index, 1)

    def test_arrival_drops_the_op_and_moves_on(self):
        w = grid(["...."], at=(1, 0))
        m = Memory()
        refresh_travel_stack(m, ["travel:point:1:0", "travel:point:3:0"])
        ctx = ctx_for(m)
        out = dispatch(w, ctx)
        self.assertEqual((out.state, m.travel_index), ("Travel", 1))
        w.pos = (3, 0)
        out = dispatch(w, ctx)
        self.assertEqual(m.travel_index, 2, "arriving at the last op drops it")
        w.pos = (0, 0)
        self.assertEqual(dispatch(w, ctx).state, "Explore", "a reached op is not revisited")

    def test_done_does_not_move_the_stack(self):
        from agentrealm_agent.states.travel import TravelState

        w = grid(["..."], at=(1, 0))
        m = Memory()
        refresh_travel_stack(m, ["travel:point:1:0", "travel:point:2:0"])
        TravelState().done(w, ctx_for(m))
        self.assertEqual(m.travel_index, 0)


class CrossMapTravelTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for target, attr, value in (
            (kb_mod, "WORLDS_DIR", Path(tmp.name) / "worlds"),
            (config, "STATE_DIR", Path(tmp.name)),
        ):
            patch = mock.patch.object(target, attr, value)
            patch.start()
            self.addCleanup(patch.stop)

    def test_town_on_another_map_walks_to_the_known_door(self):
        kb = KnowledgeBase.empty("sandbox")
        w = grid(["...."], at=(0, 0), map_id=1)
        w.view.tiles[(3, 0)] = "framed_door"
        sync_map_from_view(kb, 1, w.view)
        sync_map_from_view(kb, 2, grid(["...."], map_id=2).view)
        record_warp(kb, 1, (3, 0), "framed_door", 2, (0, 0))
        sync_town(kb, {"map_id": 2, "x": 2, "y": 0})
        m = Memory()
        refresh_travel_stack(m, ["travel:town"])
        out = dispatch(w, ctx_for(m, kb))
        self.assertEqual(out.state, "Travel")
        self.assertEqual(m.path[-1], (3, 0))


class FakeClient:
    def __init__(self, minimap=None):
        self._minimap = minimap

    def minimap(self, cid):
        if self._minimap is None:
            raise ApiError(503, "unavailable")
        return self._minimap


class RunnerTravelTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.object(config, "STATE_DIR", Path(tmp.name))
        patch.start()
        self.addCleanup(patch.stop)

    def runner(self, client) -> Runner:
        cfg = CharacterConfig("T", "default", "test", "sandbox", Policy(kind="scripted"), Path("t.toml"))
        r = Runner(cfg, client, 1, threading.Event(), out=lambda _: None, knowledge=KnowledgeBase.empty("sandbox"))
        self.addCleanup(r.trace.close)
        r.world = grid(["....."])
        return r

    def test_minimap_entrances_merge_map_id(self):
        r = self.runner(FakeClient({"maps": [{"map_id": 5, "entrances": [{"x": 10, "y": 20}]}]}))
        r._sync_minimap()
        self.assertEqual(r.knowledge.entrances["10,20"]["map_id"], 5)

    def test_minimap_failure_is_logged_and_learns_nothing(self):
        r = self.runner(FakeClient(None))
        r._sync_minimap()
        self.assertEqual(r.knowledge.entrances, {})

    def test_priced_supplies_are_shop_cells(self):
        r = self.runner(FakeClient())
        r._learn_shops_from_entities(
            {"supplies": [{"x": 2, "y": 0, "gem_price": 10}, {"x": 3, "y": 0}, {"x": 4, "y": 0, "gem_price": 0}]}
        )
        self.assertEqual(iter_shop_cells(r.knowledge), [(1, (2, 0))])

    def test_loadout_change_reopens_strength_closed_cells(self):
        r = self.runner(FakeClient())
        r._sync_loadout()
        r.world.zones[1] = {(1, 0): ZoneFact(safe=False, strength_ceiling=12)}
        learn_step_rejection(r.mem, r.world, r.knowledge, (1, 0), "over_strength_ceiling", 5)
        r._sync_loadout()
        self.assertIn((1, 0), navigation_avoid_costly(r.mem.nav, r.knowledge, 1, 5)[0], "same loadout keeps it closed")
        r.world.armed_code = "bronze_sword"
        r._sync_loadout()
        self.assertIsNone(r.mem.strength.above)
        self.assertNotIn((1, 0), navigation_avoid_costly(r.mem.nav, r.knowledge, 1, 5)[0])


if __name__ == "__main__":
    unittest.main()
