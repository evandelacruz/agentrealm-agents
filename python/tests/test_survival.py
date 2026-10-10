"""A9: survival params, retreat threshold, and the Escape, Retreat and Flee states."""

import random
import unittest
from unittest import mock

from agentrealm_agent import survival

from agentrealm_agent.brain import decide
from agentrealm_agent.config import Policy
from agentrealm_agent.directives import PARAM_DEFAULTS
from agentrealm_agent.memory import Memory
from agentrealm_agent.states import PlayContext, dispatch
from agentrealm_agent.survival import (
    effective_fight_margin,
    effective_retreat_hits,
    effective_risk,
    should_retreat,
    would_lose,
)
from agentrealm_agent.pathing import FLEE_RUN_STEPS, flee_run, outruns
from agentrealm_agent.world import Entity, WorldModel, chebyshev
from agentrealm_agent.zone_discovery import apply_zone


def world(rows: list[str], at=(0, 0)) -> WorldModel:
    glyph = {".": "dirt", "#": "wall", "~": "lava"}
    w = WorldModel(character_id=1, map_id=7, pos=at, perception=5)
    for y, row in enumerate(rows):
        for x, g in enumerate(row):
            w.view.tiles[(x, y)] = glyph[g]
    w.terrain_center, w.terrain_map = at, 7
    # The monster types these tests use have shown themselves hostile
    # (``survival.is_hostile``): an NPC of an unproven type is not a threat.
    w.hostile_types |= {("npc", "gnawer"), ("npc", "snotling")}
    return w


def ctx(params=None, m: Memory | None = None, never_attack=None, **policy_kw) -> PlayContext:
    c = PlayContext(m or Memory(), Policy(kind="scripted", **policy_kw), random.Random(0),
                    never_attack=never_attack or [])
    if params is not None:
        c.params = {**PARAM_DEFAULTS, **params}
    return c


def safe_at(w: WorldModel, pos, safe=True) -> None:
    apply_zone(w, w.map_id, pos[0], pos[1], {"safe": safe})


def step(out) -> tuple[int, int]:
    return out.intents[0]["x"], out.intents[0]["y"]


class SurvivalParamsTest(unittest.TestCase):
    def test_effective_risk_at_floor(self):
        self.assertEqual(effective_risk(0.5, 3, 3), 0.0)

    def test_effective_risk_scales_with_headroom(self):
        self.assertAlmostEqual(effective_risk(0.6, 6, 3), 0.6)
        self.assertAlmostEqual(effective_risk(0.6, 4, 3), 0.2)

    def test_effective_retreat_hits(self):
        self.assertEqual(effective_retreat_hits(2, 0.5), 2)
        self.assertEqual(effective_retreat_hits(2, 0.0), 3)
        self.assertEqual(effective_retreat_hits(2, 1.0), 1)
        self.assertEqual(effective_retreat_hits(1, 1.0), 1, "never below 1")

    def test_effective_retreat_hits_rounds_halves_up(self):
        # round(1 − 2 × 0.25) = round(0.5) is 1, not Python's banker's 0.
        self.assertEqual(effective_retreat_hits(2, 0.25), 3)
        self.assertEqual(effective_retreat_hits(2, 0.75), 2)

    def test_effective_fight_margin(self):
        self.assertAlmostEqual(effective_fight_margin(1.5, 0.5), 1.5)
        self.assertAlmostEqual(effective_fight_margin(2.0, 0.0), 3.0)


