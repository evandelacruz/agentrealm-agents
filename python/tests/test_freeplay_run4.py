"""Free-play run 4 offline: detour pricing, travel pacing, gather region churn, Retreat's window (A71, A15, A9).

1. Detour priced each find from the end of the queued walk, not from where
   the character stood: gems one step off the route were priced 5–7 and
   walked past (``pathing.route_ahead``).
2. Travel paced between two cells until oscillation gave up the town: the
   cell search ranked cells by the corridor from one cell and by a straight
   line, or a corridor read from the next tile, from the other
   (``planner._toward``, ``cost_path``).
3. The planner moved the ``gather_gems`` region 4 times in 50 s with no
   reason (``gem_yield.keep_gather_region``).
4. Retreat's stuck window kept counting while a losing Retreat drank or
   fought back (``retreat.no_progress``).
"""

from __future__ import annotations

import random
import unittest

from agentrealm_agent.brain import walkable_prefix
from agentrealm_agent.config import Policy
from agentrealm_agent.directives import PARAM_DEFAULTS
from agentrealm_agent.executor.movement import direction_between
from agentrealm_agent.gem_yield import KEY as GEM_YIELD, region_key
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.memory import Memory
from agentrealm_agent.navigation import stuck as nav_stuck
from agentrealm_agent.navigation import planner
from agentrealm_agent.navigation.planner import CostGridParams, NavSearchState, _toward, cost_path, macro_cell
from agentrealm_agent.plan import Plan, parse_directives_goals
from agentrealm_agent.states import PlayContext, dispatch
from agentrealm_agent.pathing import guided_step, nav_search, route_ahead
from agentrealm_agent.states.detour import DETOUR_EXTRA_STEPS, detour_find, extra_steps
from agentrealm_agent.states.retreat import no_progress
from agentrealm_agent.travel import sync_town
from agentrealm_agent.world import Entity, WorldModel

from tests.test_strategist import FakeLLM, fake_runner, make, round_trip

MAP = 1


def ctx(m: Memory, plan: Plan | None = None, kb: KnowledgeBase | None = None, **policy_kw) -> PlayContext:
    kw = {"goals": [], "on_hostile": "ignore", **policy_kw}
    return PlayContext(m, Policy(kind="scripted", **kw), random.Random(0), plan=plan, knowledge=kb)


def open_field(at=(2, 10), size=40) -> WorldModel:
    w = WorldModel(character_id=1, map_id=MAP, pos=at, perception=12)
    for x in range(size):
        for y in range(size):
            w.view.tiles[(x, y)] = "dirt"
    w.terrain_center, w.terrain_map = at, MAP
    w.health, w.max_health = 100, 100
    return w


class DetourPricedFromHereTest(unittest.TestCase):
    """A find next to the queued part of the route is a detour: it is priced from where we stand."""

    def queued_walk(self, w: WorldModel, m: Memory, steps: int) -> None:
        """The executor sent the first ``steps`` cells of ``m.path`` and cut them off it."""
        cells, m.path = m.path[:steps], m.path[steps:]
        pos, m.pending_intents = w.pos, []
        for c in cells:
            m.pending_intents.append({"verb": "Step", "direction": direction_between(pos, c)})
            pos = c
        m.pending_next_index = 0

    def test_gems_beside_the_queued_steps_are_detoured_to(self):
        w, m = open_field(), Memory()
        plan = Plan([{"op": "travel", "to": "point", "x": 35, "y": 10}], dict(PARAM_DEFAULTS))
        self.assertEqual(dispatch(w, ctx(m, plan, pickup=True)).state, "Travel")
        self.queued_walk(w, m, 10)
        w.entities = [Entity("supply", 70 + i, (4 + i, 12), "gem") for i in range(3)]  # 2 steps off cells 2–4
        find = detour_find(w, ctx(m, plan, pickup=True))
        self.assertIsNotNone(find, "a gem beside the route is worth the steps")
        self.assertEqual(find.id, 70)

    def test_a_find_beside_the_route_always_fits(self):
        route = [(x, 10) for x in range(3, 30)]
        for x in range(3, 30):
            for dy in (-1, 1):
                self.assertLessEqual(extra_steps((2, 10), (x, 10 + dy), route), 2)
        self.assertLessEqual(extra_steps((2, 10), (8, 12), route), DETOUR_EXTRA_STEPS)

    def test_the_route_ahead_starts_with_the_queued_steps(self):
        w, m = open_field(), Memory()
        plan = Plan([{"op": "travel", "to": "point", "x": 35, "y": 10}], dict(PARAM_DEFAULTS))
        dispatch(w, ctx(m, plan, pickup=True))
        whole = list(m.path)
        self.queued_walk(w, m, 10)
        self.assertEqual(route_ahead(w, m), whole)


