"""Free-play run 5 offline: Gather and Detour walked into a hostile's post (A22, A71, A67).

The character died to a guard that kept a post: Gather and Detour kept
walking it to gem piles beside that post, 12 hits for 16 damage. Ground was
judged only by hostiles in view near the pile cell, never by the route there
or by a hostile out of view, and Gather's pile target ignored the region the
planner named.

1. The world model remembers hostiles out of view: where each was last
   seen, the post a guard keeps, and how far from that post it has hit us
   (``WorldModel.sightings``).
2. Ground a remembered hostile holds is not gathered, and no walk to a
   target crosses a known hostile's reach (``hostile_ground.known_reach``,
   ``route_clear``), unless the op chose to fight for it (``fight``).
3. A hit on the way to a pile re-prices that pile.
4. Gather's piles honour the named region.
"""

from __future__ import annotations

import random
import unittest

from agentrealm_agent.config import Policy
from agentrealm_agent.directives import PARAM_DEFAULTS
from agentrealm_agent.memory import Memory
from agentrealm_agent.plan import Plan, validate_goal_op
from agentrealm_agent.states import PlayContext
from agentrealm_agent.states.detour import DetourState, detour_find
from agentrealm_agent.states.gather import gather_outcome
from agentrealm_agent.hostile_ground import danger, known_reach
from agentrealm_agent.states.gather_safe import gather_ground, route_clear
from agentrealm_agent.zone_discovery import apply_zone
from agentrealm_agent.world import EMPTY_POST_HALF_LIFE_TICKS, POST_STILL_TICKS, SIGHTING_TICKS, Entity, WorldModel

MAP = 1
GUARD = ("npc", "fake_guard")
POST = (20, 10)
PILE = (21, 11)  # beside the post


def policy(**kw) -> Policy:
    return Policy(kind="scripted", **{"goals": [], "on_hostile": "ignore", **kw})


def field(at=(2, 10), size=40, perception=6) -> WorldModel:
    w = WorldModel(character_id=1, map_id=MAP, pos=at, perception=perception)
    for x in range(size):
        for y in range(size):
            w.view.tiles[(x, y)] = "dirt"
    w.terrain_center, w.terrain_map = at, MAP
    w.health, w.max_health = 100, 100
    w.hostile_types.add(GUARD)
    return w


def guard(at=POST) -> Entity:
    return Entity("npc", 9, at, GUARD[1])


def see(w: WorldModel, entities: list[Entity], tick: int) -> None:
    """An entity read at ``tick``."""
    w.tick = tick
    w._set_entities(entities, tick)


def post_seen_then_left(w: WorldModel, at=(2, 10)) -> None:
    """The guard stood on its post long enough to keep it, then we walked out of view."""
    w.pos = (15, 10)
    see(w, [guard()], 0)
    see(w, [guard()], POST_STILL_TICKS)
    w.pos = at
    see(w, [], POST_STILL_TICKS + 1)


def gather(w: WorldModel, m: Memory, op: dict, **kw):
    return gather_outcome(w, m, policy(**kw), op=op)


class SightingsTest(unittest.TestCase):
    def test_a_guard_that_stands_still_keeps_a_post_out_of_view(self):
        w = field()
        post_seen_then_left(w)
        s = w.sightings[("npc", 9)]
        self.assertTrue(s.post)
        self.assertEqual(s.home, POST)
        self.assertIn((POST, policy().hostile_range + 1), known_reach(w, policy()))

    def test_a_post_with_the_guard_gone_from_it_fades_fast_and_is_forgotten(self):
        """Free-play run 9 (A85): a post in sight and empty halves every second looked at."""
        w = field()
        post_seen_then_left(w)
        w.pos = (17, 10)  # the post is in sight and nobody is on it
        t = POST_STILL_TICKS + 1
        for _ in range(EMPTY_POST_HALF_LIFE_TICKS):
            t += 1
            see(w, [], t)
        self.assertEqual(known_reach(w, policy()), [], "half strength: it holds no ground")
        self.assertIn(("npc", 9), w.sightings)
        for _ in range(3 * EMPTY_POST_HALF_LIFE_TICKS):
            t += 1
            see(w, [], t)
        self.assertNotIn(("npc", 9), w.sightings)

    def test_a_dead_guard_is_forgotten(self):
        w = field()
        post_seen_then_left(w)
        w.learn_threat([{"kind": "NPCDied", "npc_id": 9, "npc_type": GUARD[1]}], [])
        self.assertNotIn(("npc", 9), w.sightings)

    def test_a_passer_by_is_remembered_for_a_while(self):
        w = field()
        w.pos = (15, 10)
        see(w, [guard()], 0)  # seen once, never still: no post
        w.pos = (2, 10)
        see(w, [], 1)
        self.assertEqual(known_reach(w, policy()), [(POST, policy().hostile_range + 1)])
        see(w, [], SIGHTING_TICKS + 2)
        self.assertEqual(known_reach(w, policy()), [])

    def test_a_sighting_on_a_map_left_behind_expires_but_a_post_is_kept(self):
        w = field()
        w.pos = (15, 10)
        see(w, [Entity("npc", 8, (16, 16), GUARD[1])], 0)  # a passer-by
        post_seen_then_left(w)
        w.map_id = MAP + 1
        see(w, [], SIGHTING_TICKS + 1)
        self.assertEqual(set(w.sightings), {("npc", 9)})

    def test_a_type_not_known_hostile_holds_no_ground(self):
        w = field()
        w.hostile_types.clear()
        post_seen_then_left(w)
        self.assertEqual(known_reach(w, policy()), [])


