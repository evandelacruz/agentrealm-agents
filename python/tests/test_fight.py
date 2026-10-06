"""A23, A45: Fight state, win-estimate gating, and NPC attacks by id."""

import random
import unittest

from agentrealm_agent.brain import decide
from agentrealm_agent.config import Policy
from agentrealm_agent.directives import PARAM_DEFAULTS
from agentrealm_agent.memory import Memory
from agentrealm_agent.states import PlayContext, dispatch
from agentrealm_agent.states.fight import attack_intent, fight_target, should_fight
from agentrealm_agent.survival import would_lose
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


def ctx(params=None, **policy_kw) -> PlayContext:
    c = PlayContext(Memory(), Policy(kind="scripted", **policy_kw), random.Random(0))
    if params is not None:
        c.params = {**PARAM_DEFAULTS, **params}
    return c


class FightTargetTest(unittest.TestCase):
    def test_npc_in_range(self):
        w = world(["..."], at=(0, 0))
        w.entities = [Entity("npc", 5, (1, 0), code="gnawer")]
        w.hostile_types.add(("npc", "gnawer"))  # a type seen attacking (survival.is_hostile)
        pol = Policy(hostile=["npc"], on_hostile="fight", hostile_range=2)
        t = fight_target(w, pol, [])
        self.assertIsNotNone(t)
        self.assertEqual(t.kind, "npc")

    def test_never_attack_npc_type(self):
        w = world(["..."], at=(0, 0))
        w.entities = [Entity("npc", 5, (1, 0), code="goblin")]
        self.assertIsNone(fight_target(w, Policy(hostile=["npc"], on_hostile="fight"), ["goblin"]))

    def test_attack_intent_targets_npc_by_id(self):
        # The server finds the NPC's block on the tick the Use runs (A45, B126).
        e = Entity("npc", 3, (2, 1))
        self.assertEqual(
            attack_intent(e),
            {"verb": "Use", "target": {"kind": "npc", "npc_id": 3}},
        )


