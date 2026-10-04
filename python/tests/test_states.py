"""A5: priority dispatcher and list[Intent] test seam."""

import importlib
import random
import unittest
from unittest import mock

from agentrealm_agent.brain import decide
from agentrealm_agent.config import Policy
from agentrealm_agent.directives import default_directives
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


def ctx(w: WorldModel, m: Memory | None = None, **policy_kw) -> PlayContext:
    return PlayContext(
        m or Memory(),
        Policy(kind="scripted", **policy_kw),
        random.Random(0),
        directives=default_directives(),
    )


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

    def test_loot_beats_explore(self):
        w = world(["..."], at=(1, 1))
        w.entities = [Entity("supply", 8, (1, 2), "heart")]
        out = dispatch(w, ctx(w))
        self.assertEqual(out.state, "Loot")
        self.assertEqual(out.intents[0]["verb"], "Take")

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
            # Guard with no intent falls through (A44); hysteresis keeps Sticky once active.
            self.assertEqual(dispatch(w, ctx(w, m)).state, "Explore")
            m.state = "Sticky"
            sticky.on = False
            self.assertEqual(dispatch(w, ctx(w, m)).state, "Sticky")
            w.pos = None
            self.assertEqual(dispatch(w, ctx(w, m)).state, "Sync")
            w.pos = (0, 0)
            self.assertEqual(dispatch(w, ctx(w, m)).state, "Explore")
            m.state, sticky.off = "Sticky", True
            self.assertEqual(dispatch(w, ctx(w, m)).state, "Explore")