# Two maps from an offline sweep of walled fields between a start and the
# town cell, each a list of wall segments (x, y, length, horizontal). Before
# the fix Travel paced between two cells on both (free-play run 4).
TOWN = (397, 401)
PACING_MAPS = {
    "corridor then straight line": (
        (330, 456),
        4,
        [(349, 431, 4, 0), (369, 446, 7, 0), (344, 383, 5, 0), (420, 386, 3, 1), (377, 447, 13, 0), (351, 430, 25, 1),
         (359, 440, 17, 0), (398, 449, 22, 1), (368, 423, 19, 1), (375, 399, 6, 0), (348, 457, 13, 1), (391, 403, 15, 1),
         (366, 394, 21, 1), (418, 420, 5, 0), (354, 427, 20, 1), (407, 452, 23, 0), (361, 449, 9, 1), (352, 423, 8, 0),
         (363, 418, 15, 0), (342, 446, 23, 0), (365, 410, 14, 0), (349, 430, 17, 0), (357, 428, 20, 0), (375, 440, 15, 0),
         (359, 429, 24, 1), (356, 428, 4, 1), (365, 395, 15, 0), (375, 458, 19, 0), (347, 431, 10, 1), (403, 419, 12, 0),
         (370, 380, 22, 1), (390, 414, 4, 0), (407, 442, 4, 0), (382, 417, 23, 1), (341, 389, 7, 0), (417, 456, 25, 1),
         (387, 417, 8, 0), (342, 450, 23, 0), (351, 457, 10, 0), (355, 457, 14, 0)],
    ),
    "corridor read across a tile edge": (
        (331, 465),
        6,
        [(358, 438, 17, 0), (362, 430, 14, 1), (354, 448, 6, 1), (398, 413, 4, 0), (366, 422, 10, 1), (366, 402, 7, 1),
         (384, 427, 23, 1), (366, 431, 17, 0), (388, 400, 23, 0), (363, 380, 22, 1), (412, 400, 9, 1), (343, 410, 17, 0),
         (389, 396, 22, 0), (346, 457, 10, 0), (404, 417, 25, 0), (407, 423, 20, 0), (386, 441, 15, 0), (352, 418, 23, 1),
         (370, 435, 19, 0), (414, 412, 7, 1), (414, 421, 8, 1), (375, 395, 25, 1), (416, 408, 12, 0), (367, 452, 14, 1),
         (341, 394, 18, 1), (407, 395, 22, 1), (389, 393, 15, 0), (359, 459, 24, 0), (409, 384, 25, 1), (346, 440, 4, 0),
         (371, 400, 6, 1), (367, 391, 6, 1), (391, 384, 20, 1), (397, 423, 25, 1), (364, 457, 21, 0), (393, 398, 9, 0),
         (349, 456, 11, 0), (391, 413, 25, 1), (369, 386, 15, 0), (340, 414, 12, 1)],
    ),
}


def walls_of(segments) -> set:
    out = set()
    for x, y, length, horizontal in segments:
        out |= {(x + k, y) if horizontal else (x, y + k) for k in range(length)}
    return out - {TOWN}


