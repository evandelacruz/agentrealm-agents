"""A16 Walk run 4 and A23 survive-a-fight run 1 offline: one hostile killed the character in both.

Rebuilt here: Flee swung back below the health floor at a hostile its
weapon had never hurt; Retreat had no safe tile to head for, sent one step
per decision, and detoured round its chaser; entity reads stopped while
walking; and Heal gave up food behind a bush its weapon cuts (A9, A10, A16, A23).
"""

import random
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agentrealm_agent import config
from agentrealm_agent.brain import choose_call
from agentrealm_agent.config import CharacterConfig, Policy
from agentrealm_agent.directives import PARAM_DEFAULTS
from agentrealm_agent.item_table import InventorySupply
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.memory import Memory
from agentrealm_agent.navigation import cost_path
from agentrealm_agent.pathing import grid_params
from agentrealm_agent.runner import Runner
from agentrealm_agent.states import dispatch
from agentrealm_agent.states.base import PlayContext
from agentrealm_agent.states.retreat import RETREAT_PROBE_TICKS
from agentrealm_agent.travel import sync_town
from agentrealm_agent.world import Entity, WorldModel, ZoneFact, chebyshev
from agentrealm_agent.zone_discovery import apply_zone

MAP = 1


def world(at=(10, 10), health=10) -> WorldModel:
    w = WorldModel(character_id=1, map_id=MAP, pos=at, perception=8, health=health, max_health=10, lives=9)
    for x in range(-30, 41):
        for y in range(-10, 31):
            w.view.tiles[(x, y)] = "dirt"
    w.terrain_center, w.terrain_map = at, MAP
    return w


def ctx(on_hostile="flee", kb: KnowledgeBase | None = None) -> PlayContext:
    policy = Policy(kind="scripted", goals=["explore"], on_hostile=on_hostile, hostile=["npc"])
    return PlayContext(Memory(), policy, random.Random(0), params=dict(PARAM_DEFAULTS), knowledge=kb)


def hit(w: WorldModel, npc_id=7, amount=2) -> None:
    w.apply_events([{"tick": w.tick, "events": [
        {"kind": "Attacked"},
        {"kind": "Damaged", "amount": amount, "source_kind": "npc", "source_id": npc_id},
    ]}])
    w.threat.record(("npc", "chaser"), amount)


def flee_gave_up(w: WorldModel, c: PlayContext) -> None:
    """Flee has been running since before the last hit, and gave up running."""
    m = c.memory
    m.state, m.flee_since, m.flee_failed, m.flee_gaps = "Flee", w.tick, True, [(w.tick, 1)]
    w.tick += 5
    hit(w)


def weapon_hurt_chaser(w: WorldModel, kb: KnowledgeBase) -> None:
    w.armed_code = "test_blade"
    kb.items["test_blade"] = {"weapon_damage": {"chaser": 1}}


def uses(out) -> list[dict]:
    return [i for i in out.intents or [] if i["verb"] == "Use"]


class FleeSwingBackTest(unittest.TestCase):
    """``instead_of_fleeing`` never picks a losing fight at or below the health floor."""

    def setUp(self):
        self.w, self.c = world(health=4), ctx(kb=KnowledgeBase("sandbox"))
        self.w.entities = [Entity("npc", 7, (11, 10), code="chaser")]  # adjacent: in weapon reach

    def test_no_swing_back_at_the_floor(self):
        """4/10 against hits of 2 with ``retreat_hits`` 2 is the floor: run, never swing."""
        flee_gave_up(self.w, self.c)
        out = dispatch(self.w, self.c)
        self.assertEqual(out.state, "Flee")
        self.assertFalse(uses(out), out.reason)

    def test_no_fight_when_cornered_at_the_floor(self):
        for x in range(9, 12):
            for y in range(9, 12):
                if (x, y) not in ((10, 10), (11, 10)):
                    self.w.view.tiles[(x, y)] = "wall"
        flee_gave_up(self.w, self.c)
        out = dispatch(self.w, self.c)
        self.assertFalse(uses(out), out.reason)

    def test_swings_back_above_the_floor(self):
        self.w.health = 10
        flee_gave_up(self.w, self.c)
        out = dispatch(self.w, self.c)
        self.assertEqual(out.reason, "not outrunning npc 7: fight npc 7")

    def test_a_win_is_fought_even_at_the_floor(self):
        self.w.health = 2  # bold risk lowers the floor to one hit
        self.c.params["risk"] = 1.0
        self.c.params["fight_margin"] = 0.01
        weapon_hurt_chaser(self.w, self.c.knowledge)
        flee_gave_up(self.w, self.c)
        out = dispatch(self.w, self.c)
        self.assertEqual(out.reason, "not outrunning npc 7: fight npc 7")

    def test_no_win_against_a_type_the_weapon_never_hurt(self):
        """The win estimate clears, but the weapon has never damaged a chaser."""
        self.w.health = 2  # bold risk lowers the floor to one hit
        self.c.params["risk"] = 1.0
        self.c.params["fight_margin"] = 0.01
        self.w.armed_code = "test_blade"
        flee_gave_up(self.w, self.c)
        out = dispatch(self.w, self.c)
        self.assertFalse(uses(out), out.reason)


