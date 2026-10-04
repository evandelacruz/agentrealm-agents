"""A9: survival params, retreat threshold, and survival states."""

import random
import unittest

from agentrealm_agent.brain import decide
from agentrealm_agent.config import Policy
from agentrealm_agent.memory import Memory
from agentrealm_agent.states import dispatch
from agentrealm_agent.survival import (
    effective_fight_margin,
    effective_retreat_hits,
    effective_risk,
)
from agentrealm_agent.world import Entity, WorldModel
from agentrealm_agent.zone_discovery import apply_zone

from test_states import ctx, world


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

    def test_effective_fight_margin(self):
        self.assertAlmostEqual(effective_fight_margin(1.5, 0.5), 1.5)


class RetreatStateTest(unittest.TestCase):
    def _safe_at(self, w: WorldModel, pos, safe=True):
        apply_zone(w, w.map_id, pos[0], pos[1], {"safe": safe})

    def test_retreat_paths_to_nearest_safe_tile(self):
        w = world(["....", "...."], at=(0, 0))
        self._safe_at(w, (3, 0))
        w.health = 3
        w.lives = 3
        w.entities = [Entity("npc", 1, (1, 0), code="gnawer")]
        w.threat.record(("npc", "gnawer"), 5)
        out = dispatch(w, ctx(w, params={"retreat_hits": 2, "risk": 0.5, "lives_floor": 3, "fight_margin": 1.5}))
        self.assertEqual(out.state, "Retreat")
        self.assertEqual(out.intents[0]["verb"], "SetPosition")
        step = (out.intents[0]["x"], out.intents[0]["y"])
        self.assertNotEqual(step, (0, 0))
        self.assertLessEqual(abs(step[0] - 0) + abs(step[1] - 0), 2)

    def test_retreat_not_without_known_safe_tile(self):
        w = world(["..."], at=(0, 0))
        w.health = 1
        w.entities = [Entity("npc", 1, (1, 0), code="gnawer")]
        w.threat.record(("npc", "gnawer"), 10)
        out = dispatch(w, ctx(w))
        self.assertEqual(out.state, "Flee")


class EscapeAndFleeTest(unittest.TestCase):
    def test_escape_beats_flee(self):
        w = world(["~..", "...", "..."], at=(0, 0))
        w.entities = [Entity("npc", 1, (2, 0), code="gnawer")]
        out = dispatch(w, ctx(w))
        self.assertEqual(out.state, "Escape")
        self.assertNotEqual((out.intents[0]["x"], out.intents[0]["y"]), (0, 0))

    def test_flee_when_hostile_would_win(self):
        w = world(["...", "...", "..."], at=(1, 1))
        w.entities = [Entity("npc", 5, (2, 1), code="gnawer")]
        out = dispatch(w, ctx(w))
        self.assertEqual(out.state, "Flee")
        self.assertEqual(out.intents[0]["x"], 0)


class DecideIntegrationTest(unittest.TestCase):
    def test_fight_only_when_estimate_favors_us(self):
        w = world(["...", "...", "..."], at=(1, 1))
        peer = Entity("character", 5, (2, 1), code="peer")
        w.entities = [peer]
        w.health = 500
        w.lives = 10
        w.threat.record(("character", "peer"), 1)
        pol = Policy(kind="scripted", on_hostile="fight", hostile=["character"], hostile_range=1)
        d = decide(w, Memory(), pol, random.Random(0), params={"risk": 1.0, "lives_floor": 1})
        self.assertEqual(d.intent["verb"], "Use")


if __name__ == "__main__":
    unittest.main()