def travel_to_town(start, perception: int, walls: set, decisions: int = 150):
    """Travel to town on a fogged field, revealing the perception window as it
    walks and sending each walk queue as the runner does. The oscillation
    events and where it ended."""
    w = WorldModel(character_id=1, map_id=MAP, pos=start, perception=perception)
    kb = KnowledgeBase.empty("sandbox")
    sync_town(kb, {"map_id": MAP, "x": TOWN[0], "y": TOWN[1]})
    m = Memory()
    plan = Plan(parse_directives_goals(["travel:town"]), dict(PARAM_DEFAULTS))
    c = PlayContext(m, Policy(kind="scripted"), random.Random(0), plan=plan, knowledge=kb)
    events = []
    for _ in range(decisions):
        x0, y0 = w.pos
        for x in range(x0 - perception, x0 + perception + 1):
            for y in range(y0 - perception, y0 + perception + 1):
                w.view.tiles[(x, y)] = "wall" if (x, y) in walls else "dirt"
        w.terrain_center, w.terrain_map = w.pos, MAP
        out = dispatch(w, c)
        events += m.nav_stuck.oscillations
        m.nav_stuck.oscillations = []
        if plan.current() is None or out.state != "Travel":
            break
        if not out.intents:
            w.tick += 10
            continue
        step = (out.intents[0]["x"], out.intents[0]["y"])
        prefix = walkable_prefix(w, m, c.policy, m.path)
        on_path = prefix[:1] == [step]
        cells = prefix[:10] if on_path else [step]
        if on_path:
            m.path = m.path[len(cells) :]
        for cell in cells:
            w.pos, w.tick = cell, w.tick + 4
    return events, w.pos


class TravelPacingTest(unittest.TestCase):
    def test_travel_reaches_town_without_pacing(self):
        for name, (start, perception, segments) in PACING_MAPS.items():
            with self.subTest(name):
                events, end = travel_to_town(start, perception, walls_of(segments))
                self.assertEqual(events, [], "no pacing, so oscillation never gives the town up")
                self.assertEqual(end, TOWN)

    def test_a_cell_is_ranked_the_same_from_either_side_of_a_tile_edge(self):
        """The cell search's estimate reads the corridor search's tree, not the tile we stand in."""
        goal = (60, 20)
        nav = NavSearchState(goal=goal, map_id=MAP)
        # A tree grown back from the goal's tile, (3, 1): (1, 1) → (2, 1) → (3, 1); (1, 0) → (2, 1).
        nav.came = {(2, 1): (3, 1), (1, 1): (2, 1), (1, 0): (2, 1), (2, 0): (3, 1)}
        from_below = _toward(goal, [(1, 1), (2, 1), (3, 1)], nav.came)
        from_above = _toward(goal, [(1, 0), (2, 1), (3, 1)], nav.came)
        for p in [(31, 15), (31, 16), (30, 16), (33, 17), (40, 16), (40, 15)]:
            self.assertEqual(from_below(p), from_above(p), p)
        self.assertEqual(macro_cell(goal), (3, 1))
        self.assertEqual(from_below((50, 20)), 10, "in the goal's tile: straight to the goal")


