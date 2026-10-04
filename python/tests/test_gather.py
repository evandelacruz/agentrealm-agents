"""A22: Gather state — grass, bushes, gem piles in safe-ish ground."""

import random
import unittest
from unittest import mock

from agentrealm_agent.config import Policy
from agentrealm_agent.directives import Directives
from agentrealm_agent.memory import Memory
from agentrealm_agent.plan_goals import GatherGemsGoal, gather_gems_goal
from agentrealm_agent.states import PlayContext, dispatch, gather_outcome
from agentrealm_agent.states import gather as gather_mod
from agentrealm_agent.states.gather_safe import is_safe_ish
from agentrealm_agent.world import Entity, WorldModel
from agentrealm_agent.zone_discovery import apply_zone


def grid(rows: list[str], at=(1, 1)) -> WorldModel:
    glyph = {".": "dirt", "g": "grass", "b": "bush", "#": "wall", "l": "lava"}
    w = WorldModel(character_id=1, map_id=7, pos=at, perception=5)
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            w.view.tiles[(x, y)] = glyph.get(ch, "dirt")
    w.terrain_center, w.terrain_map = at, 7
    w.attack_range = 1
    w.gems = 0
    return w


def safe(w: WorldModel, *cells) -> None:
    for x, y in cells:
        apply_zone(w, 7, x, y, {"safe": True, "brightness": 1})


def ctx(w: WorldModel, goals: list[str], m: Memory | None = None, **policy_kw) -> PlayContext:
    kw = {"pickup": False, "on_hostile": "ignore", "goals": ["explore"], **policy_kw}
    return PlayContext(m or Memory(), Policy(kind="scripted", **kw), random.Random(0), directives=Directives(goals=goals))


def outcome(w: WorldModel, m: Memory | None = None, **policy_kw):
    kw = {"on_hostile": "ignore", **policy_kw}
    return gather_outcome(w, m or Memory(), Policy(**kw), never_attack=[])


class PlanGoalsTest(unittest.TestCase):
    def test_bare_op_means_one_gem(self):
        self.assertEqual(gather_gems_goal(Directives(goals=["gather_gems"])), GatherGemsGoal(1))

    def test_count_is_parsed(self):
        self.assertEqual(gather_gems_goal(Directives(goals=["gather_gems:20"])), GatherGemsGoal(20))

    def test_bad_and_zero_counts_are_skipped_for_the_next_op(self):
        d = Directives(goals=["gather_gems:x", "gather_gems:0", "gather_gems:-3", "gather_gems:4"])
        self.assertEqual(gather_gems_goal(d), GatherGemsGoal(4))

    def test_other_ops_are_not_gather(self):
        self.assertIsNone(gather_gems_goal(Directives(goals=["explore", "gather_gemsx"])))


class WorldGemsTest(unittest.TestCase):
    def test_snapshot_inventory_sets_gems(self):
        w = WorldModel(character_id=1)
        self.assertIsNone(w.gems)
        w.apply_observation({"complete": True, "version": 1, "snapshot": {"inventory": {"gems": 7}}})
        self.assertEqual(w.gems, 7)

    def test_inventory_without_gems_keeps_the_counter(self):
        w = WorldModel(character_id=1)
        w.gems = 3
        w.apply_observation({"complete": True, "version": 1, "snapshot": {"inventory": {"armed": None}}})
        self.assertEqual(w.gems, 3)


class GatherSafeIshTest(unittest.TestCase):
    def test_known_safe_tile_qualifies(self):
        w = grid(["ggg"], at=(1, 0))
        safe(w, (1, 0))
        self.assertTrue(is_safe_ish(w, (1, 0), Policy()))

    def test_hostile_in_range_disqualifies(self):
        w = grid(["ggg"], at=(1, 0))
        safe(w, (1, 0))
        w.entities = [Entity("npc", 1, (2, 0))]
        pol = Policy(hostile=["npc"], hostile_range=2)
        self.assertFalse(is_safe_ish(w, (1, 0), pol))

    def test_respawn_ring_qualifies_without_zone(self):
        w = grid(["ggg"], at=(2, 0))
        w.record_respawn_anchor(7, (0, 0))
        self.assertTrue(is_safe_ish(w, (2, 0), Policy()))