class RetreatTest(unittest.TestCase):
    def hurt(self, health=3, damage=5):
        w = world([".....", "....."], at=(1, 0))
        safe_at(w, (4, 0))
        w.health, w.lives = health, 6
        w.entities = [Entity("npc", 1, (0, 0), code="gnawer")]
        w.threat.record(("npc", "gnawer"), damage)
        return w

    def test_paths_to_the_nearest_safe_tile(self):
        w = self.hurt()
        out = dispatch(w, ctx(hostile=["npc"]))
        self.assertEqual(out.state, "Retreat")
        self.assertEqual(step(out), (2, 0))

    def test_routes_around_cells_the_oscillation_guard_caught_it_pacing_on(self):
        """A15: Retreat-only pacing makes the next Retreat replan around the other cell."""
        w, m = self.hurt(), Memory()
        m.state, m.path, m.goal = "Retreat", [(2, 0), (3, 0), (4, 0)], "safe"
        m.nav_stuck.cells_map = w.map_id
        m.nav_stuck.recent_cells = [(2, 0), (1, 0), (2, 0), (1, 0), (2, 0)]
        m.nav_stuck.recent_moves = [("safe", "Retreat")] * 5
        m.nav_stuck.last_move = ("safe", "Retreat")
        out = dispatch(w, ctx(m=m, hostile=["npc"]))
        self.assertEqual(out.state, "Retreat")
        self.assertEqual(step(out), (2, 1))

    def test_a_kept_path_into_another_hostiles_reach_is_planned_again(self):
        """A63 run 4: a walk to safety goes round a known hostile it is not running from."""
        w, m = world(["." * 9] * 6, at=(1, 0)), Memory()
        safe_at(w, (8, 0))
        w.health, w.lives = 3, 6
        w.threat.record(("npc", "gnawer"), 5)
        w.entities = [Entity("npc", 1, (0, 0), code="gnawer"), Entity("npc", 2, (5, 1), code="snotling")]
        m.state, m.path, m.goal = "Retreat", [(x, 0) for x in range(2, 9)], "safe"
        c = ctx(m=m, hostile=["npc"])
        out = dispatch(w, c)
        self.assertEqual(out.state, "Retreat")
        self.assertEqual(m.path[-1], (8, 0))
        self.assertTrue(all(chebyshev(p, (5, 1)) > 2 for p in m.path), m.path)

    def test_a_reach_it_cannot_go_round_is_not_replanned_every_decision(self):
        """Review on #137: a kept path through an unavoidable reach replanned forever."""
        from agentrealm_agent.states import retreat

        w, m = world(["#" * 11, "#.........#", "#" * 11], at=(2, 1)), Memory()
        safe_at(w, (9, 1))
        w.health, w.lives = 3, 6
        w.threat.record(("npc", "gnawer"), 5)
        w.entities = [Entity("npc", 1, (1, 1), code="gnawer"), Entity("npc", 2, (5, 3), code="snotling")]
        c = ctx(m=m, hostile=["npc"])
        with mock.patch.object(retreat, "cost_path", wraps=retreat.cost_path) as planned:
            self.assertEqual(dispatch(w, c).state, "Retreat")
            w.entities[1] = Entity("npc", 2, (6, 3), code="snotling")  # moves, still in reach (not of the safe tile)
            m.held_queue = None
            self.assertEqual(dispatch(w, c).state, "Retreat")
            self.assertEqual(planned.call_count, 1)

    def test_a_losing_retreat_walk_is_planned_without_every_hostile(self):
        """Review on #137: the runner leaves every hostile a losing Retreat
        ignored out of its queue's threats (``Memory.walk_skip``)."""
        from agentrealm_agent.states import retreat

        w, m = world(["." * 9] * 6, at=(1, 0)), Memory()
        safe_at(w, (8, 0))
        w.health, w.lives = 3, 6
        w.threat.record(("npc", "gnawer"), 5)
        w.entities = [Entity("npc", 1, (0, 0), code="gnawer"), Entity("npc", 2, (5, 4), code="snotling")]
        c = ctx(m=m, hostile=["npc"])
        with mock.patch.object(retreat, "losing_ground", return_value=True):
            out = retreat.retreat_step(w, c, "Retreat")
        self.assertIn("losing ground", out.reason)
        self.assertEqual(m.walk_skip, {("npc", 1), ("npc", 2)})
        m.walk_skip = set()
        out = retreat.retreat_step(w, c, "Retreat")
        self.assertEqual(m.walk_skip, {("npc", 1)}, "not losing: only the pursuers")

    def test_no_retreat_step_leaves_walk_skip_alone(self):
        """Review on #137: with the safe tile unreachable, dispatch falls through
        to another state's walk, which must not inherit Retreat's skip set."""
        from agentrealm_agent.states import retreat

        w, m = world(["..#.."], at=(1, 0)), Memory()
        safe_at(w, (4, 0))
        w.health, w.lives = 3, 6
        w.threat.record(("npc", "gnawer"), 5)
        w.entities = [Entity("npc", 1, (0, 0), code="gnawer")]
        with mock.patch.object(retreat, "losing_ground", return_value=True):
            out = retreat.retreat_step(w, ctx(m=m, hostile=["npc"]), "Retreat")
        self.assertIsNone(out.intents)
        self.assertEqual(m.walk_skip, set())

    def test_threshold_is_retreat_hits_times_the_hit(self):
        # risk 0.5 with lives well above the floor: retreat_hits applies as set.
        params = {"retreat_hits": 2, "risk": 0.5, "lives_floor": 3}
        self.assertTrue(should_retreat(self.hurt(health=10, damage=5), Policy(hostile=["npc"]), {**PARAM_DEFAULTS, **params}))
        self.assertFalse(should_retreat(self.hurt(health=11, damage=5), Policy(hostile=["npc"]), {**PARAM_DEFAULTS, **params}))

    def test_params_move_the_threshold(self):
        w = self.hurt(health=12, damage=5)
        self.assertNotEqual(dispatch(w, ctx(hostile=["npc"])).state, "Retreat")
        out = dispatch(w, ctx(params={"retreat_hits": 3}, hostile=["npc"]))
        self.assertEqual(out.state, "Retreat")

    def test_not_without_a_hostile_in_range(self):
        w = self.hurt(health=1)
        w.entities = []
        self.assertFalse(should_retreat(w, Policy(hostile=["npc"]), dict(PARAM_DEFAULTS)))
        self.assertEqual(dispatch(w, ctx(hostile=["npc"])).state, "Explore")

    def test_not_without_a_known_safe_tile(self):
        w = self.hurt(health=1)
        w.zones.clear()
        self.assertEqual(dispatch(w, ctx(hostile=["npc"])).state, "Flee")

    def test_not_on_a_safe_tile(self):
        w = self.hurt(health=1)
        safe_at(w, (1, 0))
        self.assertNotIn(dispatch(w, ctx(hostile=["npc"])).state, ("Retreat", "Flee"))

    def test_ignore_never_retreats(self):
        w = self.hurt(health=1)
        self.assertEqual(dispatch(w, ctx(hostile=["npc"], on_hostile="ignore")).state, "Explore")

    def test_survival_outranks_a_pinned_travel_op(self):
        """A16 Walk run 3: a pinned walk never takes the move from Retreat or Heal."""
        from agentrealm_agent.plan import Plan

        w = self.hurt()
        w.max_health = 10
        c = ctx(hostile=["npc"])
        c.plan = Plan([{"op": "travel", "to": "point", "x": 4, "y": 1}], dict(PARAM_DEFAULTS), directive_end=1)
        self.assertEqual(dispatch(w, c).state, "Retreat")
        w.entities = []  # out of combat, still hurt
        self.assertEqual(dispatch(w, c).state, "Heal")

    def test_done_once_on_the_safe_tile(self):
        w = self.hurt()
        c = ctx(hostile=["npc"])
        self.assertEqual(dispatch(w, c).state, "Retreat")
        w.pos = (4, 0)
        self.assertNotEqual(dispatch(w, c).state, "Retreat")