class ReachFromHitsTest(unittest.TestCase):
    def hit(self, w: WorldModel, at, tick: int) -> None:
        w.pos = at
        events = [{"kind": "Damaged", "amount": 2, "source_kind": "npc", "source_id": 9, "tick": tick}]
        w.apply_events([{"tick": tick, "events": events}])
        w.learn_threat(events, w.entities)

    def test_a_hit_stretches_the_guards_reach_from_its_post(self):
        w = field()
        post_seen_then_left(w)
        see(w, [guard((15, 10))], 60)  # it left its post to come for us
        self.hit(w, (15, 11), 61)
        self.assertEqual(w.sightings[("npc", 9)].reach, 5)
        self.assertIn((POST, 6), known_reach(w, policy()))

    def test_a_hit_on_the_way_to_a_pile_drops_it(self):
        """The guard's type is not known hostile yet: the pile is free ground until it hits us."""
        w, m = field(at=(10, 10), perception=12), Memory()
        w.hostile_types.clear()
        pile = Entity("supply", 50, (25, 10), "gem")
        see(w, [guard((20, 10)), pile], 0)
        see(w, [guard((20, 10)), pile], POST_STILL_TICKS)  # it keeps a post
        gather(w, m, {"op": "gather_gems", "count": 5})
        self.assertEqual(m.gather_target, ("pile", (25, 10)))
        see(w, [guard((15, 10)), pile], 60)
        self.hit(w, (14, 10), 61)  # 6 from its post
        gather(w, m, {"op": "gather_gems", "count": 5})
        self.assertNotEqual(m.gather_target, ("pile", (25, 10)))

    def test_a_roamer_that_hit_us_holds_no_cell_for_the_run(self):
        """With no post, the cell it was first seen on is no ground it keeps."""
        w = field()
        w.pos = (15, 10)
        see(w, [guard()], 0)
        see(w, [guard((15, 11))], 5)
        self.hit(w, (15, 12), 6)
        self.assertEqual(w.sightings[("npc", 9)].reach, 0)
        w.pos = (2, 10)
        see(w, [], 7)
        self.assertEqual(known_reach(w, policy()), [((15, 11), 6)])  # last seen, bar of the one that hit us
        see(w, [], SIGHTING_TICKS + 8)
        self.assertEqual(known_reach(w, policy()), [])


