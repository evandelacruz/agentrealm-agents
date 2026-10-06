"""A5: priority dispatcher and list[Intent] test seam."""

import importlib
import random
import unittest
from unittest import mock

from agentrealm_agent.brain import decide
from agentrealm_agent.config import Policy
from agentrealm_agent.directives import PARAM_DEFAULTS, default_directives
from agentrealm_agent.memory import Memory
from agentrealm_agent.navigation.rejection import NavMemory
from agentrealm_agent.plan import Plan
from agentrealm_agent.states import STATES, PlayContext, State, StateOutcome, dispatch
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


def ctx(w: WorldModel, m: Memory | None = None, plan: Plan | None = None, **policy_kw) -> PlayContext:
    return PlayContext(
        m or Memory(),
        Policy(kind="scripted", **policy_kw),
        random.Random(0),
        directives=default_directives(),
        plan=plan,
    )


def plan_of(*ops: dict) -> Plan:
    return Plan(list(ops), dict(PARAM_DEFAULTS))


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

    def test_pickup_beats_explore(self):
        w = world(["..."], at=(1, 1))
        w.entities = [Entity("supply", 8, (1, 2), "heart")]
        out = dispatch(w, ctx(w))
        self.assertEqual(out.state, "Pickup")
        self.assertEqual(out.intents[0]["verb"], "Take")
        self.assertTrue(out.reflex)

    def test_loot_runs_only_for_a_fetch_item_op(self):
        w = world(["....."] * 3, at=(0, 1))
        w.entities = [Entity("supply", 8, (4, 1), "heart")]
        out = dispatch(w, ctx(w, pickup=False, plan=plan_of({"op": "fetch_item", "code": "heart"})))
        self.assertEqual(out.state, "Loot")
        self.assertEqual(out.intents[0]["verb"], "SetPosition")
        out = dispatch(w, ctx(w, pickup=False))
        self.assertEqual(out.state, "Explore", "no op: the safe default, not Loot")

    def test_executor_waits_for_its_op(self):
        w = world(["....."] * 3, at=(0, 1))
        out = dispatch(w, ctx(w, plan=plan_of({"op": "travel", "to": "point", "x": 4, "y": 1})))
        self.assertEqual(out.state, "Travel")
        self.assertEqual(out.reason.split(" ")[0], "travel:point")

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

    def test_heal_in_safe_zone_keeps_moving(self):
        from agentrealm_agent.healing import save_regen_yes
        from agentrealm_agent.knowledge_base import KnowledgeBase
        from agentrealm_agent.world import ZoneFact

        w = world(["...", "...", "..."], at=(0, 0))
        w.health, w.max_health = 5, 10
        w.record_respawn_anchor(7, (0, 0))
        w.zones[7] = {(x, y): ZoneFact(safe=True) for x in range(3) for y in range(3)}
        kb = KnowledgeBase.empty("sandbox")
        save_regen_yes(kb)
        out = dispatch(w, PlayContext(Memory(), Policy(kind="scripted"), random.Random(0), knowledge=kb))
        self.assertEqual((out.state, out.wait), ("Heal", False))
        self.assertEqual(out.intents[0]["verb"], "SetPosition", "Heal keeps moving inside a safe zone with room")
        self.assertTrue(out.reason.startswith("heal in safe ground: "), out.reason)

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
        self.assertEqual(out.yielded, ["Escape: no escape route"])
        self.assertEqual((out.state, out.intents, out.reason, out.wait), ("Explore", None, "boxed in", True))

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

    def test_loot_with_nothing_to_fetch_falls_through_to_safe_default(self):
        w = world(["....", "....", "...."], at=(1, 1))
        out = dispatch(w, ctx(w, plan=plan_of({"op": "fetch_item", "code": "heart"})))
        self.assertEqual(out.yielded, ["Loot: no heart in sight"])
        self.assertEqual(out.state, "Explore")
        self.assertIsNotNone(out.intents)

    def test_investigate_whose_npc_is_gone_falls_through_to_safe_default(self):
        w = world(["....", "....", "...."], at=(1, 1))
        op = {"op": "say", "npc_type": "fake_sage", "text": "hello"}
        out = dispatch(w, ctx(w, plan=plan_of(op)))
        self.assertEqual(out.yielded, ["Investigate: no such NPC in sight"])
        self.assertEqual(out.state, "Explore")
        self.assertIsNotNone(out.intents)

    def test_travel_walled_off_falls_through_to_safe_default(self):
        w = world(["#####", "#.#.#", "#####"], at=(1, 1))
        out = dispatch(w, ctx(w, plan=plan_of({"op": "travel", "to": "point", "x": 3, "y": 1})))
        self.assert_yielded(out, "Travel")
        self.assertIn("blocked", out.yielded[0])
        self.assertEqual((out.state, out.intents, out.reason, out.wait), ("Explore", None, "boxed in", True))

    def test_explore_area_with_no_step_falls_through_to_safe_default(self):
        # The op's frontier (column 3) is walled off.
        w = world(["###.", "#.#.", "###."], at=(1, 1))
        c = ctx(w, plan=plan_of({"op": "explore_area", "x": 1, "y": 1, "radius": 5}))
        out = dispatch(w, c)
        # Explore itself runs the safe default when its op has no step.
        self.assertEqual((out.state, out.intents, out.reason, out.wait), ("Explore", None, "boxed in", True))
        self.assertFalse(out.progress, "the safe default's move is no progress on the op")
        self.assertEqual(c.plan.current()["op"], "explore_area")

    def test_safe_default_with_nothing_to_explore_looks_around(self):
        w = world(["...", "...", "..."], at=(1, 1))
        out = dispatch(w, ctx(w))
        self.assertEqual(out.state, "Explore")
        self.assertEqual(out.intents[0]["verb"], "SetPosition", "never idle with no plan op")