class EscapeTest(unittest.TestCase):
    def test_steps_off_a_hazard(self):
        w = world(["~..", "...", "..."], at=(0, 0))
        out = dispatch(w, ctx())
        self.assertEqual(out.state, "Escape")
        self.assertTrue(out.reflex)
        self.assertNotEqual(step(out), (0, 0))

    def test_beats_flee(self):
        w = world(["~..", "...", "..."], at=(0, 0))
        w.entities = [Entity("npc", 1, (2, 0), code="gnawer")]
        self.assertEqual(dispatch(w, ctx()).state, "Escape")

    def test_surrounded_crosses_hazard_toward_ground(self):
        # No open neighbour: the safe default's path prices hazards instead of
        # blocking them. The only frontier is the dirt column: the rest of
        # the edge is known void.
        w = world(["~~~.", "~~~.", "~~~."], at=(1, 1))
        for x in range(-1, 4):
            for y in range(-1, 4):
                w.view.tiles.setdefault((x, y), "")
        self.assertEqual(w.view.frontier(), {(3, 0), (3, 1), (3, 2)})
        out = dispatch(w, ctx())
        self.assertEqual(out.state, "Escape")
        self.assertEqual(step(out)[0], 2)

    def test_not_on_safe_ground(self):
        w = world(["...", "...", "..."], at=(1, 1))
        self.assertEqual(dispatch(w, ctx()).state, "Explore")