class GatherKeepsClearTest(unittest.TestCase):
    def test_a_pile_beside_a_remembered_post_is_not_free_ground(self):
        w = field()
        post_seen_then_left(w)
        self.assertFalse(gather_ground(w, PILE, policy()))
        self.assertTrue(gather_ground(w, PILE, policy(), danger(w, policy(), fight=True)))

    def test_gather_walks_to_another_pile(self):
        w, m = field(), Memory()
        post_seen_then_left(w)
        w.entities = [Entity("supply", 50, PILE, "gem"), Entity("supply", 51, (2, 35), "gem")]
        gather(w, m, {"op": "gather_gems", "count": 5})
        self.assertEqual(m.gather_target, ("pile", (2, 35)))

    def test_gather_fights_for_it_when_the_op_says_so(self):
        w, m = field(), Memory()
        post_seen_then_left(w)
        w.entities = [Entity("supply", 50, PILE, "gem"), Entity("supply", 51, (2, 35), "gem")]
        gather(w, m, {"op": "gather_gems", "count": 5, "fight": True})
        self.assertEqual(m.gather_target, ("pile", PILE))

    def test_a_pile_reached_only_past_the_post_is_not_taken(self):
        """A corridor runs past the post: the pile beyond it is clear, the way there is not."""
        w, m = field(), Memory()
        for x in range(-1, 41):
            for y in range(-1, 41):
                if y != 10 or x in (-1, 40):
                    w.view.tiles[(x, y)] = "wall"  # walled round, so no way through fog either
        w.view.tiles[(20, 9)] = "dirt"  # the post, beside the corridor
        w.pos = (16, 10)
        see(w, [guard((20, 9))], 0)
        see(w, [guard((20, 9))], POST_STILL_TICKS)
        w.pos = (2, 10)
        see(w, [], POST_STILL_TICKS + 1)
        far = (30, 10)
        self.assertTrue(gather_ground(w, far, policy()))
        self.assertFalse(route_clear(w, policy(), [(x, 10) for x in range(3, 31)]))
        w.entities = [Entity("supply", 50, far, "gem")]
        gather(w, m, {"op": "gather_gems", "count": 5})
        self.assertIsNone(m.gather_target)

    def test_reach_where_we_stand_does_not_bar_the_way_out(self):
        w = field()
        post_seen_then_left(w)
        w.pos = (19, 12)  # inside the post's reach
        self.assertTrue(route_clear(w, policy(), [(19, y) for y in range(13, 21)]))


    def test_a_walk_whose_way_ahead_enters_reach_is_replanned(self):
        """Walled to one corridor: a guard seen on it after the walk began ends the walk."""
        w, m = field(), Memory()
        for x in range(-1, 41):
            for y in range(-1, 41):
                if y != 10 or x in (-1, 40):
                    w.view.tiles[(x, y)] = "wall"
        w.entities = [Entity("supply", 50, (30, 10), "gem")]
        gather(w, m, {"op": "gather_gems", "count": 5})
        self.assertEqual(m.gather_target, ("pile", (30, 10)))
        w.entities = [Entity("supply", 50, (30, 10), "gem"), guard((8, 10))]
        gather(w, m, {"op": "gather_gems", "count": 5})
        self.assertIsNone(m.gather_target)


class HeadingOutKeepsClearTest(unittest.TestCase):
    def test_no_walk_out_of_safe_ground_through_reach(self):
        """Every field cell lies past the post: Gather does not head out through its reach."""
        w, m = field(), Memory()
        for x in range(-1, 41):
            for y in range(-1, 41):
                if y != 10 or x in (-1, 40):
                    w.view.tiles[(x, y)] = "wall"
        for x in range(18):
            apply_zone(w, MAP, x, 10, {"safe": True, "brightness": 1})
        w.view.tiles[(20, 9)] = "dirt"
        w.pos = (14, 10)
        see(w, [guard((20, 9))], 0)
        see(w, [guard((20, 9))], POST_STILL_TICKS)
        w.pos = (2, 10)
        see(w, [], POST_STILL_TICKS + 1)
        gather(w, m, {"op": "gather_gems", "count": 5})
        self.assertIsNone(m.gather_target)
        gather(w, m, {"op": "gather_gems", "count": 5, "fight": True})
        self.assertEqual(m.gather_target, ("out", (18, 10)))


class PilesHonourTheRegionTest(unittest.TestCase):
    """The op names region (1, 0); grass at (20, 10) gives Gather a cell to cut there."""

    def worked(self) -> tuple[WorldModel, Memory]:
        w, m = field(perception=30), Memory()
        w.view.tiles[(20, 10)] = "grass"
        return w, m

    def test_a_pile_outside_the_named_region_is_not_the_target(self):
        w, m = self.worked()
        w.entities = [Entity("supply", 50, (5, 10), "gem"), Entity("supply", 51, (25, 12), "gem")]
        gather(w, m, {"op": "gather_gems", "count": 5, "x": 20, "y": 10})
        self.assertEqual(m.gather_target, ("pile", (25, 12)))

    def test_with_no_pile_in_the_named_region_its_grass_is_worked(self):
        w, m = self.worked()
        w.entities = [Entity("supply", 50, (5, 10), "gem")]
        gather(w, m, {"op": "gather_gems", "count": 5, "x": 20, "y": 10})
        self.assertEqual(m.gather_target, ("grass", (20, 10)))

    def test_a_kept_pile_outside_a_newly_named_region_is_let_go(self):
        w, m = self.worked()
        w.entities = [Entity("supply", 50, (5, 10), "gem")]
        gather(w, m, {"op": "gather_gems", "count": 5})
        self.assertEqual(m.gather_target, ("pile", (5, 10)))
        gather(w, m, {"op": "gather_gems", "count": 5, "x": 20, "y": 10})
        self.assertEqual(m.gather_target, ("grass", (20, 10)))

    def test_a_pile_in_reach_is_taken_wherever_it_lies(self):
        w, m = self.worked()
        w.entities = [Entity("supply", 50, (3, 10), "gem")]
        out = gather(w, m, {"op": "gather_gems", "count": 5, "x": 20, "y": 10})
        self.assertEqual(out.intents[0].get("verb"), "Take")

    def test_with_nothing_to_cut_in_the_named_region_piles_anywhere_count(self):
        """Gather works as usual once it knows no cell to cut there, piles included."""
        w, m = field(perception=30), Memory()
        w.entities = [Entity("supply", 50, (5, 10), "gem")]
        gather(w, m, {"op": "gather_gems", "count": 5, "x": 20, "y": 10})
        self.assertEqual(m.gather_target, ("pile", (5, 10)))