class FightStateTest(unittest.TestCase):
    def test_swings_at_character_when_estimate_ok(self):
        w = world(["...", "...", "..."], at=(1, 1))
        w.entities = [Entity("character", 5, (2, 1), code="peer")]
        w.health, w.lives = 500, 10
        w.threat.record(("character", "peer"), 1)
        out = dispatch(
            w,
            ctx(params={"risk": 1.0, "lives_floor": 1}, on_hostile="fight", hostile=["character"], hostile_range=2),
        )
        self.assertEqual(out.state, "Fight")
        self.assertEqual(out.intents[0]["verb"], "Use")
        self.assertEqual(out.intents[0]["target"]["kind"], "character")

    def test_swings_at_weak_measured_npc(self):
        w = world(["...", "...", "..."], at=(1, 1))
        w.health, w.lives = 500, 10
        w.entities = [Entity("npc", 5, (2, 1), code="snotling")]
        w.threat.record(("npc", "snotling"), 1)
        out = dispatch(
            w,
            ctx(params={"risk": 1.0, "lives_floor": 1}, on_hostile="fight", hostile=["npc"], hostile_range=2),
        )
        self.assertEqual(out.state, "Fight")
        self.assertEqual(out.intents[0]["target"], {"kind": "npc", "npc_id": 5})
        swings = [i for i in out.intents if i["verb"] == "Use"]
        self.assertTrue(swings)
        self.assertTrue(all(i["target"] == {"kind": "npc", "npc_id": 5} for i in swings))

    def test_unmeasured_npc_at_default_params_flees(self):
        w = world(["...", "...", "..."], at=(1, 1))
        w.entities = [Entity("npc", 5, (2, 1), code="gnawer")]
        w.hostile_types.add(("npc", "gnawer"))  # a type seen attacking (survival.is_hostile)
        self.assertTrue(would_lose(w, Policy(hostile=["npc"]), dict(PARAM_DEFAULTS)))
        out = dispatch(w, ctx(on_hostile="fight", hostile=["npc"]))
        self.assertEqual(out.state, "Flee")

    def test_never_on_safe_tile(self):
        w = world(["...", "...", "..."], at=(1, 1))
        w.health, w.lives = 500, 10
        w.entities = [Entity("npc", 5, (2, 1), code="snotling")]
        w.threat.record(("npc", "snotling"), 1)
        apply_zone(w, w.map_id, 1, 1, {"safe": True})
        self.assertFalse(should_fight(w, ctx(on_hostile="fight", hostile=["npc"])))
        out = dispatch(w, ctx(on_hostile="fight", hostile=["npc"]))
        self.assertEqual(out.state, "Explore")

    def test_retreat_queued_after_use(self):
        w = world(["...", "...", "..."], at=(1, 1))
        w.health, w.lives = 500, 10
        w.entities = [Entity("npc", 5, (2, 1), code="snotling")]
        w.threat.record(("npc", "snotling"), 1)
        apply_zone(w, w.map_id, 0, 0, {"safe": True})
        out = dispatch(
            w,
            ctx(on_hostile="fight", hostile=["npc"], params={**PARAM_DEFAULTS, "risk": 1.0, "lives_floor": 1}),
        )
        self.assertEqual(out.state, "Fight")
        verbs = [i["verb"] for i in out.intents]
        self.assertIn("Use", verbs)
        use_i = verbs.index("Use")
        self.assertIn("Step", verbs[use_i + 1 :])

    def test_decide_exposes_submit_queue(self):
        w = world(["...", "...", "..."], at=(1, 1))
        w.health, w.lives = 500, 10
        w.entities = [Entity("npc", 5, (2, 1), code="snotling")]
        w.threat.record(("npc", "snotling"), 1)
        apply_zone(w, w.map_id, 0, 0, {"safe": True})
        d = decide(
            w,
            Memory(),
            Policy(kind="scripted", on_hostile="fight", hostile=["npc"]),
            random.Random(0),
            params={**PARAM_DEFAULTS, "risk": 1.0, "lives_floor": 1},
        )
        self.assertIsNotNone(d.submit_queue)
        self.assertGreater(len(d.submit_queue), 1)

    def test_single_intent_fight_queue_is_not_repaced(self):
        w = world(["...", "...", "..."], at=(1, 1))
        w.health, w.lives = 500, 10
        w.entities = [Entity("npc", 5, (2, 1), code="snotling")]
        w.threat.record(("npc", "snotling"), 1)
        m = Memory()
        m.calm_poll_interval = 1
        d = decide(
            w,
            m,
            Policy(kind="scripted", on_hostile="fight", hostile=["npc"]),
            random.Random(0),
            params={**PARAM_DEFAULTS, "risk": 1.0, "lives_floor": 1},
        )
        self.assertEqual(d.submit_queue, [d.intent])

    def test_attack_uses_are_separate_dicts(self):
        w = world(["...", "...", "..."], at=(1, 1))
        w.health, w.lives = 500, 10
        w.entities = [Entity("npc", 5, (2, 1), code="snotling")]
        w.threat.record(("npc", "snotling"), 1)
        c = ctx(params={"risk": 1.0, "lives_floor": 1}, on_hostile="fight", hostile=["npc"], hostile_range=2)
        c.memory.calm_poll_interval = 100
        out = dispatch(w, c)
        uses = [i for i in out.intents if i["verb"] == "Use"]
        self.assertGreater(len(uses), 1)
        self.assertIsNot(uses[0], uses[1])

    def test_cannot_close_hands_off_to_flee(self):
        # Boxed in at (0, 0); the NPC is two away, outside reach 1.
        w = world([".#.", "##.", "..."], at=(0, 0))
        w.health, w.lives = 500, 10
        w.entities = [Entity("npc", 5, (2, 0), code="snotling")]
        w.threat.record(("npc", "snotling"), 1)
        c = ctx(params={"risk": 1.0, "lives_floor": 1}, on_hostile="fight", hostile=["npc"], hostile_range=2)
        self.assertFalse(should_fight(w, c))
        self.assertEqual(dispatch(w, c).state, "Flee")

    def test_closing_still_fights(self):
        w = world(["...", "...", "..."], at=(0, 0))
        w.health, w.lives = 500, 10
        w.entities = [Entity("npc", 5, (2, 0), code="snotling")]
        w.threat.record(("npc", "snotling"), 1)
        c = ctx(params={"risk": 1.0, "lives_floor": 1}, on_hostile="fight", hostile=["npc"], hostile_range=2)
        out = dispatch(w, c)
        self.assertEqual(out.state, "Fight")
        self.assertEqual(out.intents[0]["verb"], "SetPosition")


if __name__ == "__main__":
    unittest.main()