class RetreatGoalTest(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase("sandbox")
        self.w, self.c = world(health=4), ctx(on_hostile="fight", kb=self.kb)
        self.w.entities = [Entity("npc", 7, (11, 10), code="chaser")]
        hit(self.w)

    def test_heads_for_the_town_cell_with_no_safe_tile_known(self):
        sync_town(self.kb, {"map_id": MAP, "x": -20, "y": 10})
        out = dispatch(self.w, self.c)
        self.assertEqual(out.state, "Retreat")
        self.assertEqual(out.reason, "retreat → safe (-20, 10)")
        self.assertLess(out.intents[0]["x"], 10, "heads west, for town")

    def test_a_nearer_known_safe_tile_beats_the_town_cell(self):
        sync_town(self.kb, {"map_id": MAP, "x": -20, "y": 10})
        apply_zone(self.w, MAP, 0, 10, {"safe": True})
        self.assertEqual(dispatch(self.w, self.c).reason, "retreat → safe (0, 10)")

    def test_a_town_on_another_map_is_not_a_goal(self):
        sync_town(self.kb, {"map_id": MAP + 1, "x": -20, "y": 10})
        self.assertNotEqual(dispatch(self.w, self.c).state, "Retreat")

    def test_takes_the_shortest_way_past_its_chaser(self):
        """A wall with a gap beside the chaser and another far off: the near gap,
        not the detour the default grid takes round the chaser (A23 run 1)."""
        apply_zone(self.w, MAP, -10, 10, {"safe": True})
        for y in range(-10, 31):
            if y not in (10, 28):
                self.w.view.tiles[(5, y)] = "wall"
        self.w.pos = (7, 10)
        self.w.entities = [Entity("npc", 7, (6, 11), code="chaser")]
        self.assertEqual(dispatch(self.w, self.c).state, "Retreat")
        path = self.c.memory.path
        self.assertIn((5, 10), path)
        self.assertEqual(len(path), chebyshev((7, 10), (-10, 10)))
        default = cost_path(self.w, (-10, 10), grid_params(self.c.policy, set(), set()))
        self.assertNotIn((5, 10), default, "the default grid detours: the test exercises the weighting")

    def test_still_keeps_clear_of_a_hostile_it_is_not_running_from(self):
        apply_zone(self.w, MAP, -10, 10, {"safe": True})
        self.w.entities.append(Entity("npc", 8, (0, 10), code="bystander"))  # on the route, out of range
        dispatch(self.w, self.c)
        self.assertTrue(all(chebyshev(p, (0, 10)) >= 2 for p in self.c.memory.path), self.c.memory.path)


class RetreatQueueTest(unittest.TestCase):
    def setUp(self):
        self.w, self.c = world(health=4), ctx(on_hostile="fight")
        apply_zone(self.w, MAP, -10, 10, {"safe": True})
        self.w.entities = [Entity("npc", 7, (11, 10), code="chaser")]
        hit(self.w)

    def test_lets_its_own_queue_run(self):
        m = self.c.memory
        first = dispatch(self.w, self.c)
        self.assertTrue(first.reflex)
        m.held_queue = {"queue_id": "q1", "next_index": 1}
        self.w.pos = (9, 10)
        self.w.tick += 1
        out = dispatch(self.w, self.c)
        self.assertEqual(out.state, "Retreat")
        self.assertTrue(out.wait)
        self.assertFalse(out.reflex, "a reflex would replace the queue")
        self.assertIsNone(out.intents)

    def test_replaces_another_states_queue(self):
        m = self.c.memory
        m.state, m.held_queue = "Travel", {"queue_id": "q1", "next_index": 1}
        out = dispatch(self.w, self.c)
        self.assertTrue(out.reflex)
        self.assertEqual(out.intents[0]["verb"], "SetPosition")

    def test_losing_ground_replaces_the_queue(self):
        """No gain on safety over the probe window, and hits landing: the fallback runs."""
        m = self.c.memory
        dispatch(self.w, self.c)
        m.held_queue = {"queue_id": "q1", "next_index": 1}
        self.w.tick += RETREAT_PROBE_TICKS
        hit(self.w)
        out = dispatch(self.w, self.c)
        self.assertTrue(out.reflex)
        self.assertIn("losing ground", out.reason)

    def test_losing_ground_drinks_what_it_carries(self):
        m = self.c.memory
        self.w.held_supplies = [InventorySupply(5, "small_potion")]
        dispatch(self.w, self.c)
        self.w.tick += RETREAT_PROBE_TICKS
        hit(self.w)
        out = dispatch(self.w, self.c)
        self.assertEqual(out.state, "Retreat")
        self.assertTrue(out.reason.startswith("retreat losing ground: arm and use small_potion"), out.reason)
        self.assertTrue(out.reflex)

    def test_gaining_ground_is_not_losing(self):
        dispatch(self.w, self.c)
        self.w.tick += RETREAT_PROBE_TICKS
        self.w.pos = (6, 10)
        hit(self.w)
        self.assertNotIn("losing ground", dispatch(self.w, self.c).reason)


class RetreatRunnerTest(unittest.TestCase):
    """Through the runner: Retreat's walk queue is held, not resent each round trip."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.object(config, "STATE_DIR", Path(tmp.name))
        patch.start()
        self.addCleanup(patch.stop)

    def test_the_queue_keeps_running(self):
        class Fake:
            def __init__(self):
                self.sent = []

            def tick(self, cid, intents, *, snapshot_version=None):
                self.sent.append(intents)
                r = {"tick": 100 + len(self.sent), "window_remaining_ms": 0, "queue": {"length": 5}}
                if intents is not None:
                    r["queue_id"] = f"q{len(self.sent)}"
                return r

        fake = Fake()
        pol = Policy(kind="scripted", goals=["explore"], on_hostile="fight", hostile=["npc"])
        r = Runner(CharacterConfig("T", "sandbox", pol, Path("t.toml")), fake, 1, threading.Event(), out=lambda _: None)
        self.addCleanup(r.trace.close)
        w = world(health=4)
        w.tick = 100
        apply_zone(w, MAP, -10, 10, {"safe": True})
        w.entities = [Entity("npc", 7, (11, 10), code="chaser")]
        hit(w)
        r.world, r.mem = w, Memory(need_self=False, need_position=False)
        r.tick()
        steps = [i for i in fake.sent[0] if i["verb"] == "Step"]
        self.assertGreater(len(steps), 1, "the whole path goes as one queue")
        r.tick()
        self.assertIsNone(fake.sent[1], "the held retreat queue is left running")


class EntityReadCadenceTest(unittest.TestCase):
    """A walk gets a real entity read every ``entity_refresh`` ticks, whatever the deltas carry."""

    def calm(self) -> Memory:
        return Memory(need_self=False, need_position=False, last_poll_tick=100, calm_poll_interval=7)

    def test_deltas_do_not_hold_off_a_read_once_we_have_moved(self):
        w, pol = world(at=(10, 10)), Policy(kind="scripted", entity_refresh=20)
        w.tick = 100
        w.apply_entities({"tick": 100})
        w.pos = w.terrain_center = (14, 10)  # walked on since that read
        w.tick = 121
        w.apply_observation({"version": 3, "delta": {"entities": {}}})  # a delta: entities_tick is fresh
        self.assertEqual(w.entities_tick, 121)
        self.assertEqual(choose_call(w, self.calm(), pol), "entities")

    def test_standing_still_lets_deltas_stand_in(self):
        w, pol = world(at=(10, 10)), Policy(kind="scripted", entity_refresh=20)
        w.tick = 100
        w.apply_entities({"tick": 100})
        w.tick = 121
        w.apply_observation({"version": 3, "delta": {"entities": {}}})
        self.assertNotEqual(choose_call(w, self.calm(), pol), "entities")

    def test_not_before_the_cadence(self):
        w, pol = world(at=(10, 10)), Policy(kind="scripted", entity_refresh=20)
        w.tick = 100
        w.apply_entities({"tick": 100})
        w.pos = w.terrain_center = (12, 10)
        w.tick = 110
        w.apply_observation({"version": 3, "delta": {"entities": {}}})
        self.assertNotEqual(choose_call(w, self.calm(), pol), "entities")


class HealCutsToFoodTest(unittest.TestCase):
    """Food behind a bush the weapon cuts is cut to, not given up ``no_path`` (A16 Walk run 4)."""

    def setUp(self):
        self.w = world(at=(10, 10), health=5)
        self.w.zones[MAP] = {(10, 10): ZoneFact(safe=False)}
        for x in range(10, 15):
            for y in range(10, 15):
                if max(abs(x - 12), abs(y - 12)) == 1:
                    self.w.view.tiles[(x, y)] = "bush"  # a ring of bushes round the berry
        self.w.entities = [Entity("supply", 8, (12, 12), "berry")]
        self.w.held_supplies = [InventorySupply(3, "pocket_knife")]
        self.w.armed_code = "pocket_knife"
        self.c = PlayContext(Memory(), Policy(kind="scripted"), random.Random(0), knowledge=KnowledgeBase("sandbox"))

    def test_cuts_the_bush_in_the_way(self):
        out = dispatch(self.w, self.c)
        self.assertEqual(out.state, "Heal")
        self.assertEqual(out.intents, [{"verb": "Use", "target": {"kind": "block", "x": 11, "y": 11}}])
        self.assertTrue(out.reason.startswith("heal_food: break cut"), out.reason)

    def test_walks_when_there_is_a_way_round(self):
        self.w.view.tiles[(13, 11)] = "dirt"  # a gap in the ring
        out = dispatch(self.w, self.c)
        self.assertEqual(out.state, "Heal")
        self.assertEqual(out.intents[0]["verb"], "SetPosition")

    def test_without_a_blade_it_does_not_cut(self):
        self.w.held_supplies, self.w.armed_code = [], None
        out = dispatch(self.w, self.c)
        self.assertFalse([i for i in out.intents or [] if i["verb"] == "Use"], out.reason)


if __name__ == "__main__":
    unittest.main()