class FleeTest(unittest.TestCase):
    def test_flee_steps_away(self):
        w = world(["...", "...", "..."], at=(1, 1))
        w.entities = [Entity("npc", 5, (2, 1), "gnawer")]
        out = dispatch(w, ctx())
        self.assertEqual(out.state, "Flee")
        self.assertEqual(step(out)[0], 0)

    def test_flees_a_measured_weak_hostile_too(self):
        # on_hostile = "flee" is not gated on a win estimate.
        w = world(["...", "...", "..."], at=(1, 1))
        w.health, w.lives = 500, 10
        w.entities = [Entity("npc", 5, (2, 1), code="snotling")]
        w.threat.record(("npc", "snotling"), 1)
        self.assertEqual(dispatch(w, ctx()).state, "Flee")

    def test_two_moving_hostiles_do_not_pin_flee_between_two_cells(self):
        """A58 runs 4 and 6: Flee paced between two diagonal cells for minutes.

        Two NPCs each step back and forth between two cells. Re-picking the
        greedy best step every decision answers each move by stepping back to
        the other cell, forever; the committed escape keeps going until it is
        out of their range.
        """
        w = world(["." * 12] * 14, at=(6, 6))
        c = ctx(hostile_range=3)
        moves = [((3, 6), (8, 7)), ((3, 7), (9, 6))]
        cells = []
        for t in range(12):
            w.tick += 1
            w.entities = [Entity("npc", 1, moves[t % 2][0], "gnawer"), Entity("npc", 2, moves[t % 2][1], "gnawer")]
            out = dispatch(w, c)
            if out.state != "Flee":
                break
            w.pos = w.terrain_center = step(out)
            cells.append(w.pos)
        self.assertEqual(cells[:2], [(7, 5), (6, 6)], "the first steps are flee_step's")
        self.assertGreater(len(set(cells)), 2, cells)
        self.assertNotEqual(out.state, "Flee", f"still fleeing after {cells}")

    def test_the_oscillation_guard_forces_an_escape_off_the_paced_cells(self):
        """A15: Flee-only pacing makes the next Flee keep off the other cell."""
        here, back = (2, 2), (3, 3)
        w = world([".....", ".....", ".....", ".....", "....."], at=here)
        w.entities = [Entity("npc", 5, (0, 2), "gnawer")]
        m = Memory()
        c = ctx(m=m, hostile_range=3)
        self.assertEqual(step(dispatch(w, c)), back, "flee_step's best step")
        m.flee_path, m.state = [back], "Flee"  # the escape that was pacing
        m.nav_stuck.cells_map = w.map_id
        m.nav_stuck.recent_cells = [back, here, back, here, back]
        m.nav_stuck.recent_moves = [("", "Flee")] * 5
        m.nav_stuck.last_move = ("", "Flee")
        out = dispatch(w, c)
        self.assertEqual(out.state, "Flee")
        self.assertEqual(step(out), (3, 2))
        self.assertTrue(m.nav_stuck.oscillations[-1]["escape"])
        self.assertEqual(m.flee_avoid, {back}, "kept off while this escape runs")
        self.assertNotIn(back, m.flee_path)

    def test_never_steps_next_to_a_hostile_to_avoid_the_cell_behind(self):
        """Only the cell behind is open away from the hostile: Flee takes it."""
        w = world(["#####", ".....", "#####"], at=(2, 1))
        w.entities = [Entity("npc", 5, (0, 1), "gnawer")]
        out = dispatch(w, ctx(hostile_range=3))
        self.assertEqual(step(out), (3, 1))

    def test_escape_routes_on_to_a_known_safe_tile(self):
        w = world([".........", ".........", "........."], at=(2, 1))
        w.entities = [Entity("npc", 5, (0, 1), "gnawer")]
        safe_at(w, (7, 1))
        m = Memory()
        c = ctx(m=m, hostile_range=7)
        out = dispatch(w, c)
        self.assertEqual(out.state, "Flee")
        self.assertEqual(m.flee_path[0], step(out))
        self.assertEqual(m.flee_path[-1], (7, 1), m.flee_path)
        for _ in range(8):
            if out.state != "Flee":
                break
            w.pos = w.terrain_center = step(out)
            out = dispatch(w, c)
        self.assertEqual(w.pos, (7, 1), "walked the route onto the safe tile")
        self.assertNotEqual(out.state, "Flee", "and stays there")

    def test_a_safe_tile_one_step_away_is_the_whole_escape(self):
        w = world([".......", ".......", ".......", "......."], at=(2, 1))
        w.entities = [Entity("npc", 5, (2, 0), "gnawer")]
        safe_at(w, (3, 2))
        m = Memory()
        out = dispatch(w, ctx(m=m))
        self.assertEqual(step(out), (3, 2))
        self.assertEqual(m.flee_path, [(3, 2)])

    def test_ties_break_toward_the_safe_tile_even_against_the_cell_order(self):
        w = world([".....", ".....", "....."], at=(2, 1))
        w.entities = [Entity("npc", 5, (2, 0), "gnawer")]
        safe_at(w, (0, 2))
        self.assertEqual(step(dispatch(w, ctx())), (1, 2))

    def test_a_forced_escape_routes_to_safety_around_the_paced_cell(self):
        here, back = (2, 1), (1, 2)
        w = world(["#######", "#.....#", "#.....#", "#.....#", "#######"], at=here)
        w.entities = [Entity("npc", 1, (5, 1), "gnawer")]
        safe_at(w, (1, 3))
        m = Memory()
        m.state = "Flee"
        m.nav_stuck.cells_map = w.map_id
        m.nav_stuck.recent_cells = [back, here, back, here, back]
        m.nav_stuck.recent_moves = [("", "Flee")] * 5
        m.nav_stuck.last_move = ("", "Flee")
        out = dispatch(w, ctx(m=m, hostile_range=3))
        self.assertEqual(out.state, "Flee")
        self.assertEqual(m.flee_path[-1], (1, 3), m.flee_path)
        self.assertNotIn(back, m.flee_path)

    def test_a_restart_after_another_state_plans_a_fresh_escape(self):
        """Flee's old escape is dropped once another state took a decision."""
        w = world(["........."] * 9, at=(4, 4))
        w.entities = [Entity("npc", 1, (4, 1), "gnawer")]
        m = Memory()
        c = ctx(m=m, hostile_range=3)
        w.pos = w.terrain_center = step(dispatch(w, c))
        self.assertEqual((w.pos, m.flee_path[1]), ((5, 5), (4, 5)))
        m.state = "Investigate"  # another state had the last decision; the agent stayed put
        w.entities = [Entity("npc", 2, (6, 4), "gnawer")]
        self.assertEqual(step(dispatch(w, c)), (6, 6))

    def test_a_shut_committed_cell_is_replanned(self):
        w = world([".....", ".....", "....."], at=(2, 1))
        w.entities = [Entity("npc", 5, (0, 1), "gnawer")]
        w.view.tiles[(3, 1)] = "wall"
        m = Memory()
        m.state, m.flee_path = "Flee", [(3, 1), (4, 1)]
        out = dispatch(w, ctx(m=m, hostile_range=3))
        self.assertNotEqual(step(out), (3, 1))

    def test_a_committed_cell_worse_than_standing_still_is_dropped(self):
        w = world(["......."], at=(3, 0))
        w.entities = [Entity("npc", 1, (0, 0), "gnawer"), Entity("npc", 2, (6, 0), "gnawer")]
        m = Memory()
        m.state, m.flee_path = "Flee", [(4, 0), (5, 0)]
        out = dispatch(w, ctx(m=m, hostile_range=3))
        self.assertEqual(out.state, "Flee")
        self.assertIsNone(out.intents, "both ways are nearer a hostile; it stands still")

    def test_a_dead_end_corridor_waits_instead_of_stepping_closer(self):
        w = world(["###", "#.#", "#.#", "#.#", "#.#"], at=(1, 1))
        w.entities = [Entity("npc", 5, (1, 4), "gnawer")]
        out = dispatch(w, ctx(hostile_range=3))
        self.assertEqual(out.state, "Flee")
        self.assertIsNone(out.intents)

    def test_a_safe_tile_behind_a_hostile_is_not_fled_to(self):
        # The safe tile is west, past the NPC: the escape runs east instead.
        w = world([".........", ".........", "........."], at=(4, 1))
        w.entities = [Entity("npc", 5, (2, 1), "gnawer")]
        safe_at(w, (0, 1))
        m = Memory()
        out = dispatch(w, ctx(m=m, hostile_range=3))
        self.assertEqual(out.state, "Flee")
        self.assertNotIn((0, 1), m.flee_path)
        self.assertTrue(all(x > 4 for x, _ in m.flee_path), m.flee_path)

    def test_a_forced_escape_keeps_off_the_paced_cell_while_it_runs(self):
        """The decision after the guard forced an escape off ``back``: the
        hostiles have moved so ``back`` is now the best single step, and the
        escape still takes its own next cell instead of pacing back."""
        here, back, nxt = (3, 2), (3, 3), (4, 3)
        w = world([".......", "..###..", "..#.#..", "..#..#.", "..###..", ".......", "......."], at=here)
        w.entities = [Entity("npc", 1, (0, -1), "gnawer"), Entity("npc", 2, (7, 0), "gnawer")]
        m = Memory()
        m.state, m.flee_path, m.flee_avoid = "Flee", [nxt, (5, 4)], {back}
        out = dispatch(w, ctx(m=m, hostile_range=4))
        self.assertEqual(out.state, "Flee")
        self.assertEqual(step(out), nxt)

    def test_ties_break_toward_a_known_safe_tile(self):
        w = world([".....", ".....", "....."], at=(2, 1))
        w.entities = [Entity("npc", 5, (2, 0), "gnawer")]
        safe_at(w, (4, 2))
        out = dispatch(w, ctx())
        self.assertEqual(out.state, "Flee")
        self.assertEqual(step(out), (3, 2))

    def test_stays_on_a_safe_tile(self):
        w = world(["...", "...", "..."], at=(1, 1))
        w.entities = [Entity("npc", 5, (2, 1), "gnawer")]
        safe_at(w, (1, 1))
        self.assertEqual(dispatch(w, ctx()).state, "Explore")

    def test_nowhere_to_flee_sends_nothing(self):
        w = world(["###", "#.#", "###"], at=(1, 1))
        w.entities = [Entity("npc", 5, (2, 1), "gnawer")]
        out = dispatch(w, ctx())
        self.assertEqual(out.state, "Flee")
        self.assertIsNone(out.intents)

    def test_out_of_range_is_not_fled(self):
        from agentrealm_agent.plan import Plan

        w = world([".....", ".....", "....."], at=(0, 1))
        w.entities = [Entity("npc", 5, (4, 1), "gnawer")]
        self.assertEqual(dispatch(w, ctx(hostile_range=2)).state, "Explore")
        # An NPC out of hostile range can be spoken to for a ``say`` op (A30).
        c = ctx(hostile_range=2)
        c.plan = Plan([{"op": "say", "npc_id": 5, "text": "hello"}], dict(PARAM_DEFAULTS))
        self.assertEqual(dispatch(w, c).state, "Investigate")