class GatherActTest(unittest.TestCase):
    def test_cuts_grass_on_safe_tile(self):
        w = grid(["ggg"], at=(1, 0))
        safe(w, (1, 0))
        out = outcome(w)
        self.assertEqual(out.intents[0]["verb"], "Use")
        self.assertEqual(out.intents[0]["target"], {"kind": "block", "x": 1, "y": 0})

    def test_cuts_adjacent_bush(self):
        w = grid([".b."], at=(0, 0))
        safe(w, (0, 0), (1, 0))
        out = outcome(w)
        self.assertEqual(out.intents[0]["target"], {"kind": "block", "x": 1, "y": 0})

    def test_bush_beyond_one_block_is_walked_to_not_cut(self):
        # A longer weapon reach does not stretch block Use past adjacent.
        w = grid(["..b"], at=(0, 0))
        w.attack_range = 3
        safe(w, (0, 0), (1, 0), (2, 0))
        m = Memory()
        out = outcome(w, m)
        self.assertEqual(out.intents[0]["verb"], "SetPosition")
        self.assertEqual(m.gather_target, ("bush", (2, 0)))

    def test_gem_piles_are_not_targeted_while_their_code_is_unknown(self):
        w = grid(["...", "..."], at=(1, 0))
        safe(w, (1, 0), (2, 0))
        w.entities = [Entity("supply", 9, (2, 0), "gem")]
        self.assertIsNone(outcome(w).intents)

    def test_takes_gem_pile_in_range_once_its_code_is_known(self):
        w = grid(["g.g"], at=(1, 0))
        safe(w, (1, 0))
        w.entities = [Entity("supply", 9, (2, 0), "gem_pile")]
        with mock.patch.object(gather_mod, "UNKNOWN_GEM_PILE_CODES", frozenset({"gem_pile"})):
            out = outcome(w)
        self.assertEqual(out.intents[0]["verb"], "Take")

    def test_skips_grass_outside_safe_ish_ground(self):
        w = grid(["ggg"], at=(1, 0))
        self.assertIsNone(outcome(w).intents)


class GatherPathingTest(unittest.TestCase):
    def test_walks_to_safe_grass(self):
        w = grid(["..g"], at=(0, 0))
        safe(w, (2, 0))
        m = Memory()
        out = outcome(w, m)
        self.assertEqual(out.intents[0], {"verb": "SetPosition", "x": 1, "y": 0})
        self.assertEqual((m.goal, m.gather_target), ("gather", ("grass", (2, 0))))

    def test_walks_beside_a_bush_before_grass(self):
        w = grid(["g...b"], at=(2, 0))
        safe(w, (0, 0), (4, 0))
        m = Memory()
        outcome(w, m)
        self.assertEqual(m.gather_target, ("bush", (4, 0)))
        self.assertEqual(m.path[-1], (3, 0))

    def test_walks_to_a_known_pile_first(self):
        w = grid(["g...."], at=(1, 0))
        safe(w, (0, 0), (4, 0))
        w.entities = [Entity("supply", 9, (4, 0), "gem_pile")]
        m = Memory()
        with mock.patch.object(gather_mod, "UNKNOWN_GEM_PILE_CODES", frozenset({"gem_pile"})):
            outcome(w, m)
        self.assertEqual(m.gather_target, ("pile", (4, 0)))
        self.assertEqual(m.path[-1], (4, 0))

    def test_pile_target_path_is_kept_between_ticks(self):
        w = grid(["......"], at=(0, 0))
        safe(w, (5, 0))
        w.entities = [Entity("supply", 9, (5, 0), "gem_pile")]
        m = Memory()
        with mock.patch.object(gather_mod, "UNKNOWN_GEM_PILE_CODES", frozenset({"gem_pile"})):
            outcome(w, m)
            path = list(m.path)
            with mock.patch.object(gather_mod, "_replan_gather") as replan:
                outcome(w, m)
        replan.assert_not_called()
        self.assertEqual(m.path, path)

    def test_target_that_stops_qualifying_is_dropped(self):
        w = grid(["...g"], at=(0, 0))
        safe(w, (3, 0))
        m = Memory()
        outcome(w, m)
        self.assertEqual(m.gather_target, ("grass", (3, 0)))
        w.view.tiles[(3, 0)] = "dirt"  # cut by someone else
        out = outcome(w, m)
        self.assertIsNone(out.intents)
        self.assertEqual((m.path, m.goal, m.gather_target), ([], "", None))

    def test_no_target_keeps_another_states_path(self):
        w = grid(["...."], at=(0, 0))
        m = Memory(path=[(1, 0), (2, 0)], goal="explore")
        self.assertIsNone(outcome(w, m).intents)
        self.assertEqual((m.path, m.goal), ([(1, 0), (2, 0)], "explore"))