class SafeDefaultPushesTheBoundaryTest(unittest.TestCase):
    """Live: hurt, safe ground fully explored, "look around" took 18% of
    decisions while unexplored ground lay east. The safe default now walks
    to the nearest frontier outside safe ground."""

    def hurt_in_a_small_safe_zone(self) -> WorldModel:
        from agentrealm_agent.world import ZoneFact

        # A walled corridor whose only unknown edge is its east end (x=12).
        w = world(["############", "#...........", "############"], at=(1, 1))
        w.health, w.max_health, w.alive = 3, 10, True
        w.record_respawn_anchor(7, (1, 1))
        w.zones[7] = {(1, 1): ZoneFact(safe=True), (2, 1): ZoneFact(safe=True)}
        return w

    def test_explores_the_nearest_frontier_outside_safe_ground(self):
        w = self.hurt_in_a_small_safe_zone()
        m = Memory(heal_regen_absent=True)  # Heal yields: no food, no regen
        out = dispatch(w, ctx(w, m))
        self.assertEqual(out.state, "Explore")
        self.assertEqual(out.intents, [{"verb": "SetPosition", "x": 2, "y": 1}])
        self.assertIn("(11, 1)", out.reason)
        self.assertNotIn("look around", out.reason)

    def test_never_loops_look_around_while_the_frontier_is_reachable(self):
        # From every cell on the way, safe ground behind it, it heads east.
        w = self.hurt_in_a_small_safe_zone()
        for x in range(1, 10):
            w.pos, w.terrain_center = (x, 1), (x, 1)
            out = dispatch(w, ctx(w, Memory(heal_regen_absent=True)))
            self.assertNotIn("look around", out.reason)
            self.assertEqual(out.intents, [{"verb": "SetPosition", "x": x + 1, "y": 1}])

    def test_a_hostile_by_the_frontier_keeps_it_closed(self):
        w = self.hurt_in_a_small_safe_zone()
        w.entities = [Entity("npc", 5, (10, 1), "slime")]
        out = dispatch(w, ctx(w, Memory(heal_regen_absent=True), hostile=["npc"], hostile_range=2))
        self.assertIn("look around", out.reason)


class SyncWakeTest(unittest.TestCase):
    def asleep_off_the_map(self) -> WorldModel:
        w = world(["..."])
        w.pos, w.asleep = None, True
        return w

    def test_asleep_sends_one_wait_and_rereads_self(self):
        c = ctx(self.asleep_off_the_map(), Memory(need_self=False))
        out = dispatch(self.asleep_off_the_map(), c)
        self.assertEqual((out.state, out.reason), ("Sync", "wake"))
        self.assertEqual(out.intents, [{"verb": "Wait"}], "intents is a list like every other state's")
        self.assertTrue(c.memory.need_self, "self is re-read, so a missed wake cannot loop")

    def test_decide_wakes_an_asleep_character_without_error(self):
        d = decide(self.asleep_off_the_map(), Memory(need_self=False), Policy(kind="scripted"), random.Random(0))
        self.assertEqual((d.intent, d.reason), ({"verb": "Wait"}, "wake"))

    def test_awake_with_no_position_sends_nothing(self):
        w = world(["..."])
        w.pos = None
        d = decide(w, Memory(need_self=False), Policy(kind="scripted"), random.Random(0))
        self.assertEqual((d.intent, d.reason), (None, "sync"))


class DecideShimTest(unittest.TestCase):
    def test_decide_keeps_the_first_intent_reason_and_reflex(self):
        w = world(["...", "...", "..."], at=(1, 1))
        w.entities = [Entity("supply", 8, (1, 2))]
        pol = Policy(kind="scripted", pickup=True)
        out = dispatch(w, PlayContext(Memory(), pol, random.Random(0)))
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
