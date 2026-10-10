"""Free-play run 2 offline: 10 minutes, no deaths, gems 6 → 17 and nothing bought.

Rebuilt here (A16, A9, A10, A66, A22, A63):

1. One pacing give-up on ``travel:town`` banned the town cell for the rest of
   the run, so the potion buy was dropped. A give-up on a hub (town, shop)
   now lapses, by time or by moving well off, and the planner sees when.
2. Park, Retreat and Heal aimed at safe tiles walled off by bush and wall,
   with no fallback. They now head for the nearest safe cell a path reaches,
   else the town cell.
3. Gather cut 54 times for 1 gem in one region while the next region over
   gave 1 gem in 4, and a ``gather_gems`` x, y could not send it there. The
   op's x, y now names a target region, and Gather leaves a poor region for a
   better known one by itself.
"""

from __future__ import annotations

import json
import unittest

from agentrealm_agent.config import Policy
from agentrealm_agent.directives import PARAM_DEFAULTS, Directives
from agentrealm_agent.gem_yield import (
    FAIR_SAMPLE_CUTS,
    REGION_SIZE,
    better_region,
    poor_regions,
    record_cut,
)
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.memory import Memory
from agentrealm_agent.navigation import stuck as nav_stuck
from agentrealm_agent.pathing import SAFE_GOAL_CHECKS, SAFE_THREATENED_TICKS, grid_params, reachable_safe_goal
from agentrealm_agent.plan import Plan
from agentrealm_agent.runner import Runner
from agentrealm_agent.states import dispatch
from agentrealm_agent.states.fight import retreat_tail
from agentrealm_agent.states.gather import gather_outcome
from agentrealm_agent.states.retreat import retreat_step
from agentrealm_agent.strategist import build_prompt, hub_give_up_lines
from agentrealm_agent.travel import sync_town
from agentrealm_agent.world import Entity, WorldModel
from agentrealm_agent.zone_discovery import apply_zone
from tests.test_strategist import FakeLLM, fake_runner, make, round_trip
from tests.test_survival_runs import MAP, ctx, hit, world

TOWN = (9, 9)
TOWN_OP = {"op": "travel", "to": "town", "x": 0, "y": 0}


def give_up(m: Memory, w: WorldModel, goal: str, cell) -> None:
    nav_stuck.give_up(m, w, nav_stuck.track(m, w, goal, cell), "pacing")


def elsewhere(w: WorldModel) -> WorldModel:
    """``w`` as seen from another map: every hub give-up lapses."""
    return WorldModel(character_id=w.character_id, map_id=-1, pos=(0, 0), tick=w.tick)