class CorridorWithNoStepTest(unittest.TestCase):
    """A corridor whose window search finds no cell better than where we stand is no path (A13, A15).

    The goal lies far east; the tile east of us is wall, so the corridor
    turns south-east, and a wall row just south shuts that way inside the
    window. A straight line east still offers a step: the old fallback took
    it, and the next decision's corridor took it back.
    """

    GOAL = (100, 8)

    def pocket(self) -> WorldModel:
        w = WorldModel(character_id=1, map_id=MAP, pos=(8, 8), perception=3)
        for x in range(32):
            for y in range(32):
                wall = (16 <= x and y < 16) or (y == 9 and x < 16) or (x == 7 and y < 16)
                w.view.tiles[(x, y)] = "wall" if wall else "dirt"
        return w

    def test_cost_path_says_no_path_where_a_straight_line_would_step(self):
        w = self.pocket()
        grid = planner._Grid(w, {self.GOAL}, CostGridParams())
        self.assertTrue(planner._fine_path(grid, planner._toward(self.GOAL, None), None, 400), "a straight line steps")
        self.assertIsNone(cost_path(w, self.GOAL, CostGridParams(), nav=NavSearchState(goal=self.GOAL, map_id=MAP)))

    def test_stuck_detection_escalates_it(self):
        w, m = self.pocket(), Memory()

        def plan(att):
            params = nav_stuck.planning_params(m, CostGridParams(allow_goal_door=True))
            return cost_path(w, self.GOAL, params, nav=nav_search(m, w, "travel:point", self.GOAL))

        guided_step(m, w, "travel:point", self.GOAL, set(), plan)
        att = nav_stuck.active(m, w)
        self.assertGreater(att.level, nav_stuck.WALK)
        self.assertEqual(att.reasons[0], "no_path")


