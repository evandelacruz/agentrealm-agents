"""A5: priority dispatcher and list[Intent] test seam."""

import importlib
import random
import unittest
from unittest import mock

from agentrealm_agent.brain import decide
from agentrealm_agent.config import Policy
from agentrealm_agent.memory import Memory
from agentrealm_agent.navigation.rejection import NavMemory
from agentrealm_agent.states import STATES, PlayContext, State, StateOutcome, dispatch, scripted_outcome
from agentrealm_agent.world import Entity, WorldModel

dispatch_module = importlib.import_module("agentrealm_agent.states.dispatch")


def world(rows: list[str], at=(0, 0), perception=3) -> WorldModel:
    glyph = {".": "dirt", "#": "wall", "D": "framed_door", "~": "lava"}
    w = WorldModel(character_id=1, map_id=7, pos=at, perception=perception)
    for y, row in enumerate(rows):
        for x, g in enumerate(row):
            w.view.tiles[(x, y)] = glyph[g]
    w.terrain_center, w.terrain_map = at, 7
    return w


def ctx(w: WorldModel, m: Memory | None = None, params=None, **policy_kw) -> PlayContext:
    kw = {}
    if params is not None:
        kw["params"] = params
    return PlayContext(m or Memory(), Policy(kind="scripted", **policy_kw), random.Random(0), **kw)


class DispatchPriorityTest(unittest.TestCase):
    def test_sync_beats_explore(self):
        w = world(["..."])
        w.pos = None
        out = dispatch(w, ctx(w))
        self.assertEqual(out.state, "Sync")
        self.assertIsNone(out.intents)

    def test_downed_beats_explore(self):
        w = world(["..."])
        w.alive = False
        out = dispatch(w, ctx(w))
        self.assertEqual(out.state, "Downed")
        self.assertIsNone(out.intents)

    def test_explore_returns_intent_list(self):
        w = world(["....", "...."])
        for x in range(-1, 5):
            w.view.tiles.setdefault((x, -1), "")
            w.view.tiles.setdefault((x, 2), "")
        w.view.tiles[(-1, 0)] = w.view.tiles[(-1, 1)] = ""
        out = dispatch(w, ctx(w, goals=["explore"]))
        self.assertEqual(out.state, "Explore")
        self.assertIsNotNone(out.intents)
        self.assertEqual(out.intents[0]["verb"], "SetPosition")

    def test_idle_policy(self):
        w = world(["..."])
        out = dispatch(w, PlayContext(Memory(), Policy(kind="idle"), random.Random(0)))
        self.assertEqual(out.state, "Idle")
        self.assertIsNone(out.intents)

    def test_wander_policy_steps_through_idle(self):
        w = world(["...", "...", "..."], at=(1, 1))
        out = dispatch(w, PlayContext(Memory(), Policy(kind="wander"), random.Random(0)))
        self.assertEqual(out.state, "Idle")
        self.assertEqual(len(out.intents), 1)
        step = out.intents[0]
        self.assertEqual(step["verb"], "SetPosition")
        self.assertIn((step["x"], step["y"]), w.open_neighbours((1, 1), set()))

    def test_wander_keeps_off_blocked_tiles(self):
        w = world(["..", ".."], at=(0, 0))
        m = Memory(nav=NavMemory(impassable={(7, (1, 0))}, wait_tile=(7, (0, 1))))
        out = dispatch(w, PlayContext(m, Policy(kind="wander"), random.Random(0)))
        self.assertEqual((out.intents[0]["x"], out.intents[0]["y"]), (1, 1))

    def test_no_state_sends_nothing(self):
        w = world(["..."])
        m = Memory(state="Explore")
        out = dispatch(w, PlayContext(m, Policy(kind="bogus"), random.Random(0)))
        self.assertEqual((out.state, out.intents, out.reason), ("", None, "no state"))
        self.assertEqual(m.state, "")