class HubGiveUpLapsesTest(unittest.TestCase):
    def setUp(self):
        self.w = WorldModel(character_id=1, map_id=MAP, pos=(0, 0), tick=100)
        self.m = Memory()

    def test_a_town_give_up_lapses_after_the_cooldown(self):
        give_up(self.m, self.w, "travel:town", TOWN)
        stuck = self.m.nav_stuck
        self.assertIn((MAP, TOWN), stuck.given_up_travel)
        self.w.tick += nav_stuck.HUB_GIVE_UP_TICKS - 1
        self.assertEqual(nav_stuck.expire_hub_give_ups(stuck, self.w), [])
        self.w.tick += 1
        self.assertEqual(nav_stuck.expire_hub_give_ups(stuck, self.w), [(MAP, TOWN)])
        self.assertNotIn((MAP, TOWN), stuck.given_up_travel)
        self.assertEqual(stuck.given_up_hubs, {})

    def test_a_shop_give_up_lapses_once_the_agent_moved_well_off(self):
        give_up(self.m, self.w, "travel:shop", TOWN)
        stuck = self.m.nav_stuck
        self.w.pos = (nav_stuck.HUB_GIVE_UP_CELLS - 1, 0)
        self.assertEqual(nav_stuck.expire_hub_give_ups(stuck, self.w), [])
        self.w.pos = (0, -nav_stuck.HUB_GIVE_UP_CELLS)
        self.assertEqual(nav_stuck.expire_hub_give_ups(stuck, self.w), [(MAP, TOWN)])

    def test_travel_walks_to_town_again_once_its_give_up_lapses(self):
        # Review on #140: a lapse left Travel's backoff on, so it would not move.
        w, c = world(), ctx()
        w.respawn_anchors = [(MAP, (20, 10))]
        c.plan = Plan([dict(TOWN_OP)], dict(PARAM_DEFAULTS))
        for _ in range(4):  # from the 4th give-up the backoff outlasts the cooldown
            give_up(c.memory, w, "travel:town", (20, 10))
            nav_stuck.expire_hub_give_ups(c.memory.nav_stuck, elsewhere(w))
        give_up(c.memory, w, "travel:town", (20, 10))
        self.assertTrue(nav_stuck.backed_off(c.memory, "travel:town", MAP, (20, 10), w.tick))
        w.tick += nav_stuck.HUB_GIVE_UP_TICKS
        self.assertEqual(nav_stuck.expire_hub_give_ups(c.memory.nav_stuck, w), [(MAP, (20, 10))])
        self.assertFalse(nav_stuck.backed_off(c.memory, "travel:town", MAP, (20, 10), w.tick))
        out = dispatch(w, c)
        self.assertEqual(out.state, "Travel")
        self.assertEqual(out.intents[0]["verb"], "SetPosition", out.reason)

    def test_a_distance_lapse_ends_the_backoff_too(self):
        give_up(self.m, self.w, "travel:town", TOWN)
        self.w.pos = (0, nav_stuck.HUB_GIVE_UP_CELLS)
        nav_stuck.expire_hub_give_ups(self.m.nav_stuck, self.w)
        self.assertFalse(nav_stuck.backed_off(self.m, "travel:town", MAP, TOWN, self.w.tick))

    def test_a_give_up_with_no_position_known_waits_out_its_cooldown(self):
        att = nav_stuck.track(self.m, self.w, "travel:town", TOWN)
        self.w.pos = None  # unknown at the give-up, as after a warp
        nav_stuck.give_up(self.m, self.w, att, "pacing")
        self.w.pos = (0, 0)
        self.assertEqual(nav_stuck.expire_hub_give_ups(self.m.nav_stuck, self.w), [])

    def test_a_hub_cell_given_up_as_a_point_too_holds_for_the_run(self):
        give_up(self.m, self.w, "travel:town", TOWN)
        give_up(self.m, self.w, "travel:point", TOWN)
        self.w.tick += nav_stuck.HUB_GIVE_UP_TICKS
        self.assertEqual(nav_stuck.expire_hub_give_ups(self.m.nav_stuck, self.w), [])
        self.assertIn((MAP, TOWN), self.m.nav_stuck.given_up_travel)

    def test_a_point_give_up_holds_for_the_run(self):
        give_up(self.m, self.w, "travel:point", TOWN)
        self.w.tick += 100 * nav_stuck.HUB_GIVE_UP_TICKS
        self.w.pos = (500, 500)
        self.assertEqual(nav_stuck.expire_hub_give_ups(self.m.nav_stuck, self.w), [])
        self.assertIn((MAP, TOWN), self.m.nav_stuck.given_up_travel)

    def test_the_planner_sees_the_give_up_and_when_it_lapses(self):
        give_up(self.m, self.w, "travel:town", TOWN)
        lines = hub_give_up_lines(self.m.nav_stuck)
        self.assertEqual(
            lines,
            [{"cell": f"{MAP}:9,9", "retry_at_tick": 100 + nav_stuck.HUB_GIVE_UP_TICKS, "or_after_moving_cells": nav_stuck.HUB_GIVE_UP_CELLS, "from": "0,0"}],
        )
        messages = build_prompt(
            triggers=[],
            w=self.w,
            plan=Plan([], dict(PARAM_DEFAULTS)),
            directives=Directives(params=dict(PARAM_DEFAULTS)),
            knowledge=None,
            given_up_travel=self.m.nav_stuck.given_up_travel,
            given_up_hubs=lines,
        )
        self.assertIn(f"given_up_hubs={json.dumps(lines, sort_keys=True)}", messages[1]["content"])
        self.assertIn("given_up_hubs, with when that give-up lapses", messages[0]["content"])

    def test_a_town_travel_is_filtered_until_its_give_up_lapses(self):
        s = make(FakeLLM({"goals": [TOWN_OP]}, {"goals": [TOWN_OP]}))
        r = fake_runner()
        r.world.respawn_anchors = [(7, TOWN)]
        give_up(r.mem, r.world, "travel:town", TOWN)
        r.mem.nav_stuck.stuck_signals.clear()
        round_trip(s, r)
        self.assertIsNone(r.plan.current(), "given up: the re-sent travel:town never reaches the stack")
        r.world.tick += nav_stuck.HUB_GIVE_UP_TICKS
        Runner.expire_hub_give_ups(r)
        r.log.assert_called()
        s.clock.now += 60
        round_trip(s, r)
        self.assertEqual(r.plan.current()["to"], "town", "lapsed: the planner may send it again")