class FleeRunTest(unittest.TestCase):
    """``pathing.flee_run``: the committed run away from the hostiles when no safe tile is known (A9)."""

    def test_stops_at_flee_run_steps_past_the_first(self):
        w = world(["." * 12], at=(0, 0))
        run = flee_run(w, [Entity("npc", 1, (-2, 0), "gnawer")], set(), (1, 0))
        self.assertEqual(len(run), FLEE_RUN_STEPS + 1)
        self.assertEqual(run, [(x, 0) for x in range(1, FLEE_RUN_STEPS + 2)])

    def test_keeps_off_blocked_cells(self):
        w = world(["." * 12], at=(0, 0))
        run = flee_run(w, [Entity("npc", 1, (-2, 0), "gnawer")], {(4, 0)}, (1, 0))
        self.assertEqual(run, [(1, 0), (2, 0), (3, 0)])

    def test_never_steps_back_onto_the_agents_cell(self):
        # East of the agent is further from the NPC, but only through its own cell.
        w = world(["....."], at=(2, 0))
        run = flee_run(w, [Entity("npc", 1, (-3, 0), "gnawer")], set(), (1, 0))
        self.assertEqual(run, [(1, 0)])

    def test_prefers_the_longer_run_on_ties(self):
        w = world(["......", "......", "......"], at=(0, 1))
        run = flee_run(w, [Entity("npc", 1, (4, 0), "gnawer")], set(), (0, 2))
        self.assertEqual(run, [(0, 2), (1, 1), (0, 0)])

    def test_outruns_counts_steps_from_the_agent(self):
        npc = [Entity("npc", 1, (4, 0), "gnawer")]
        self.assertTrue(outruns([(1, 0), (2, 0)], [Entity("npc", 1, (5, 0), "gnawer")]))
        self.assertFalse(outruns([(1, 0), (2, 0)], npc), "(2, 0) is 2 steps out and 2 from the NPC")
        self.assertTrue(outruns([(3, 0)], npc), "the first step is flee_step's, not checked")

    def test_never_runs_past_a_hostile_that_reaches_the_cell_first(self):
        # The corridor east runs 7 cells but passes beside the NPC at (4, 0);
        # it would reach (3, 1)-(5, 1) no later than the agent does.
        w = world(["#########", ".........", "#.#####.#", "#.......#", "#########"], at=(1, 1))
        hostiles = [Entity("npc", 1, (-3, 1), "gnawer"), Entity("npc", 2, (4, 0), "gnawer")]
        run = flee_run(w, hostiles, set(), (2, 1))
        self.assertNotIn((4, 1), run)
        self.assertTrue(all(w.view.walkable(cell) for cell in run), run)
        for depth, cell in enumerate(run[1:], start=2):
            self.assertGreater(min(chebyshev(cell, h.pos) for h in hostiles), depth, run)