class NavAgingTest(unittest.TestCase):
    def test_dispatch_ages_rejection_learnings_once(self):
        w = world(["..."])
        w.tick = 20
        m = Memory(nav=NavMemory(wait_tile=(7, (1, 0)), occupant_until={(7, (2, 0)): 21, (7, (1, 0)): 20}))
        dispatch(w, ctx(w, m))
        self.assertIsNone(m.nav.wait_tile)
        self.assertEqual(m.nav.occupant_until, {(7, (2, 0)): 21})

    def test_no_state_still_ages_rejection_learnings(self):
        w = world(["..."])
        m = Memory(nav=NavMemory(wait_tile=(7, (1, 0))))
        dispatch(w, PlayContext(m, Policy(kind="bogus"), random.Random(0)))
        self.assertIsNone(m.nav.wait_tile)


class HysteresisTest(unittest.TestCase):
    class Sticky(State):
        """Takes over on ``on``, lets go only on ``off``."""

        name = "Sticky"
        on = off = False

        def guard(self, world, ctx):
            return self.on

        def done(self, world, ctx):
            return self.off

        def act(self, world, ctx):
            return StateOutcome(None, "sticky", state=self.name)

    def test_active_state_runs_until_done(self):
        sticky = self.Sticky()
        w = world(["..."])
        m = Memory()
        with mock.patch.object(dispatch_module, "STATES", (STATES[0], STATES[1], sticky, *STATES[2:])):
            sticky.on = True
            self.assertEqual(dispatch(w, ctx(w, m)).state, "Sticky")
            sticky.on = False
            self.assertEqual(dispatch(w, ctx(w, m)).state, "Sticky")
            w.pos = None
            self.assertEqual(dispatch(w, ctx(w, m)).state, "Sync")
            w.pos = (0, 0)
            self.assertEqual(dispatch(w, ctx(w, m)).state, "Explore")
            m.state, sticky.off = "Sticky", True
            self.assertEqual(dispatch(w, ctx(w, m)).state, "Explore")


class DecideShimTest(unittest.TestCase):
    def test_decide_keeps_the_first_intent_reason_and_reflex(self):
        w = world(["...", "...", "..."], at=(1, 1))
        w.entities = [Entity("supply", 8, (1, 2))]
        pol = Policy(kind="scripted", pickup=True)
        out = scripted_outcome(w, Memory(), pol, random.Random(0), never_attack=[])
        d = decide(w, Memory(), pol, random.Random(0))
        self.assertTrue(out.reflex)
        self.assertEqual(out.intents, [d.intent])
        self.assertEqual((d.reason, d.reflex), (out.reason, out.reflex))

    def test_decide_maps_no_intents_to_none(self):
        w = world(["..."])
        w.alive = False
        d = decide(w, Memory(), Policy(kind="scripted"), random.Random(0))
        self.assertIsNone(d.intent)
        self.assertEqual((d.reason, d.reflex), ("downed", False))

    def test_decide_passes_never_attack_through(self):
        w = world(["...", "...", "..."], at=(1, 1))
        w.entities = [Entity("character", 9, (2, 1), code="peer")]
        w.health = 500
        w.lives = 10
        w.threat.record(("character", "peer"), 1)
        pol = Policy(kind="scripted", on_hostile="fight", hostile=["character"])
        self.assertEqual(
            decide(w, Memory(), pol, random.Random(0), params={"risk": 1.0, "lives_floor": 1}).intent["verb"],
            "Use",
        )
        d = decide(w, Memory(), pol, random.Random(0), never_attack=["character"])
        self.assertEqual(d.intent["verb"], "SetPosition")

    def test_fight_keeps_the_plan(self):
        w = world(["...", "...", "..."], at=(1, 1))
        w.entities = [Entity("character", 9, (2, 1), code="peer")]
        w.health = 500
        w.lives = 10
        w.threat.record(("character", "peer"), 1)
        m = Memory(path=[(0, 0)], goal="explore")
        pol = Policy(kind="scripted", on_hostile="fight", hostile=["character"])
        decide(w, m, pol, random.Random(0), params={"risk": 1.0, "lives_floor": 1})
        self.assertEqual(m.path, [(0, 0)])

if __name__ == "__main__":
    unittest.main()