def wall_in(w: WorldModel, cell) -> None:
    """Wall every neighbour of ``cell`` (bush on one side, as in the run)."""
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            if (dx, dy) != (0, 0):
                w.view.tiles[(cell[0] + dx, cell[1] + dy)] = "bush" if dx == 1 else "wall"


WALLED = (6, 10)  # nearer than OPEN to the agent at (10, 10)
OPEN = (-5, 10)


def safe(w: WorldModel, *cells) -> None:
    for cell in cells:
        apply_zone(w, MAP, cell[0], cell[1], {"safe": True})


class ReachableSafeGoalTest(unittest.TestCase):
    def test_skips_a_walled_off_safe_tile_and_remembers_it(self):
        w, m = world(), Memory()
        wall_in(w, WALLED)
        params = grid_params(Policy(kind="scripted"), set(), set())
        self.assertEqual(reachable_safe_goal(m, w, [WALLED, OPEN], params, None), OPEN)
        self.assertIn((MAP, WALLED), m.safe_unreachable)

    def test_a_boxed_in_start_marks_no_safe_cell_unreachable(self):
        # Review on #140: a search that runs out from our side proves only
        # that we stand shut in (here behind a bush Break could open).
        w, m = world(), Memory()
        wall_in(w, (10, 10))
        params = grid_params(Policy(kind="scripted"), set(), set())
        reachable_safe_goal(m, w, [OPEN, (14, 10)], params, (-20, 10))
        self.assertEqual(m.safe_unreachable, {})
        w.pos = (20, 10)
        self.assertEqual(reachable_safe_goal(m, w, [OPEN, (14, 10)], params, (-20, 10)), OPEN)

    def test_falls_back_to_town(self):
        w, m = world(), Memory()
        wall_in(w, WALLED)
        params = grid_params(Policy(kind="scripted"), set(), set())
        self.assertEqual(reachable_safe_goal(m, w, [WALLED], params, (-20, 10)), (-20, 10))
        self.assertIsNone(reachable_safe_goal(m, w, [WALLED], params, None))

    def test_past_the_checks_the_nearest_candidate_is_still_tried(self):
        w, m = world(), Memory()
        far = [(-20, y) for y in range(0, 2 * SAFE_GOAL_CHECKS, 2)]
        for cell in far:
            wall_in(w, cell)
        params = grid_params(Policy(kind="scripted"), set(), set())
        self.assertEqual(reachable_safe_goal(m, w, far + [(25, 10), (12, 10)], params, None), (12, 10))

    def test_skips_a_safe_tile_heal_gave_up_on(self):
        w, m = world(), Memory()
        give_up(m, w, "heal_rest", (8, 10))
        params = grid_params(Policy(kind="scripted"), set(), set())
        self.assertEqual(reachable_safe_goal(m, w, [(8, 10), OPEN], params, None), OPEN)