class OnHostileTest(unittest.TestCase):
    """policy.on_hostile as the README documents it."""

    def setUp(self):
        self.w = world(["...", "...", "..."], at=(1, 1))

    def test_fight_swings_when_the_win_estimate_clears(self):
        self.w.entities = [Entity("character", 5, (2, 1), code="peer")]
        self.w.health, self.w.lives = 500, 10
        self.w.threat.record(("character", "peer"), 1)
        d = decide(
            self.w,
            Memory(),
            Policy(kind="scripted", on_hostile="fight", hostile=["character"]),
            random.Random(0),
            params={**PARAM_DEFAULTS, "risk": 1.0, "lives_floor": 1},
        )
        self.assertEqual(d.intent["verb"], "Use")

    def test_fight_flees_an_unmeasured_type_at_low_risk(self):
        self.w.entities = [Entity("character", 5, (2, 1), code="peer")]
        d = decide(self.w, Memory(), Policy(kind="scripted", on_hostile="fight", hostile=["character"]),
                   random.Random(0))
        self.assertEqual(d.intent["verb"], "SetPosition")

    def test_fight_flees_an_npc(self):
        self.w.entities = [Entity("npc", 5, (2, 1), "gnawer")]
        out = dispatch(self.w, ctx(on_hostile="fight"))
        self.assertEqual(out.state, "Flee")

    def test_fight_flees_a_never_attack_target(self):
        self.w.entities = [Entity("character", 5, (2, 1))]
        out = dispatch(self.w, ctx(never_attack=["character"], on_hostile="fight", hostile=["character"]))
        self.assertEqual(out.state, "Flee")

    def test_ignore_neither_flees_nor_fights(self):
        self.w.entities = [Entity("npc", 5, (2, 1), "gnawer"), Entity("character", 6, (0, 1))]
        out = dispatch(self.w, ctx(on_hostile="ignore", hostile=["npc", "character"]))
        self.assertEqual(out.state, "Explore")
        self.assertFalse(out.reflex)


class WinEstimateTest(unittest.TestCase):
    """The estimate A23's Fight will gate on."""

    def test_unmeasured_type_loses_below_half_risk(self):
        w = world(["..."], at=(0, 0))
        w.health, w.lives = 10, 3
        w.entities = [Entity("npc", 5, (1, 0), code="gnawer")]
        self.assertTrue(would_lose(w, Policy(hostile=["npc"]), dict(PARAM_DEFAULTS)))

    def test_lone_weak_measured_type_at_full_health_wins(self):
        w = world(["..."], at=(0, 0))
        w.health, w.lives = 500, 10
        w.entities = [Entity("npc", 5, (1, 0), code="snotling")]
        w.threat.record(("npc", "snotling"), 1)
        self.assertFalse(would_lose(w, Policy(hostile=["npc"]), {**PARAM_DEFAULTS, "risk": 1.0, "lives_floor": 1}))

    def test_unknown_health_is_a_new_characters_not_a_hostiles(self):
        w = world(["..."], at=(0, 0))
        w.lives = 10
        w.entities = [Entity("npc", 5, (1, 0), code="snotling")]
        w.threat.record(("npc", "snotling"), 1)
        params = {**PARAM_DEFAULTS, "risk": 1.0, "lives_floor": 1, "fight_margin": 1.0}
        # Our assumed health must not move with the hostile health assumption.
        for hostile_health in (10, 1000):
            with mock.patch.object(survival, "UNKILLED_HOSTILE_HEALTH", hostile_health):
                self.assertEqual(survival.win_ratio(10, w.entities, w.threat) <= 1.0,
                                 would_lose(w, Policy(hostile=["npc"]), params))