class GatherRegionKeptTest(unittest.TestCase):
    """A planner reply that moves the ``gather_gems`` region needs a reason (A71)."""

    def runner(self, x: int, y: int):
        r = fake_runner()
        r.plan = Plan([{"op": "gather_gems", "count": 20, "x": x, "y": y}], dict(PARAM_DEFAULTS), tick_hz=10)
        r.knowledge = KnowledgeBase.empty("sandbox")
        return r

    def reply(self, x: int, y: int):
        return {"goals": [{"op": "gather_gems", "count": 20, "x": x, "y": y, "why": "better here"}]}

    def settled(self, *replies):
        """A strategist whose first call (raised by the start map) is already answered."""
        s, r = make(FakeLLM({"goals": [{"op": "gather_gems", "count": 20, "x": 10, "y": 10}]}, *replies)), self.runner(10, 10)
        round_trip(s, r)
        return s, r

    def test_a_reply_that_only_moves_the_region_keeps_it(self):
        s, r = self.settled(self.reply(100, 100))
        before = r.plan
        r.mem.strategist_signals.append({"trigger": "goal_done", "tick": 5})
        round_trip(s, r)
        self.assertIs(r.plan, before, "same head, progress kept")
        self.assertEqual((r.plan.current()["x"], r.plan.current()["y"]), (10, 10))
        self.assertTrue(any("kept its region" in note for note in s.rejected), s.rejected)

    def test_another_cell_of_the_same_region_is_the_same_target(self):
        s, r = self.settled(self.reply(12, 3))
        before = r.plan
        r.mem.strategist_signals.append({"trigger": "goal_done", "tick": 5})
        round_trip(s, r)
        self.assertIs(r.plan, before)
        self.assertEqual(s.rejected, [], "nothing was refused, so the planner is told nothing")

    def test_an_exhausted_region_may_move(self):
        s, r = self.settled(self.reply(100, 100))
        r.knowledge.extra[GEM_YIELD] = {str(r.world.map_id): {"regions": {region_key((10, 10)): {"cuts": 40, "gems": 0}}}}
        r.mem.strategist_signals.append({"trigger": "goal_done", "tick": 5})
        round_trip(s, r)
        self.assertEqual((r.plan.current()["x"], r.plan.current()["y"]), (100, 100))

    def test_a_region_with_no_cut_for_the_stall_window_may_move(self):
        from agentrealm_agent.states.gather import STALL_SECONDS

        s, r = self.settled(self.reply(100, 100))
        r.world.tick = 1000
        r.mem.gather_in_region = ((0, 0), 0, 1000 - STALL_SECONDS * 10, 995)  # stood in it 30 s, no cut
        r.mem.strategist_signals.append({"trigger": "goal_done", "tick": 5})
        round_trip(s, r)
        self.assertEqual(r.plan.current()["x"], 100)

    def test_a_walk_that_gets_no_nearer_may_move_it(self):
        """Blocked on the way: 30 s with no nearer approach is impossible, never reached or not."""
        from agentrealm_agent.states.gather import STALL_SECONDS

        s, r = self.settled(self.reply(100, 100))
        r.world.tick = 1000
        r.mem.gather_in_region = ((0, 0), 12, 1000 - STALL_SECONDS * 10, 995)  # 12 blocks off for 30 s
        r.mem.strategist_signals.append({"trigger": "goal_done", "tick": 5})
        round_trip(s, r)
        self.assertEqual(r.plan.current()["x"], 100)

    def test_a_long_walk_to_the_region_is_not_a_stall(self):
        """The clock starts on arrival: 60 s of walking there, then 10 s in it, keeps the region."""
        s, r = self.settled(self.reply(100, 100))
        r.world.tick = 1000
        r.mem.gather_spell = (400, 995)  # Gather began 60 s ago, walking
        r.mem.gather_in_region = ((0, 0), 0, 900, 995)  # arrived 10 s ago
        r.mem.strategist_signals.append({"trigger": "goal_done", "tick": 5})
        round_trip(s, r)
        self.assertEqual(r.plan.current()["x"], 10)

    def test_a_cut_elsewhere_does_not_reset_the_clock_but_one_there_does(self):
        from agentrealm_agent.states.gather import STALL_SECONDS

        for cut_at, kept in (((200, 200), False), ((5, 5), True)):
            with self.subTest(cut_at=cut_at):
                s, r = self.settled(self.reply(100, 100))
                r.world.tick = 1000
                r.mem.gather_in_region = ((0, 0), 0, 1000 - STALL_SECONDS * 10, 995)
                r.gem_cuts.last_cut_tick, r.gem_cuts.last_cut_pos = 990, cut_at
                r.mem.strategist_signals.append({"trigger": "goal_done", "tick": 5})
                round_trip(s, r)
                self.assertEqual(r.plan.current()["x"], 10 if kept else 100)

    def test_a_poor_region_may_move(self):
        s, r = self.settled(self.reply(100, 100))
        r.knowledge.extra[GEM_YIELD] = {str(r.world.map_id): {"regions": {region_key((10, 10)): {"cuts": 20, "gems": 0}}}}
        r.mem.strategist_signals.append({"trigger": "goal_done", "tick": 5})
        round_trip(s, r)
        self.assertEqual(r.plan.current()["x"], 100)

    def test_a_hostile_pack_seen_may_move_it(self):
        s, r = self.settled(self.reply(100, 100))
        pack = {"kind": "hostile_pack", "count": 3, "types": ["snarl"], "cell": [12, 12]}
        r.mem.strategist_signals.append({"trigger": "discovery", "finds": [pack], "tick": 5})
        r.mem.strategist_signals.append({"trigger": "goal_done", "tick": 5})  # a call now, not after the discovery gap
        round_trip(s, r)
        self.assertEqual(r.plan.current()["x"], 100)

    def test_new_information_may_move_it(self):
        for trigger in ("death", "map", "hurt"):
            with self.subTest(trigger):
                s, r = self.settled(self.reply(100, 100))
                r.mem.strategist_signals.append({"trigger": trigger, "tick": 5})
                round_trip(s, r)
                self.assertEqual(r.plan.current()["x"], 100)


