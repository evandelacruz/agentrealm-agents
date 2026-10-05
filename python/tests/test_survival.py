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
from agentrealm_agent.world import Entity, WorldModel
from agentrealm_agent.zone_discovery import apply_zone


def world(rows: list[str], at=(0, 0)) -> WorldModel:
    glyph = {".": "dirt", "#": "wall", "~": "lava"}
    w = WorldModel(character_id=1, map_id=7, pos=at, perception=5)
    for y, row in enumerate(rows):
        for x, g in enumerate(row):
            w.view.tiles[(x, y)] = glyph[g]
    w.terrain_center, w.terrain_map = at, 7
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
        # No open neighbour: the plan prices hazards instead of blocking them.
        w = world(["~~~.", "~~~.", "~~~."], at=(1, 1))
        out = dispatch(w, ctx(goals=["goto"], goto=(3, 1)))
        self.assertEqual(out.state, "Escape")
        self.assertEqual(step(out)[0], 2)

    def test_not_on_safe_ground(self):
        w = world(["...", "...", "..."], at=(1, 1))
        self.assertEqual(dispatch(w, ctx()).state, "Explore")


class FleeTest(unittest.TestCase):
    def test_flee_steps_away(self):
        w = world(["...", "...", "..."], at=(1, 1))
        w.entities = [Entity("npc", 5, (2, 1))]
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
            w.entities = [Entity("npc", 1, moves[t % 2][0]), Entity("npc", 2, moves[t % 2][1])]
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
        w.entities = [Entity("npc", 5, (0, 2))]
        m = Memory()
        c = ctx(m=m, hostile_range=3)
        self.assertEqual(step(dispatch(w, c)), back, "flee_step's best step")
        m.flee_path, m.state = [], "Flee"
        m.nav_stuck.cells_map = w.map_id
        m.nav_stuck.recent_cells = [back, here, back, here, back]
        m.nav_stuck.recent_moves = [("", "Flee")] * 5
        m.nav_stuck.last_move = ("", "Flee")
        out = dispatch(w, c)
        self.assertEqual(out.state, "Flee")
        self.assertEqual(step(out), (3, 2))
        self.assertTrue(m.nav_stuck.oscillations[-1]["escape"])

    def test_never_steps_next_to_a_hostile_to_avoid_the_cell_behind(self):
        """Only the cell behind is open away from the hostile: Flee takes it."""
        w = world(["#####", ".....", "#####"], at=(2, 1))
        w.entities = [Entity("npc", 5, (0, 1))]
        out = dispatch(w, ctx(hostile_range=3))
        self.assertEqual(step(out), (3, 1))

    def test_ties_break_toward_a_known_safe_tile(self):
        w = world([".....", ".....", "....."], at=(2, 1))
        w.entities = [Entity("npc", 5, (2, 0))]
        safe_at(w, (4, 2))
        out = dispatch(w, ctx())
        self.assertEqual(out.state, "Flee")
        self.assertEqual(step(out), (3, 2))

    def test_stays_on_a_safe_tile(self):
        w = world(["...", "...", "..."], at=(1, 1))
        w.entities = [Entity("npc", 5, (2, 1))]
        safe_at(w, (1, 1))
        self.assertEqual(dispatch(w, ctx()).state, "Explore")

    def test_nowhere_to_flee_sends_nothing(self):
        w = world(["###", "#.#", "###"], at=(1, 1))
        w.entities = [Entity("npc", 5, (2, 1))]
        out = dispatch(w, ctx())
        self.assertEqual(out.state, "Flee")
        self.assertIsNone(out.intents)

    def test_out_of_range_is_not_fled(self):
        w = world([".....", ".....", "....."], at=(0, 1))
        w.entities = [Entity("npc", 5, (4, 1))]
        # An NPC in speech range but out of hostile range is greeted (A30).
        self.assertEqual(dispatch(w, ctx(hostile_range=2)).state, "Investigate")


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
        self.w.entities = [Entity("npc", 5, (2, 1))]
        out = dispatch(self.w, ctx(on_hostile="fight"))
        self.assertEqual(out.state, "Flee")

    def test_fight_flees_a_never_attack_target(self):
        self.w.entities = [Entity("character", 5, (2, 1))]
        out = dispatch(self.w, ctx(never_attack=["character"], on_hostile="fight", hostile=["character"]))
        self.assertEqual(out.state, "Flee")

    def test_ignore_neither_flees_nor_fights(self):
        self.w.entities = [Entity("npc", 5, (2, 1)), Entity("character", 6, (0, 1))]
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


if __name__ == "__main__":
    unittest.main()