class SwingTest(unittest.TestCase):
    """A81: every character has attack power 2, so a swing at a hostile hits
    on 8 or better (65%) and deals 1 up to 2 plus weapon damage."""

    def test_hit_chance_follows_the_published_roll(self):
        self.assertAlmostEqual(survival.hit_chance(), 0.65)
        self.assertAlmostEqual(survival.hit_chance(attack_power=0), 0.55)
        self.assertAlmostEqual(survival.hit_chance(attack_power=30), 0.95, msg="a 1 always misses")
        self.assertAlmostEqual(survival.hit_chance(defense=30), 0.05, msg="a 20 always hits")

    def test_swing_damage_is_the_hit_chance_times_the_mean_roll(self):
        self.assertAlmostEqual(survival.swing_damage("pocket_knife"), 0.65 * 2.5)  # 1 to 4
        self.assertAlmostEqual(survival.swing_damage("bronze_sword"), 0.65 * 3.5)  # 1 to 6
        self.assertAlmostEqual(survival.swing_damage(None), survival.swing_damage("pocket_knife"))
        self.assertAlmostEqual(survival.swing_damage("small_potion"), survival.swing_damage("pocket_knife"))

    def test_the_knife_beats_one_weak_measured_hostile_at_full_health(self):
        """At attack power 0 and 1 damage a swing, this was a loss at the default margin."""
        w = world(["..."], at=(0, 0))
        w.health, w.lives, w.armed_code = 10, 10, "pocket_knife"
        w.entities = [Entity("npc", 5, (1, 0), code="snotling")]
        w.threat.record(("npc", "snotling"), 1)
        self.assertFalse(would_lose(w, Policy(hostile=["npc"]), dict(PARAM_DEFAULTS)))

    def test_every_weapon_has_a_damage_and_nothing_else_does(self):
        """Review on #172: one list of weapons, so none swings as the knife unseen."""
        from agentrealm_agent.break_memory import WEAPON_DAMAGE, WEAPONS
        self.assertEqual(set(WEAPON_DAMAGE), set(WEAPONS))
        self.assertIs(survival.WEAPON_DAMAGE, WEAPON_DAMAGE)

    def test_a_hostile_swings_on_the_same_roll(self):
        """Its damage number is its attack power: 2 hits 65%, 1 hits 60%. The
        largest hit seen is a floor on it, so a landed hit is priced at it
        (review on #172), never at the mean below it."""
        self.assertAlmostEqual(survival.hostile_swing_damage(2), 0.65 * 2)
        self.assertAlmostEqual(survival.hostile_swing_damage(1), 0.60 * 1.0)

    def test_the_knife_takes_one_weak_hostile_but_not_a_pair(self):
        w = world([".."], at=(0, 0))
        w.health, w.lives, w.armed_code = 10, 10, "pocket_knife"
        w.threat.record(("npc", "snotling"), 1)
        w.entities = [Entity("npc", 5, (1, 0), code="snotling")]
        self.assertFalse(would_lose(w, Policy(hostile=["npc"]), dict(PARAM_DEFAULTS)))
        w.entities.append(Entity("npc", 6, (1, 1), code="snotling"))
        self.assertTrue(would_lose(w, Policy(hostile=["npc"]), dict(PARAM_DEFAULTS)))

    def test_an_unmeasured_type_is_still_refused_by_default(self):
        """Review on #172: the conservative guess for a type never measured is
        not discounted by the hit chance, so the default profile still refuses it."""
        w = world(["..."], at=(0, 0))
        w.health, w.lives, w.armed_code = 10, 10, "pocket_knife"
        w.entities = [Entity("npc", 5, (1, 0), code="gnawer")]
        self.assertTrue(would_lose(w, Policy(hostile=["npc"]), dict(PARAM_DEFAULTS)))

    def test_a_better_weapon_wins_sooner(self):
        w = world(["..."], at=(0, 0))
        w.entities = [Entity("npc", 5, (1, 0), code="snotling")]
        w.threat.record(("npc", "snotling"), 2)
        knife = survival.win_ratio(10, w.entities, w.threat, "pocket_knife")
        self.assertGreater(survival.win_ratio(10, w.entities, w.threat, "bronze_sword"), knife)


class TownsfolkTest(unittest.TestCase):
    """A23 survive-a-fight run 2: Flee and Retreat fired on a rumor teller, an
    apothecary and a salvager. Only a hostile is a threat: a type that has
    attacked or died in view, a boss, the one hitting us, or a character."""

    def town(self, health=1):
        w = world(["....."] * 3, at=(1, 1))
        safe_at(w, (4, 1))
        w.health, w.max_health, w.lives = health, 10, 6
        w.entities = [Entity("npc", 135, (2, 1), "rumor_teller")]
        return w

    def test_a_townsperson_beside_us_is_not_a_threat(self):
        w = self.town()
        policy = Policy(hostile=["npc"], on_hostile="fight")
        self.assertFalse(survival.is_hostile(w, policy, w.entities[0]))
        self.assertEqual(survival.hostiles_in_range(w, policy), [])
        self.assertFalse(should_retreat(w, policy, dict(PARAM_DEFAULTS)))
        self.assertFalse(would_lose(w, policy, dict(PARAM_DEFAULTS)))

    def test_no_flee_retreat_or_fight_on_a_townsperson(self):
        for on_hostile in ("flee", "fight"):
            out = dispatch(self.town(), ctx(on_hostile=on_hostile))
            self.assertNotIn(out.state, ("Flee", "Retreat", "Fight"), on_hostile)

    def test_a_townsperson_never_joins_a_combat_group(self):
        w = self.town()
        w.entities.append(Entity("npc", 9, (3, 1), "gnawer"))  # gnawer is hostile in these tests
        policy = Policy(hostile=["npc"])
        self.assertEqual([e.id for e in survival.combat_group(w, policy)], [9])

    def test_a_type_that_swings_at_us_becomes_hostile(self):
        w = self.town()
        policy = Policy(hostile=["npc"])
        events = [{"tick": w.tick, "kind": "Attacked", "actor_kind": "npc", "actor_id": 135}]
        w.learn_threat(events, [])
        self.assertIn(("npc", "rumor_teller"), w.hostile_types)
        other = Entity("npc", 136, (0, 1), "rumor_teller")
        self.assertTrue(survival.is_hostile(w, policy, other), "the whole type, not only that NPC")

    def test_a_type_that_hits_us_becomes_hostile(self):
        w = self.town()
        events = [{"tick": w.tick, "kind": "Damaged", "amount": 0, "source_kind": "npc", "source_id": 135}]
        w.learn_threat(events, [])
        self.assertIn(("npc", "rumor_teller"), w.hostile_types, "a hit armour absorbed still shows the type")

    def test_a_type_seen_dying_is_hostile(self):
        w = self.town()
        w.learn_threat([{"kind": "NPCDied", "npc_id": 40, "npc_type": "wartlurch", "map_id": 7, "x": 0, "y": 0}], [])
        self.assertTrue(survival.is_hostile(w, Policy(hostile=["npc"]), Entity("npc", 41, (2, 1), "wartlurch")))

    def test_the_npc_hitting_us_is_hostile_whatever_its_type(self):
        w = self.town()
        w.attacker, w.attacked_tick = ("npc", 135), w.tick
        self.assertTrue(survival.is_hostile(w, Policy(hostile=["npc"]), w.entities[0]))

    def test_a_boss_is_hostile(self):
        w = self.town()
        boss = Entity("npc", 50, (2, 1), "cellar_boss", health=40, max_health=40)
        self.assertTrue(survival.is_hostile(w, Policy(hostile=["npc"]), boss))

    def test_a_character_is_hostile_by_kind(self):
        w = self.town()
        peer = Entity("character", 6, (2, 1), "peer")
        self.assertTrue(survival.is_hostile(w, Policy(hostile=["character"]), peer))
        self.assertFalse(survival.is_hostile(w, Policy(hostile=["npc"]), peer))