class GatherArrivalClockTest(unittest.TestCase):
    """Gather writes ``Memory.gather_in_region`` itself: the clock runs from its last nearer step or its arrival."""

    def test_the_clock_runs_from_the_last_approach_or_arrival(self):
        from agentrealm_agent.gem_yield import GemYieldTracker, STALL_SECONDS
        from agentrealm_agent.states import gather_outcome

        w, m = open_field(at=(30, 5), size=48), Memory()
        w.view.tiles[(5, 5)] = "grass"
        op = {"op": "gather_gems", "count": 5, "x": 5, "y": 5}
        policy, cuts = Policy(kind="scripted", goals=[], on_hostile="ignore"), GemYieldTracker()

        def decide(at, tick):
            w.pos, w.tick = at, tick
            gather_outcome(w, m, policy, op=op, gem_cuts=cuts, tick_hz=10)

        decide((30, 5), 100)
        self.assertEqual(m.gather_in_region, ((0, 0), 15, 100, 100), "the walk there starts at 15 blocks off")
        decide((20, 5), 200)
        self.assertEqual(m.gather_in_region, ((0, 0), 5, 200, 200), "nearer: the clock starts over")
        decide((20, 6), 250)
        self.assertEqual(m.gather_in_region, ((0, 0), 5, 200, 250), "no nearer: it runs")
        decide((10, 5), 400)
        self.assertEqual(m.gather_in_region, ((0, 0), 0, 400, 400), "arrival starts it again")
        decide((9, 5), 450)
        self.assertEqual(m.gather_in_region, ((0, 0), 0, 400, 450), "staying in the region keeps it running")
        decide((10, 5), 450 + STALL_SECONDS * 10)
        self.assertEqual(m.gather_in_region[2], 450 + STALL_SECONDS * 10, "after 30 s off the region it starts over")


class RetreatWindowPausedTest(unittest.TestCase):
    """The safe walk's stuck window does not count time a losing Retreat spent drinking or fighting back (A9)."""

    def setUp(self):
        self.w, self.m = open_field(at=(2, 10)), Memory()
        self.goal = (30, 10)
        self.m.path, self.m.goal = [(x, 10) for x in range(3, 31)], "safe"
        self.assertFalse(no_progress(self.w, self.m, self.goal))  # the window opens

    def pass_time(self, ticks: int, paused: bool) -> bool:
        stuck = False
        for _ in range(ticks // 10):
            self.w.tick += 10
            self.m.nav_stuck.decision += 1
            stuck = no_progress(self.w, self.m, self.goal) or stuck
            if paused:
                self.m.retreat_paused = (self.goal, self.w.tick)
        return stuck

    def test_a_losing_retreat_that_drinks_marks_the_pause(self):
        """Through ``retreat_step``: the drink it sends instead of a step pauses its safe walk's window."""
        from agentrealm_agent.item_table import InventorySupply
        from agentrealm_agent.states.retreat import RETREAT_PROBE_TICKS
        from agentrealm_agent.zone_discovery import apply_zone
        from tests.test_survival_runs import MAP as SURVIVAL_MAP, ctx as survival_ctx, hit, world

        w, c = world(health=4), survival_ctx(on_hostile="fight")
        apply_zone(w, SURVIVAL_MAP, -10, 10, {"safe": True})
        w.entities = [Entity("npc", 7, (11, 10), code="chaser")]
        hit(w)
        w.held_supplies = [InventorySupply(5, "small_potion")]
        first = dispatch(w, c)
        self.assertEqual(first.state, "Retreat")
        self.assertIsNone(c.memory.retreat_paused, "walking is not a pause")
        w.tick += RETREAT_PROBE_TICKS
        hit(w)
        out = dispatch(w, c)
        self.assertTrue(out.reason.startswith("retreat losing ground: arm and use"), out.reason)
        self.assertEqual(c.memory.retreat_paused, (c.memory.retreat_to, w.tick))

    def test_drinking_does_not_run_the_window_out(self):
        self.m.retreat_paused = (self.goal, self.w.tick)
        self.assertFalse(self.pass_time(nav_stuck.PROGRESS_TICK_LIMIT + 50, paused=True))

    def test_standing_still_without_drinking_does(self):
        self.assertTrue(self.pass_time(nav_stuck.PROGRESS_TICK_LIMIT + 50, paused=False))


if __name__ == "__main__":
    unittest.main()
