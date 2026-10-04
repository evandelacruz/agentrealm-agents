"""A22: Gather state — grass, bushes, gem piles in safe-ish ground."""

import random
import unittest

from agentrealm_agent.directives import Directives
from agentrealm_agent.states import PlayContext, dispatch, gather_outcome
from agentrealm_agent.states.gather_safe import is_safe_ish
from agentrealm_agent.config import Policy
from agentrealm_agent.memory import Memory
from agentrealm_agent.world import Entity, WorldModel
from agentrealm_agent.zone_discovery import apply_zone


def grid(rows: list[str], at=(1, 1)) -> WorldModel:
    glyph = {".": "dirt", "g": "grass", "b": "bush", "#": "wall"}
    w = WorldModel(character_id=1, map_id=7, pos=at, perception=5)
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            w.view.tiles[(x, y)] = glyph.get(ch, "dirt")
    w.terrain_center, w.terrain_map = at, 7
    w.attack_range = 1
    w.gems = 0
    return w


def ctx(w: WorldModel, goals: list[str], **policy_kw) -> PlayContext:
    return PlayContext(
        Memory(),
        Policy(kind="scripted", pickup=False, on_hostile="ignore", **policy_kw),
        random.Random(0),
        directives=Directives(goals=goals),
    )


class GatherSafeIshTest(unittest.TestCase):
    def test_known_safe_tile_qualifies(self):
        w = grid(["ggg"], at=(1, 0))
        apply_zone(w, 7, 1, 0, {"safe": True, "brightness": 1})
        self.assertTrue(is_safe_ish(w, (1, 0), Policy()))

    def test_hostile_in_range_disqualifies(self):
        w = grid(["ggg"], at=(1, 0))
        apply_zone(w, 7, 1, 0, {"safe": True, "brightness": 1})
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
        apply_zone(w, 7, 1, 0, {"safe": True, "brightness": 1})
        out = gather_outcome(w, Memory(), Policy(on_hostile="ignore"), never_attack=[])
        self.assertEqual(out.intents[0]["verb"], "Use")
        self.assertEqual(out.intents[0]["target"], {"kind": "block", "x": 1, "y": 0})

    def test_takes_gem_pile_in_range(self):
        w = grid(["g.g"], at=(1, 0))
        apply_zone(w, 7, 1, 0, {"safe": True, "brightness": 1})
        w.entities = [Entity("supply", 9, (2, 0), "gem")]
        out = gather_outcome(w, Memory(), Policy(on_hostile="ignore"), never_attack=[])
        self.assertEqual(out.intents[0]["verb"], "Take")

    def test_cuts_adjacent_bush(self):
        w = grid([".b."], at=(0, 0))
        apply_zone(w, 7, 0, 0, {"safe": True, "brightness": 1})
        apply_zone(w, 7, 1, 0, {"safe": True, "brightness": 1})
        out = gather_outcome(w, Memory(), Policy(on_hostile="ignore"), never_attack=[])
        self.assertEqual(out.intents[0]["target"], {"kind": "block", "x": 1, "y": 0})


class GatherDispatchTest(unittest.TestCase):
    def test_gather_beats_explore_when_goal_active(self):
        w = grid(["ggg"], at=(1, 0))
        apply_zone(w, 7, 1, 0, {"safe": True, "brightness": 1})
        out = dispatch(w, ctx(w, ["gather_gems:3"]))
        self.assertEqual(out.state, "Gather")

    def test_gather_yields_when_gem_count_met(self):
        w = grid(["ggg"], at=(1, 0))
        w.gems = 5
        apply_zone(w, 7, 1, 0, {"safe": True, "brightness": 1})
        out = dispatch(w, ctx(w, ["gather_gems:3"]))
        self.assertEqual(out.state, "Explore")

    def test_skips_grass_outside_safe_ish_ground(self):
        w = grid(["ggg"], at=(1, 0))
        out = gather_outcome(w, Memory(), Policy(on_hostile="ignore"), never_attack=[])
        self.assertIsNone(out.intents)


if __name__ == "__main__":
    unittest.main()