class RetreatThreatTest(unittest.TestCase):
    """A23 survive-a-fight run 2: Retreat started three times at 10/10 from
    ``would_lose`` alone. A fight we would lose sends us to safety only when
    a hostile is coming for us; one that stands nearby is avoided."""

    def standing(self, at=(3, 0)):
        w = world(["......."], at=(1, 0))
        safe_at(w, (6, 0))
        w.health, w.max_health, w.lives = 10, 10, 6
        w.tick = 100
        w.entities = [Entity("npc", 240, at, "gnawer")]  # unmeasured: would_lose at the default risk
        return w

    def policy(self):
        return Policy(hostile=["npc"], on_hostile="fight")

    def params(self):
        return {**PARAM_DEFAULTS, "fight_margin": 2.0}

    def test_a_hostile_standing_two_cells_off_is_not_retreated_from(self):
        w = self.standing()
        self.assertTrue(would_lose(w, self.policy(), self.params()))
        self.assertFalse(should_retreat(w, self.policy(), self.params()))
        self.assertNotEqual(dispatch(w, ctx(params=self.params(), on_hostile="fight")).state, "Retreat")

    def test_a_hostile_in_reach_is(self):
        w = self.standing(at=(2, 0))
        self.assertTrue(should_retreat(w, self.policy(), self.params()))

    def test_a_hostile_approaching_is(self):
        w = self.standing(at=(4, 0))
        w.apply_entities({"tick": 100, "npcs": [{"id": 240, "x": 4, "y": 0, "npc_type_code": "gnawer"}]})
        w.tick = 104
        w.apply_entities({"tick": 104, "npcs": [{"id": 240, "x": 3, "y": 0, "npc_type_code": "gnawer"}]})
        self.assertTrue(survival.approaching(w, w.entities[0]))
        self.assertTrue(should_retreat(w, self.policy(), self.params()))

    def test_an_old_approach_is_forgotten(self):
        w = self.standing(at=(4, 0))
        w.apply_entities({"tick": 100, "npcs": [{"id": 240, "x": 4, "y": 0, "npc_type_code": "gnawer"}]})
        w.apply_entities({"tick": 101, "npcs": [{"id": 240, "x": 3, "y": 0, "npc_type_code": "gnawer"}]})
        w.tick = 101 + survival.THREAT_MEMORY_TICKS + 1
        self.assertFalse(should_retreat(w, self.policy(), self.params()))

    def test_a_hostile_moving_away_is_not(self):
        w = self.standing(at=(2, 0))
        w.apply_entities({"tick": 100, "npcs": [{"id": 240, "x": 2, "y": 0, "npc_type_code": "gnawer"}]})
        w.apply_entities({"tick": 101, "npcs": [{"id": 240, "x": 3, "y": 0, "npc_type_code": "gnawer"}]})
        self.assertFalse(survival.approaching(w, w.entities[0]))
        self.assertFalse(should_retreat(w, self.policy(), self.params()))

    def test_the_hostile_hitting_us_is(self):
        w = self.standing()
        w.attacker, w.attacked_tick = ("npc", 240), w.tick
        self.assertTrue(should_retreat(w, self.policy(), self.params()))

    def test_the_health_floor_still_retreats_from_a_standing_hostile(self):
        w = self.standing()
        w.health = 1
        self.assertTrue(should_retreat(w, self.policy(), self.params()))

if __name__ == "__main__":
    unittest.main()