class DetourKeepsClearTest(unittest.TestCase):
    def ctx(self, m: Memory, ops: list[dict]) -> PlayContext:
        return PlayContext(m, policy(pickup=True), random.Random(0), plan=Plan(ops, dict(PARAM_DEFAULTS)))

    def walking_past_the_post(self) -> tuple[WorldModel, Memory]:
        w, m = field(at=(14, 12)), Memory()
        post_seen_then_left(w, at=(14, 12))
        m.goal, m.path = "travel", [(x, 12) for x in range(15, 35)]
        w.entities = [Entity("supply", 70, PILE, "gem")]
        return w, m

    def test_a_find_beside_a_remembered_post_is_skipped(self):
        w, m = self.walking_past_the_post()
        self.assertIsNone(detour_find(w, self.ctx(m, [{"op": "travel", "to": "point", "x": 35, "y": 12}])))

    def test_unless_the_op_fights_for_it(self):
        w, m = self.walking_past_the_post()
        find = detour_find(w, self.ctx(m, [{"op": "gather_gems", "count": 5, "fight": True}]))
        self.assertIsNotNone(find)
        self.assertEqual(find.id, 70)

    def test_a_hit_on_the_way_ends_the_detour(self):
        w, m = field(at=(14, 12)), Memory()
        w.hostile_types.clear()
        m.goal, m.path = "travel", [(x, 12) for x in range(15, 35)]
        gem = Entity("supply", 70, PILE, "gem")
        see(w, [guard(), gem], 0)
        see(w, [guard(), gem], POST_STILL_TICKS)  # a post, not yet known hostile
        c = self.ctx(m, [{"op": "travel", "to": "point", "x": 35, "y": 12}])
        DetourState().act(w, c)
        self.assertIsNotNone(m.detour)
        ReachFromHitsTest().hit(w, (14, 12), POST_STILL_TICKS + 1)  # 6 from its post: the find is in its reach
        DetourState().act(w, c)
        self.assertIsNone(m.detour)

    def test_a_find_reached_only_through_reach_is_skipped(self):
        """Row 12 joins the walk's row 10 only at (20, 11), inside the post's reach; the find is outside it."""
        w, m = field(at=(12, 10)), Memory()
        for x in range(-1, 41):
            for y in range(-1, 41):
                if y not in (10, 12) or x in (-1, 40):
                    w.view.tiles[(x, y)] = "wall"
        w.view.tiles[(20, 11)] = "dirt"
        w.view.tiles[(19, 14)] = "dirt"
        w.pos = (19, 20)
        see(w, [guard((19, 14))], 0)
        see(w, [guard((19, 14))], POST_STILL_TICKS)
        w.pos = (12, 10)
        find = Entity("supply", 70, (24, 12), "gem")
        see(w, [find], POST_STILL_TICKS + 1)
        m.goal, m.path = "travel", [(x, 10) for x in range(13, 36)]
        c = self.ctx(m, [{"op": "travel", "to": "point", "x": 35, "y": 10}])
        self.assertEqual(detour_find(w, c), find)  # its ground is clear
        DetourState().act(w, c)
        self.assertIsNone(m.detour)
        self.assertIn(70, m.detour_skipped)


class FightFieldTest(unittest.TestCase):
    def test_fight_must_be_true_or_false(self):
        self.assertIsNotNone(validate_goal_op({"op": "gather_gems", "count": 5, "fight": True}))
        self.assertIsNone(validate_goal_op({"op": "gather_gems", "count": 5, "fight": "yes"}))


if __name__ == "__main__":
    unittest.main()