class SafeWalksPickAReachableTileTest(unittest.TestCase):
    def test_retreat_heads_for_the_nearest_reachable_safe_tile(self):
        w, c = world(health=4), ctx(on_hostile="fight")
        w.entities = [Entity("npc", 7, (11, 10), code="chaser")]
        hit(w)
        safe(w, WALLED, OPEN)
        wall_in(w, WALLED)
        out = dispatch(w, c)
        self.assertEqual((out.state, out.reason), ("Retreat", f"retreat → safe {OPEN}"))

    def test_retreat_falls_back_to_town_when_no_safe_tile_is_reachable(self):
        kb = KnowledgeBase("sandbox")
        sync_town(kb, {"map_id": MAP, "x": -20, "y": 10})
        w, c = world(health=4), ctx(on_hostile="fight", kb=kb)
        w.entities = [Entity("npc", 7, (11, 10), code="chaser")]
        hit(w)
        safe(w, WALLED)
        wall_in(w, WALLED)
        out = dispatch(w, c)
        self.assertEqual((out.state, out.reason), ("Retreat", "retreat → safe (-20, 10)"))

    def test_an_escape_cell_never_marks_its_safe_tile_unreachable(self):
        # Review on #140: a one-shot oscillation escape on the pocket's only
        # entrance, or on the goal itself, is no proof the tile is walled in.
        for paced in ({(7, 10)}, {WALLED}):
            w, c = world(health=4), ctx(on_hostile="fight")
            w.entities = [Entity("npc", 7, (11, 10), code="chaser")]
            hit(w)
            safe(w, WALLED)
            wall_in(w, WALLED)
            w.view.tiles[(7, 10)] = "dirt"  # the pocket's only way in
            retreat_step(w, c, "Retreat", paced=paced)
            self.assertNotIn((MAP, WALLED), c.memory.safe_unreachable, paced)
            self.assertEqual(dispatch(w, c).reason, f"retreat → safe {WALLED}", "next decision walks in")

    def test_fight_retreat_tail_heads_where_retreat_does(self):
        w, c = world(health=4), ctx(on_hostile="fight")
        safe(w, WALLED, OPEN)
        wall_in(w, WALLED)
        self.assertTrue(retreat_tail(w, c.memory, c.policy, c, limit=3))
        self.assertEqual(c.memory.path[-1], OPEN)

    def test_heal_walks_to_a_reachable_safe_tile(self):
        w, c = world(health=4), ctx()
        safe(w, WALLED, OPEN)
        wall_in(w, WALLED)
        self.assertEqual(dispatch(w, c).reason, f"heal_measure → {OPEN}")

    def test_park_walks_to_a_reachable_safe_tile(self):
        w, c = world(), ctx()
        c.memory.parking = True
        safe(w, WALLED, OPEN)
        wall_in(w, WALLED)
        out = dispatch(w, c)
        self.assertEqual((out.state, out.reason), ("Park", f"retreat → safe {OPEN}"))