class GatherReflexTest(unittest.TestCase):
    def test_flees_a_hostile(self):
        w = grid(["ggggg"], at=(2, 0))
        safe(w, (2, 0))
        w.entities = [Entity("npc", 4, (3, 0))]
        m = Memory(path=[(1, 0)], goal="gather", gather_target=("grass", (0, 0)))
        out = outcome(w, m, on_hostile="flee", hostile=["npc"], hostile_range=2)
        self.assertTrue(out.reflex)
        self.assertTrue(out.reason.startswith("flee"))
        self.assertEqual((m.path, m.goal, m.gather_target), ([], "", None))

    def test_steps_off_a_hazard(self):
        w = grid([".lg"], at=(1, 0))
        safe(w, (2, 0))
        out = outcome(w, avoid_blocks=["lava"])
        self.assertTrue(out.reflex)
        self.assertEqual(out.reason, "off lava")

    def test_fights_a_character_like_explore(self):
        w = grid(["ggg"], at=(1, 0))
        w.entities = [Entity("character", 5, (2, 0))]
        out = outcome(w, on_hostile="fight", hostile=["character"], hostile_range=2)
        self.assertEqual(out.intents[0]["verb"], "Use")
        self.assertEqual(out.intents[0]["target"], {"kind": "character", "character_id": 5})


class GatherDispatchTest(unittest.TestCase):
    def test_gather_beats_explore_when_goal_active(self):
        w = grid(["ggg"], at=(1, 0))
        safe(w, (1, 0))
        out = dispatch(w, ctx(w, ["gather_gems:3"]))
        self.assertEqual(out.state, "Gather")

    def test_gather_yields_when_gem_count_met(self):
        w = grid(["ggg"], at=(1, 0))
        w.gems = 5
        safe(w, (1, 0))
        out = dispatch(w, ctx(w, ["gather_gems:3"]))
        self.assertEqual(out.state, "Explore")

    def test_gather_waits_for_a_gem_count(self):
        w = grid(["ggg"], at=(1, 0))
        w.gems = None
        safe(w, (1, 0))
        out = dispatch(w, ctx(w, ["gather_gems:3"]))
        self.assertEqual(out.state, "Explore")

    def test_no_target_in_sight_yields_to_explore(self):
        w = grid(["....", "....", "...."], at=(1, 1))
        c = ctx(w, ["gather_gems:3"])
        out = dispatch(w, c)
        self.assertEqual(out.state, "Explore")
        self.assertIsNotNone(out.intents, "Explore moves instead of the character standing still")
        self.assertEqual(c.memory.state, "Explore")

    def test_gather_takes_back_over_once_a_target_appears(self):
        w = grid(["....", "....", "...."], at=(1, 1))
        c = ctx(w, ["gather_gems:3"])
        dispatch(w, c)
        w.view.tiles[(1, 1)] = "grass"
        safe(w, (1, 1))
        out = dispatch(w, c)
        self.assertEqual(out.state, "Gather")
        self.assertEqual(out.reason, "cut grass")


if __name__ == "__main__":
    unittest.main()