class DispatcherFallThroughTest(unittest.TestCase):
    """A44: guard with no intent yields to the next state unless waiting."""

    def test_sync_and_downed_do_not_fall_through(self):
        w = world(["..."])
        w.pos = None
        self.assertEqual(dispatch(w, ctx(w)).state, "Sync")
        w.pos, w.alive = (0, 0), False
        self.assertEqual(dispatch(w, ctx(w)).state, "Downed")

    def test_heal_yield_falls_through_heal_wait_does_not(self):
        from agentrealm_agent.healing import save_regen_yes
        from agentrealm_agent.knowledge_base import KnowledgeBase

        w = world(["...", "...", "..."], at=(2, 2))
        w.health = 4
        w.zones[7] = {}
        m = Memory()
        out = dispatch(w, ctx(w, m))
        self.assertEqual(out.state, "Explore")
        self.assertIsNotNone(out.intents)

        from agentrealm_agent.world import ZoneFact

        w2 = world(["...", "...", "..."], at=(0, 0))
        w2.health, w2.max_health = 5, 10
        w2.record_respawn_anchor(7, (0, 0))
        w2.zones[7] = {(0, 0): ZoneFact(safe=True)}
        kb = KnowledgeBase.empty("sandbox")
        save_regen_yes(kb)
        out = dispatch(w2, PlayContext(Memory(), Policy(kind="scripted"), random.Random(0), knowledge=kb))
        self.assertEqual((out.state, out.intents), ("Heal", None))

    def test_escape_retreat_and_fight_hand_off_lower_states(self):
        from agentrealm_agent.directives import PARAM_DEFAULTS
        from agentrealm_agent.zone_discovery import apply_zone

        w = world(["###", "#~#", "###"], at=(1, 1))
        w.view.tiles[(1, 1)] = "lava"
        out = dispatch(w, ctx(w, avoid_blocks=["lava"]))
        self.assertNotEqual(out.state, "Escape")

        w = world([".....", "....."], at=(1, 0))
        w.health, w.lives = 3, 6
        w.entities = [Entity("npc", 1, (0, 0), code="gnawer")]
        w.threat.record(("npc", "gnawer"), 5)
        apply_zone(w, 7, 4, 0, {"safe": True, "brightness": 1})
        for x in range(5):
            w.view.tiles[(x, 0)] = "wall"
        c = ctx(w, on_hostile="ignore")
        c.params = {**PARAM_DEFAULTS, "retreat_hits": 2, "risk": 0.0, "lives_floor": 1}
        self.assertNotEqual(dispatch(w, c).state, "Retreat")

        w = world([".#.", "##.", "..."], at=(0, 0))
        w.health, w.lives = 500, 10
        w.entities = [Entity("npc", 5, (2, 0), code="snotling")]
        w.threat.record(("npc", "snotling"), 1)
        c = ctx(w, on_hostile="fight", hostile=["npc"], hostile_range=2)
        c.params = {**PARAM_DEFAULTS, "risk": 1.0, "lives_floor": 1}
        self.assertEqual(dispatch(w, c).state, "Flee")

    def test_flee_and_idle_keep_empty_rounds(self):
        w = world(["###", "#.#", "###"], at=(1, 1))
        w.entities = [Entity("npc", 5, (2, 1))]
        out = dispatch(w, ctx(w, on_hostile="flee", hostile=["npc"], hostile_range=2))
        self.assertEqual((out.state, out.intents), ("Flee", None))
        w = world(["..."])
        out = dispatch(w, PlayContext(Memory(), Policy(kind="idle"), random.Random(0)))
        self.assertEqual((out.state, out.intents), ("Idle", None))

    def test_recover_open_chest_waits_gather_falls_through(self):
        from agentrealm_agent.zone_discovery import apply_zone
        from agentrealm_agent.directives import Directives

        w = world(["....."], at=(1, 0))
        here = w.pos
        w.apply_events(
            [{"tick": 5, "events": [{"kind": "Died", "cause": "killed", "chest_id": 80, "map_id": 7, "x": 0, "y": 0}]}]
        )
        w.map_id, w.pos = 7, here
        apply_zone(w, 7, 1, 0, {"safe": True, "brightness": 1})
        out = dispatch(w, ctx(w, pickup=True, goals=["hold"]))
        self.assertEqual((out.state, out.reason), ("Recover", "open chest 80"))

        w = world(["######", "#..#.#", "######"], at=(1, 1))
        w.view.tiles[(4, 1)] = "grass"
        apply_zone(w, 7, 4, 1, {"safe": True, "brightness": 1})
        w.gems = 0
        c = PlayContext(
            Memory(),
            Policy(kind="scripted", pickup=False, on_hostile="ignore", goals=["explore"]),
            random.Random(0),
            directives=Directives(goals=["gather_gems:3"]),
        )
        out = dispatch(w, c)
        self.assertNotEqual(out.state, "Gather")

    def test_loot_and_investigate_fall_through_on_empty_act(self):
        w = world(["..."], at=(1, 1))
        empty = StateOutcome(None, "nothing to loot", state="Loot")
        with mock.patch.object(__import__("agentrealm_agent.states.loot", fromlist=["LootState"]).LootState, "guard", return_value=True):
            with mock.patch.object(__import__("agentrealm_agent.states.loot", fromlist=["LootState"]).LootState, "act", return_value=empty):
                out = dispatch(w, ctx(w, pickup=True))
        self.assertEqual(out.state, "Explore")
        item = mock.Mock(kind="read_block", map_id=7, pos=(0, 0), reason="sign")
        with mock.patch("agentrealm_agent.interest_list.pick_interest_tick", return_value=item):
            with mock.patch(
                "agentrealm_agent.states.investigate.InvestigateState.act",
                return_value=StateOutcome(None, "nothing to investigate", state="Investigate"),
            ):
                out = dispatch(w, ctx(w))
        self.assertEqual(out.state, "Explore")

    def test_travel_blocked_falls_through_to_explore(self):
        from agentrealm_agent.travel.ops import TravelOp
        from agentrealm_agent.states.travel import TravelState

        w = world(["....", "...."], at=(0, 0))
        m = Memory(travel_ops=[TravelOp("point", map_id=7, x=3, y=0)])
        empty = StateOutcome(None, "travel blocked", state="Travel")
        with mock.patch.object(TravelState, "guard", return_value=True):
            with mock.patch.object(TravelState, "act", return_value=empty):
                out = dispatch(w, PlayContext(m, Policy(kind="scripted", goals=["explore"]), random.Random(0)))
        self.assertEqual(out.state, "Explore")
        self.assertIsNotNone(out.intents)

    def test_explore_with_no_move_falls_through_to_no_state(self):
        w = world(["###", "#.#", "###"], at=(1, 1))
        out = dispatch(w, ctx(w, goals=["explore"]))
        self.assertEqual((out.state, out.intents), ("", None))


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
        from agentrealm_agent.directives import PARAM_DEFAULTS

        w = world(["...", "...", "..."], at=(1, 1))
        w.entities = [Entity("character", 9, (2, 1), code="peer")]
        w.health, w.lives = 500, 10
        w.threat.record(("character", "peer"), 1)
        pol = Policy(kind="scripted", on_hostile="fight", hostile=["character"])
        params = {**PARAM_DEFAULTS, "risk": 1.0, "lives_floor": 1}
        self.assertEqual(decide(w, Memory(), pol, random.Random(0), params=params).intent["verb"], "Use")
        d = decide(w, Memory(), pol, random.Random(0), never_attack=["character"], params=params)
        self.assertEqual(d.intent["verb"], "SetPosition")

    def test_fight_clears_the_plan(self):
        w = world(["...", "...", "..."], at=(1, 1))
        w.entities = [Entity("character", 9, (2, 1), code="peer")]
        w.health, w.lives = 500, 10
        w.threat.record(("character", "peer"), 1)
        m = Memory(path=[(0, 0)], goal="explore")
        pol = Policy(kind="scripted", on_hostile="fight", hostile=["character"])
        from agentrealm_agent.directives import PARAM_DEFAULTS

        decide(w, m, pol, random.Random(0), params={**PARAM_DEFAULTS, "risk": 1.0, "lives_floor": 1})
        self.assertEqual(m.path, [])

if __name__ == "__main__":
    unittest.main()