class SafePickSkipsHostileReachTest(unittest.TestCase):
    """A63 run 4: Heal walked to the nearest safe tile with a known-hostile
    pack beside it and died. The pick skips safe tiles in a known hostile's
    reach (``survival.hostile_reach``), then falls back to the next reachable
    safe tile, then the town cell."""

    PACKED = (6, 10)  # nearer than OPEN to the agent at (10, 10)
    PACK = [Entity("npc", 30 + i, p, code="wartlurch") for i, p in enumerate([(6, 12), (5, 12), (7, 13)])]

    def packed(self, w: WorldModel) -> None:
        """The pack in view, of a type seen attacking: known hostile."""
        w.hostile_types.add(("npc", "wartlurch"))
        w.entities.extend(self.PACK)

    def test_skips_a_safe_tile_in_a_hostiles_reach(self):
        w, m = world(), Memory()
        params = grid_params(Policy(kind="scripted"), set(), set())
        pack = {("npc", 30): {self.PACKED}}
        self.assertEqual(reachable_safe_goal(m, w, [self.PACKED, OPEN], params, None, pack), OPEN)
        self.assertNotIn((MAP, self.PACKED), m.safe_unreachable, "a threat is not a wall")
        self.assertEqual(m.safe_threatened, {(MAP, self.PACKED): {("npc", 30): w.tick}})
        self.assertEqual(reachable_safe_goal(m, w, [self.PACKED, OPEN], params, None), OPEN, "remembered out of view")
        w.tick += SAFE_THREATENED_TICKS
        self.assertEqual(reachable_safe_goal(m, w, [self.PACKED, OPEN], params, None), self.PACKED)
        self.assertEqual(m.safe_threatened, {}, "a lapsed mark is dropped")

    def test_with_every_safe_tile_threatened_it_falls_back_to_town(self):
        w, m = world(), Memory()
        params = grid_params(Policy(kind="scripted"), set(), set())
        pack = {("npc", 30): {self.PACKED}, ("npc", 31): {OPEN}}
        self.assertEqual(reachable_safe_goal(m, w, [self.PACKED, OPEN], params, (-20, 10), pack), (-20, 10))
        self.assertIsNone(reachable_safe_goal(m, w, [self.PACKED, OPEN], params, None, pack))

    def test_heal_skips_the_safe_tile_beside_a_pack(self):
        w, c = world(health=4), ctx()
        safe(w, self.PACKED, OPEN)
        self.packed(w)
        out = dispatch(w, c)
        self.assertEqual((out.state, out.reason), ("Heal", f"heal_measure → {OPEN}"))

    def test_heal_keeps_its_pick_when_the_pack_leaves_view(self):
        # Review on #147: with the pack out of view the nearer tile looked
        # safe again, so the walk turned back toward the pack and flip-flopped.
        w, c = world(health=4), ctx()
        safe(w, self.PACKED, OPEN)
        self.packed(w)
        self.assertEqual(dispatch(w, c).reason, f"heal_measure → {OPEN}")
        w.entities = []
        w.tick += 10
        self.assertEqual(dispatch(w, c).reason, f"heal_measure → {OPEN}")
        w.tick += SAFE_THREATENED_TICKS
        self.assertEqual(dispatch(w, c).reason, f"heal_measure → {self.PACKED}", "tried again once the mark lapses")

    def test_retreat_keeps_its_pick_when_the_pack_leaves_view(self):
        w, c = world(health=4), ctx(on_hostile="fight")
        w.entities = [Entity("npc", 7, (11, 10), code="chaser")]
        hit(w)
        self.packed(w)
        safe(w, self.PACKED, OPEN)
        self.assertEqual(dispatch(w, c).reason, f"retreat → safe {OPEN}")
        w.entities = [Entity("npc", 7, (11, 10), code="chaser")]
        w.tick += 10
        c.memory.held_queue = None
        self.assertEqual(dispatch(w, c).reason, f"retreat → safe {OPEN}")

    def test_retreat_skips_a_tile_heal_marked_beside_its_chaser(self):
        # Free-play run 7: the chaser's reach rules a tile out too, so Heal's
        # mark from before it hit us still holds once Retreat runs from it.
        w, c = world(health=4), ctx(on_hostile="fight")
        safe(w, (12, 10), OPEN)
        w.hostile_types.add(("npc", "chaser"))
        w.entities = [Entity("npc", 7, (13, 10), code="chaser")]  # out of hostile_range: Heal runs
        self.assertEqual(dispatch(w, c).reason, f"heal_measure → {OPEN}")
        w.entities = [Entity("npc", 7, (11, 10), code="chaser")]
        hit(w)
        out = dispatch(w, c)
        self.assertEqual((out.state, out.reason), ("Retreat", f"retreat → safe {OPEN}"))

    def test_heal_falls_back_to_town_when_every_safe_tile_is_beside_a_pack(self):
        kb = KnowledgeBase("sandbox")
        sync_town(kb, {"map_id": MAP, "x": 20, "y": 10})
        w, c = world(health=4), ctx(kb=kb)
        safe(w, self.PACKED)
        self.packed(w)
        out = dispatch(w, c)
        self.assertEqual((out.state, out.reason), ("Heal", "heal_measure → (20, 10)"))

    def test_retreat_skips_a_safe_tile_beside_a_pack_it_is_not_running_from(self):
        w, c = world(health=4), ctx(on_hostile="fight")
        w.entities = [Entity("npc", 7, (11, 10), code="chaser")]
        hit(w)
        self.packed(w)
        safe(w, self.PACKED, OPEN)
        out = dispatch(w, c)
        self.assertEqual((out.state, out.reason), ("Retreat", f"retreat → safe {OPEN}"))

    def test_retreat_skips_a_safe_tile_beside_its_chaser(self):
        # Free-play run 7: a safe tile 2 cells from the hostile Retreat ran
        # from drew it back into 6 hits. The chaser's reach rules it out too.
        w, c = world(health=4), ctx(on_hostile="fight")
        w.entities = [Entity("npc", 7, (11, 10), code="chaser")]
        hit(w)
        safe(w, (12, 10), OPEN)
        out = dispatch(w, c)
        self.assertEqual((out.state, out.reason), ("Retreat", f"retreat → safe {OPEN}"))

    def test_fight_retreat_tail_skips_it_too(self):
        w, c = world(health=4), ctx(on_hostile="fight")
        self.packed(w)
        safe(w, self.PACKED, OPEN)
        self.assertTrue(retreat_tail(w, c.memory, c.policy, c, limit=3))
        self.assertEqual(c.memory.path[-1], OPEN)


GEM_MAP = 7


def gem_world(at=(40, 8)) -> WorldModel:
    """Grass over three regions in a row: x 0–15, 16–31 and 32–47."""
    w = WorldModel(character_id=1, map_id=GEM_MAP, pos=at, perception=5)
    w.alive, w.tick = True, 10_000
    for x in range(3 * REGION_SIZE):
        for y in range(REGION_SIZE):
            w.view.tiles[(x, y)] = "grass"
    w.terrain_center, w.terrain_map = at, GEM_MAP
    return w


