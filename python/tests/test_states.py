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
            return StateOutcome(None, "sticky", state=self.name, wait=True)

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


class DispatcherFallThroughTest(unittest.TestCase):
    """A44: a state whose guard holds but sends no intent yields the round,
    unless it declares an intentional wait (``StateOutcome.wait``)."""

    def assert_yielded(self, out, *states):
        self.assertEqual([y.split(":")[0] for y in out.yielded], list(states))

    def test_state_without_wait_falls_through_and_reasons_are_kept(self):
        class Empty(State):
            name = "Empty"

            def guard(self, world, ctx):
                return True

            def done(self, world, ctx):
                return False

            def act(self, world, ctx):
                return StateOutcome(None, "nothing", state=self.name)

        w = world(["..."])
        m = Memory()
        with mock.patch.object(dispatch_module, "STATES", (Empty(), *STATES)):
            out = dispatch(w, ctx(w, m))
        self.assertEqual(out.state, "Explore")
        self.assertIsNotNone(out.intents)
        self.assertEqual(out.yielded, ["Empty: nothing"])
        self.assertEqual(m.state, "Explore")

    # Intentional waits hold the round.

    def test_sync_and_downed_wait(self):
        w = world(["..."])
        w.pos = None
        out = dispatch(w, ctx(w))
        self.assertEqual((out.state, out.intents, out.wait), ("Sync", None, True))
        w.pos, w.alive = (0, 0), False
        out = dispatch(w, ctx(w))
        self.assertEqual((out.state, out.intents, out.wait), ("Downed", None, True))

    def test_idle_waits(self):
        w = world(["..."])
        out = dispatch(w, PlayContext(Memory(), Policy(kind="idle"), random.Random(0)))
        self.assertEqual((out.state, out.intents, out.wait), ("Idle", None, True))

    def test_flee_with_nowhere_to_go_waits(self):
        w = world(["###", "#.#", "###"], at=(1, 1))
        w.entities = [Entity("npc", 5, (2, 1))]
        out = dispatch(w, ctx(w, on_hostile="flee", hostile=["npc"], hostile_range=2))
        self.assertEqual((out.state, out.intents, out.reason, out.wait), ("Flee", None, "nowhere to flee", True))

    def test_heal_resting_waits(self):
        from agentrealm_agent.healing import save_regen_yes
        from agentrealm_agent.knowledge_base import KnowledgeBase
        from agentrealm_agent.world import ZoneFact

        w = world(["...", "...", "..."], at=(0, 0))
        w.health, w.max_health = 5, 10
        w.record_respawn_anchor(7, (0, 0))
        w.zones[7] = {(0, 0): ZoneFact(safe=True)}
        kb = KnowledgeBase.empty("sandbox")
        save_regen_yes(kb)
        out = dispatch(w, PlayContext(Memory(), Policy(kind="scripted"), random.Random(0), knowledge=kb))
        self.assertEqual((out.state, out.intents, out.reason, out.wait), ("Heal", None, "rest in safe zone", True))

    def test_recover_waits_to_open_chest(self):
        from agentrealm_agent.zone_discovery import apply_zone

        w = world(["....."], at=(1, 0))
        here = w.pos
        w.apply_events(
            [{"tick": 5, "events": [{"kind": "Died", "cause": "killed", "chest_id": 80, "map_id": 7, "x": 0, "y": 0}]}]
        )
        w.map_id, w.pos = 7, here
        apply_zone(w, 7, 1, 0, {"safe": True, "brightness": 1})
        out = dispatch(w, ctx(w, pickup=True, goals=["explore"]))
        self.assertEqual((out.state, out.intents, out.reason, out.wait), ("Recover", None, "open chest 80", True))

    # Each shipped state driven into guard-holds-but-no-intent. Heal, Gather
    # and Recover's real cases are in test_heal, test_gather and test_recover.

    def test_escape_with_no_route_falls_through(self):
        w = world(["###", "#~#", "###"], at=(1, 1))
        out = dispatch(w, ctx(w, avoid_blocks=["lava"]))
        self.assert_yielded(out, "Escape", "Explore")
        self.assertEqual(out.yielded[0], "Escape: no escape route")
        self.assertEqual((out.state, out.intents), ("", None))

    def test_retreat_with_safe_tile_walled_off_falls_through(self):
        from agentrealm_agent.directives import PARAM_DEFAULTS
        from agentrealm_agent.zone_discovery import apply_zone

        # Safe tile at (4, 0) behind a wall; open ground to the south for Flee.
        w = world(["..#..", "..###", "....."], at=(0, 0))
        w.health, w.lives = 3, 6
        w.entities = [Entity("npc", 1, (1, 0), code="gnawer")]
        w.threat.record(("npc", "gnawer"), 5)
        apply_zone(w, 7, 4, 0, {"safe": True, "brightness": 1})
        c = ctx(w, on_hostile="flee", hostile=["npc"], hostile_range=2)
        c.params = {**PARAM_DEFAULTS, "retreat_hits": 2, "risk": 0.0, "lives_floor": 1}
        out = dispatch(w, c)
        self.assertEqual(out.yielded, ["Retreat: safe tile unreachable"])
        self.assertEqual(out.state, "Flee")
        self.assertIsNotNone(out.intents)

    def test_fight_whose_target_vanishes_falls_through(self):
        from agentrealm_agent.directives import PARAM_DEFAULTS

        fight_module = importlib.import_module("agentrealm_agent.states.fight")
        w = world(["...", "...", "..."], at=(0, 0))
        w.health, w.lives = 500, 10
        npc = Entity("npc", 5, (1, 0), code="snotling")
        w.entities = [npc]
        w.threat.record(("npc", "snotling"), 1)
        c = ctx(w, on_hostile="fight", hostile=["npc"], hostile_range=2, goals=["explore"])
        c.params = {**PARAM_DEFAULTS, "risk": 1.0, "lives_floor": 1}
        real = fight_module.fight_target
        calls = iter([True])

        def target_gone_after_guard(world_, policy, never_attack):
            # The guard sees the target; by act the world has changed.
            return real(world_, policy, never_attack) if next(calls, False) else None

        with mock.patch.object(fight_module, "fight_target", side_effect=target_gone_after_guard):
            out = dispatch(w, c)
        self.assertEqual(out.yielded[0], "Fight: no target")

    def test_loot_whose_pickup_vanishes_falls_through(self):
        loot_module = importlib.import_module("agentrealm_agent.states.loot")
        w = world(["....", "....", "...."], at=(1, 1))
        seen = StateOutcome([{"verb": "Take", "supply_id": 3}], "take 3", state="Loot")
        calls = iter([seen])
        with mock.patch.object(loot_module, "loot_outcome", side_effect=lambda *a, **k: next(calls, None)):
            out = dispatch(w, ctx(w, pickup=True, goals=["explore"]))
        self.assertEqual(out.yielded, ["Loot: nothing to loot"])
        self.assertEqual(out.state, "Explore")
        self.assertIsNotNone(out.intents)

    def test_investigate_whose_target_vanishes_falls_through(self):
        from agentrealm_agent.interest_list import InterestItem, read_key

        investigate_module = importlib.import_module("agentrealm_agent.states.investigate")
        w = world(["....", "....", "...."], at=(1, 1))
        item = InterestItem("read_block", "read sign @0,0", read_key(7, (0, 0)), map_id=7, pos=(0, 0))
        calls = iter([item])
        with mock.patch.object(investigate_module, "pick_interest_tick", side_effect=lambda *a, **k: next(calls, None)):
            out = dispatch(w, ctx(w, goals=["explore"]))
        self.assertEqual(out.yielded, ["Investigate: nothing to investigate"])
        self.assertEqual(out.state, "Explore")
        self.assertIsNotNone(out.intents)

    def test_travel_walled_off_falls_through(self):
        from agentrealm_agent.travel.ops import TravelOp

        w = world(["#####", "#.#.#", "#####"], at=(1, 1))
        m = Memory(travel_ops=[TravelOp("point", map_id=7, x=3, y=1)])
        out = dispatch(w, PlayContext(m, Policy(kind="scripted", goals=["explore"]), random.Random(0)))
        self.assert_yielded(out, "Travel", "Explore")
        self.assertIn("blocked", out.yielded[0])
        self.assertEqual((out.state, out.intents), ("", None))
        self.assertEqual(out.reason, f"no state ({'; '.join(out.yielded)})")

    def test_explore_with_no_move_falls_through_to_no_state(self):
        w = world(["###", "#.#", "###"], at=(1, 1))
        out = dispatch(w, ctx(w, goals=["explore"]))
        self.assertEqual((out.state, out.intents), ("", None))
        self.assertEqual(out.yielded, ["Explore: no goal reachable"])
        self.assertEqual(out.reason, "no state (Explore: no goal reachable)")


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