def sample(kb: KnowledgeBase, cell, cuts: int, gems: int) -> None:
    """``cuts`` cuts of ``cell`` long ago, ``gems`` of them with a gem."""
    for i in range(cuts):
        record_cut(kb, GEM_MAP, cell, "grass", -10_000 - i, i < gems)


GATHER = {"op": "gather_gems", "count": 30}
POLICY = Policy(on_hostile="ignore")


class GatherRegionsTest(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase.empty("fake-world")
        sample(self.kb, (40, 8), 54, 1)  # run 2: 54 cuts, 1 gem, where Gather stands
        sample(self.kb, (24, 8), 8, 2)  # the next region over: 1 gem in 4

    def test_poor_and_better_regions(self):
        self.assertEqual(poor_regions(self.kb, GEM_MAP), {(2, 0)})
        self.assertEqual(better_region(self.kb, GEM_MAP, (40, 8)), (1, 0))

    def test_a_poor_region_is_left_for_a_better_known_one(self):
        m = Memory()
        out = gather_outcome(gem_world(), m, POLICY, knowledge=self.kb, op=GATHER)
        self.assertEqual(out.intents[0]["verb"], "SetPosition", "no cut where it stands")
        self.assertEqual(m.gather_target[0], "grass")
        self.assertEqual(m.gather_target[1][0] // REGION_SIZE, 1)
        self.assertEqual(m.gather_status, "walking to grass (region 16,0)")

    def test_in_the_better_region_it_cuts_and_does_not_drift_back(self):
        m = Memory()
        out = gather_outcome(gem_world(at=(31, 8)), m, POLICY, knowledge=self.kb, op=GATHER)
        self.assertEqual(out.reason, "cut grass")
        self.assertEqual(m.gather_status, "cutting")
        w = gem_world(at=(31, 8))
        w.view.tiles[(31, 8)] = "dirt"  # cut: the nearest grass left is in the poor region
        for y in range(REGION_SIZE):
            for x in range(17, 31):
                w.view.tiles[(x, y)] = "dirt"
        gather_outcome(w, m, POLICY, knowledge=self.kb, op=GATHER)
        self.assertNotEqual(m.gather_target[1][0] // REGION_SIZE, 2, "poor ground is skipped")

    def test_with_too_few_cuts_here_it_keeps_cutting(self):
        kb = KnowledgeBase.empty("fake-world")
        sample(kb, (40, 8), FAIR_SAMPLE_CUTS - 1, 1)
        sample(kb, (24, 8), 8, 2)
        out = gather_outcome(gem_world(), Memory(), POLICY, knowledge=kb, op=GATHER)
        self.assertEqual(out.reason, "cut grass")

    def test_with_no_better_region_it_keeps_cutting(self):
        kb = KnowledgeBase.empty("fake-world")
        sample(kb, (40, 8), 54, 1)
        out = gather_outcome(gem_world(), Memory(), POLICY, knowledge=kb, op=GATHER)
        self.assertEqual(out.reason, "cut grass")

    def test_the_op_x_y_names_the_region_gather_walks_to(self):
        m = Memory()
        op = {**GATHER, "x": 3, "y": 3}
        out = gather_outcome(gem_world(at=(24, 8)), m, POLICY, knowledge=self.kb, op=op)
        self.assertEqual(out.intents[0]["verb"], "SetPosition", "the better region underfoot is not the one named")
        self.assertEqual(m.gather_target[1][0] // REGION_SIZE, 0)
        self.assertEqual(m.gather_status, "walking to grass (region 0,0)")

    def test_an_unseen_named_region_is_walked_toward(self):
        m = Memory()
        op = {**GATHER, "x": 4 * REGION_SIZE + 2, "y": 3}
        out = gather_outcome(gem_world(), m, POLICY, knowledge=self.kb, op=op)
        self.assertEqual(out.intents[0]["verb"], "SetPosition")
        self.assertGreater(out.intents[0]["x"], 40, "east, toward the named region")
        self.assertEqual(m.gather_status, "walking to a target region (region 64,0)")

    def test_a_named_region_with_nothing_to_cut_is_worked_as_usual(self):
        w = gem_world(at=(24, 8))
        for x in range(REGION_SIZE):
            for y in range(REGION_SIZE):
                w.view.tiles[(x, y)] = "dirt"
        out = gather_outcome(w, Memory(), POLICY, knowledge=self.kb, op={**GATHER, "x": 3, "y": 3})
        self.assertEqual(out.reason, "cut grass")


if __name__ == "__main__":
    unittest.main()
