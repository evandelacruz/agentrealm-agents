"""Commit to the target and the goal it serves (A71).

Once a state picks a target it keeps it until it reaches it, proves it
impossible, or meets a prerequisite, which becomes a stop before it. Another
candidate coming nearer never re-picks. A valuable a few steps off the walk
is a detour stop, and the walk resumes after it. New information wakes the
planner at once, but only once, and a new head waits for the action under way.
"""

from __future__ import annotations

import random
import unittest

from agentrealm_agent import targets
from agentrealm_agent.config import Policy
from agentrealm_agent.directives import PARAM_DEFAULTS
from agentrealm_agent.healing import standing_in_safe_zone
from agentrealm_agent.item_table import InventorySupply
from agentrealm_agent.knowledge_base import KnowledgeBase
from agentrealm_agent.memory import Memory
from agentrealm_agent.navigation import stuck as nav_stuck
from agentrealm_agent.plan import Plan
from agentrealm_agent.states import PlayContext, dispatch, gather_outcome
from agentrealm_agent.states.detour import DETOUR_EXTRA_STEPS, DETOUR_TICKS, extra_steps
from agentrealm_agent.states.explore import explore_outcome
from agentrealm_agent.states.level import ENTRANCE_GOAL
from agentrealm_agent.states.travel import TRAVEL_TARGET, resolve_destination
from agentrealm_agent.travel.knowledge import entrance_key, record_shop_cell
from agentrealm_agent.world import Entity, WorldModel
from agentrealm_agent.zone_discovery import apply_zone

from tests.test_strategist import WAIT_ANSWER, FakeLLM, fake_runner, make, round_trip

MAP = 7


def field(width: int = 21, height: int = 9, at=(2, 4), fog: bool = False) -> WorldModel:
    """Open dirt walled in; with ``fog`` the outer ring is unknown, so there is frontier."""
    w = WorldModel(character_id=1, map_id=MAP, pos=at, perception=12)
    for x in range(width):
        for y in range(height):
            edge = x in (0, width - 1) or y in (0, height - 1)
            if edge and fog:
                continue
            w.view.tiles[(x, y)] = "wall" if edge else "dirt"
    w.terrain_center, w.terrain_map = at, MAP
    w.health, w.max_health = 100, 100
    w.attack_range, w.gems = 1, 0
    return w


def ctx(m: Memory, plan: Plan | None = None, kb: KnowledgeBase | None = None, **policy_kw) -> PlayContext:
    kw = {"goals": [], "on_hostile": "ignore", **policy_kw}
    return PlayContext(m, Policy(kind="scripted", **kw), random.Random(0), plan=plan, knowledge=kb)


def walk_off(w: WorldModel, m: Memory, to) -> None:
    """Another state walked the character to ``to`` and took the path."""
    w.pos = to
    m.path, m.goal = [(to[0] + 1, to[1])], "other"


class NearerCandidateTest(unittest.TestCase):
    """A target is not re-picked when another candidate becomes nearer."""

    def test_gather_keeps_its_grass_when_nearer_grass_turns_up(self):
        w = field(at=(2, 4))
        w.view.tiles[(12, 4)] = "grass"
        m = Memory()
        gather_outcome(w, m, Policy(on_hostile="ignore"))
        self.assertEqual(m.gather_target, ("grass", (12, 4)))
        walk_off(w, m, (5, 4))
        w.view.tiles[(4, 4)] = "grass"  # nearer now, behind
        out = gather_outcome(w, m, Policy(on_hostile="ignore"))
        self.assertEqual(m.gather_target, ("grass", (12, 4)))
        self.assertEqual((out.intents[0]["x"], m.path[-1]), (6, (12, 4)), "on toward it, not back")

    def test_gather_lets_go_of_grass_that_was_cut(self):
        w = field(at=(2, 4))
        w.view.tiles[(12, 4)] = "grass"
        w.view.tiles[(16, 4)] = "grass"
        m = Memory()
        gather_outcome(w, m, Policy(on_hostile="ignore"))
        w.view.tiles[(12, 4)] = "dirt"  # reached by other means
        gather_outcome(w, m, Policy(on_hostile="ignore"))
        self.assertEqual(m.gather_target, ("grass", (16, 4)))

    def test_explore_keeps_its_frontier_when_a_nearer_one_opens(self):
        w = field(fog=True, at=(15, 4))
        m = Memory()
        c = ctx(m)
        explore_outcome(w, m, c.policy, c.rng)
        first = targets.committed(m, w, "explore").target
        # Fog opens beside where another state walks the character: a frontier
        # far nearer than the one the walk heads for.
        far = first[1]
        near = (2, 4) if far[0] > 10 else (18, 4)
        del w.view.tiles[near]
        walk_off(w, m, (near[0] + (1 if near[0] < 10 else -1), 4))
        out = explore_outcome(w, m, c.policy, c.rng)
        self.assertEqual(targets.committed(m, w, "explore").target, first)
        self.assertEqual(m.path[-1], far, out.reason)

    def test_heal_keeps_its_safe_tile_when_a_nearer_one_is_found(self):
        w = field(at=(10, 4))
        w.health = 4
        apply_zone(w, MAP, 18, 4, {"safe": True, "brightness": 1})
        m = Memory()
        out = dispatch(w, ctx(m))
        self.assertEqual(out.reason, "heal_measure → (18, 4)")
        apply_zone(w, MAP, 7, 4, {"safe": True, "brightness": 1})  # nearer, found later
        w.tick += 5
        out = dispatch(w, ctx(m))
        self.assertEqual(out.reason, "heal_measure → (18, 4)")

    def test_travel_keeps_its_shop_when_a_nearer_shop_is_learned(self):
        kb = KnowledgeBase("sandbox")
        record_shop_cell(kb, MAP, (18, 4))
        w = field(at=(10, 4))
        op = {"op": "travel", "to": "shop", "x": 0, "y": 0}
        c = ctx(Memory(), Plan([op], dict(PARAM_DEFAULTS)), kb)
        self.assertEqual(resolve_destination(w, c, op).pos, (18, 4))
        record_shop_cell(kb, MAP, (8, 4))
        self.assertEqual(resolve_destination(w, c, op).pos, (18, 4))
        # Given up on: another candidate is picked.
        c.memory.nav_stuck.given_up_travel[(MAP, (18, 4))] = w.tick
        self.assertEqual(resolve_destination(w, c, op).pos, (8, 4))
        self.assertEqual(targets.committed(c.memory, w, TRAVEL_TARGET, op).target.pos, (8, 4))

    def test_a_target_proven_unreachable_is_let_go(self):
        w = field(at=(2, 4))
        w.view.tiles[(12, 4)] = "grass"
        w.view.tiles[(16, 4)] = "grass"
        m = Memory()
        gather_outcome(w, m, Policy(on_hostile="ignore"))
        self.assertEqual(m.gather_target, ("grass", (12, 4)))
        for x in (11, 12, 13):  # walled in: no path at all
            for y in (3, 4, 5):
                if (x, y) != (12, 4):
                    w.view.tiles[(x, y)] = "wall"
        m.path, m.goal = [], ""
        gather_outcome(w, m, Policy(on_hostile="ignore"))
        self.assertEqual(m.gather_target, ("grass", (16, 4)))

    def test_a_commitment_lapses_once_nothing_pursues_it(self):
        w, m = field(), Memory()
        self.assertEqual(targets.hold(m, w, "g", lambda: (1, 1), lambda t: True), (1, 1))
        w.tick += targets.LAPSE_TICKS + 1
        self.assertEqual(targets.hold(m, w, "g", lambda: (2, 2), lambda t: True), (2, 2))


class PrerequisiteTest(unittest.TestCase):
    """A prerequisite is a stop inserted before the target, which stays."""

    def kb(self) -> KnowledgeBase:
        kb = KnowledgeBase("sandbox")
        kb.entrances[entrance_key(MAP, (18, 4))] = {"map_id": MAP, "x": 18, "y": 4, "locked": True, "needs": "key"}
        return kb

    def test_the_key_is_a_stop_before_the_locked_entrance(self):
        w = field(at=(10, 4))
        w.view.tiles[(18, 4)] = "framed_door"
        w.entities = [Entity("supply", 51, (6, 6), "key"), Entity("supply", 52, (12, 6), "keystone")]
        m = Memory()
        plan = Plan([{"op": "enter_level", "x": 18, "y": 4}], dict(PARAM_DEFAULTS))
        out = dispatch(w, ctx(m, plan, self.kb()))
        self.assertEqual(out.state, "Level")
        self.assertEqual(m.path[-1], (6, 6), out.reason)
        c = m.targets[ENTRANCE_GOAL]
        self.assertEqual(c.target, (MAP, (18, 4)), "the entrance stays the target")
        self.assertEqual([(s.pos, s.why) for s in c.stops], [((6, 6), "prerequisite: key")])
        w.pos = (7, 5)  # beside the key
        out = dispatch(w, ctx(m, plan, self.kb()))
        self.assertEqual(out.intents, [{"verb": "Take", "supply_id": 51}])
        # Taken: the stop is done and the walk resumes toward the entrance.
        w.entities = []
        w.held_supplies = [InventorySupply(51, "key")]
        out = dispatch(w, ctx(m, plan, self.kb()))
        self.assertEqual(m.targets[ENTRANCE_GOAL].stops, [])
        self.assertEqual((out.state, m.path[-1]), ("Level", (18, 4)), out.reason)

    def test_no_stop_when_the_item_is_carried(self):
        w = field(at=(10, 4))
        w.view.tiles[(18, 4)] = "framed_door"
        w.entities = [Entity("supply", 51, (6, 6), "key")]
        w.held_supplies = [InventorySupply(9, "key")]
        m = Memory()
        plan = Plan([{"op": "enter_level", "x": 18, "y": 4}], dict(PARAM_DEFAULTS))
        out = dispatch(w, ctx(m, plan, self.kb()))
        self.assertEqual(m.path[-1], (18, 4), out.reason)
        self.assertEqual(m.targets[ENTRANCE_GOAL].stops, [])


class DetourTest(unittest.TestCase):
    """A gem 2 steps off the path is detoured to, and the walk then resumes."""

    def play(self, w: WorldModel, m: Memory, plan: Plan, decisions: int) -> list:
        outs = []
        for _ in range(decisions):
            o = dispatch(w, ctx(m, plan, pickup=True))
            outs.append(o)
            for i in o.intents or []:
                if i["verb"] == "SetPosition":
                    w.pos = (i["x"], i["y"])
                    if m.path and m.path[0] == w.pos:
                        m.path = m.path[1:]
                    nav_stuck.on_step(m, w)
                elif i["verb"] == "Take":
                    w.entities = [e for e in w.entities if e.id != i["supply_id"]]
                    w.gems += 1
            o.at = w.pos
            w.tick += 4
        return outs

    def test_a_gem_two_steps_off_the_path_is_taken_then_the_walk_resumes(self):
        w = field(at=(2, 4))
        plan = Plan([{"op": "travel", "to": "point", "x": 18, "y": 4}], dict(PARAM_DEFAULTS))
        m = Memory()
        self.assertEqual(self.play(w, m, plan, 1)[0].state, "Travel")
        x, y = m.path[5]
        gem = (x, y + 2 if y + 2 <= 7 else y - 2)
        w.entities = [Entity("supply", 77, gem, "gem")]  # comes into view, 2 steps off the path
        outs = self.play(w, m, plan, 30)
        states = [o.state for o in outs if o.intents]
        self.assertEqual(states[0], "Detour", outs[0].reason)
        take = next(i for i, o in enumerate(outs) if o.intents and o.intents[0]["verb"] == "Take")
        self.assertEqual(outs[take].state, "Pickup")
        self.assertEqual(w.gems, 1)
        arrived = [o.at for o in outs].index((18, 4))
        movers = {o.state for o in outs[take + 1 : arrived + 1] if o.intents}
        self.assertEqual(movers, {"Travel"}, "the committed walk resumes")
        self.assertIsNone(m.detour)

    def test_what_is_not_a_valuable_is_left(self):
        w = field(at=(2, 4))
        plan = Plan([{"op": "travel", "to": "point", "x": 18, "y": 4}], dict(PARAM_DEFAULTS))
        m = Memory()
        self.play(w, m, plan, 1)
        w.entities = [Entity("supply", 78, (8, 6), "dirt_clod"), Entity("supply", 79, (9, 6), "apple")]  # food while not hurt
        self.assertEqual(self.play(w, m, plan, 1)[0].state, "Travel")

    def test_a_detour_that_times_out_gives_its_find_up(self):
        w = field(at=(2, 4))
        plan = Plan([{"op": "travel", "to": "point", "x": 18, "y": 4}], dict(PARAM_DEFAULTS))
        m = Memory()
        self.play(w, m, plan, 1)
        x, y = m.path[5]
        gem = (x, y + 2 if y + 2 <= 7 else y - 2)
        w.entities = [Entity("supply", 77, gem, "gem")]
        self.assertEqual(dispatch(w, ctx(m, plan, pickup=True)).state, "Detour")
        w.tick += DETOUR_TICKS  # never got there
        out = dispatch(w, ctx(m, plan, pickup=True))
        self.assertEqual(out.state, "Travel", out.reason)
        self.assertIn(77, m.detour_skipped)
        self.assertIsNone(m.detour)

    def test_extra_steps_bound(self):
        path = [(x, 4) for x in range(3, 19)]
        self.assertLessEqual(extra_steps((2, 4), (8, 6), path), DETOUR_EXTRA_STEPS)
        self.assertIsNone(extra_steps((2, 4), (8, 8), path), "further than DETOUR_REACH from the path")

    def test_a_find_beside_a_hostile_is_left(self):
        w = field(at=(2, 4))
        plan = Plan([{"op": "travel", "to": "point", "x": 18, "y": 4}], dict(PARAM_DEFAULTS))
        m = Memory()
        self.play(w, m, plan, 1)
        x, y = m.path[5]
        gem = (x, y + 2 if y + 2 <= 7 else y - 2)
        w.hostile_types.add(("npc", "gnawer"))
        w.entities = [Entity("supply", 77, gem, "gem"), Entity("npc", 5, (gem[0] + 1, gem[1]), "gnawer")]
        out = dispatch(w, ctx(m, plan, pickup=True, hostile=["npc"], hostile_range=1))
        self.assertNotEqual(out.state, "Detour")


ENTRANCE_ROW = {"map_id": MAP, "x": 40, "y": 10}


class DiscoveryTest(unittest.TestCase):
    """New information wakes the planner at once, once."""

    def test_a_new_entrance_triggers_one_replan(self):
        llm = FakeLLM(WAIT_ANSWER, WAIT_ANSWER, WAIT_ANSWER)
        s, r = make(llm, replan_s=15), fake_runner()
        r.knowledge = KnowledgeBase("sandbox")
        round_trip(s, r)  # the first call primes what is known
        self.assertEqual(llm.calls, 1)
        s.clock.now += DISCOVERY_WAIT
        r.knowledge.entrances[entrance_key(MAP, (40, 10))] = dict(ENTRANCE_ROW)
        for _ in range(5):  # seen again on every window
            round_trip(s, r)
            s.clock.now += 1
        self.assertEqual(llm.calls, 2, "one replan, before the timer")
        asks = [c.args[2]["strategist"] for c in r.log.call_args_list if c.args[2]["strategist"]["event"] == "ask"]
        self.assertEqual([t["trigger"] for t in asks[1]["triggers"]], ["discovery"])
        self.assertEqual(asks[1]["triggers"][0]["finds"], [{"kind": "entrance", "map_id": MAP, "cell": [40, 10]}])

    def test_finds_close_together_are_one_replan(self):
        llm = FakeLLM(WAIT_ANSWER, WAIT_ANSWER, WAIT_ANSWER)
        s, r = make(llm, replan_s=60), fake_runner()
        r.knowledge = KnowledgeBase("sandbox")
        round_trip(s, r)
        s.clock.now += 1  # inside the gap: the find waits for it
        r.world.entities = [Entity("npc", 3, (2, 2), "villager")]
        round_trip(s, r)
        r.knowledge.entrances[entrance_key(MAP, (40, 10))] = dict(ENTRANCE_ROW)
        round_trip(s, r)
        self.assertEqual(llm.calls, 1)
        s.clock.now += DISCOVERY_WAIT
        round_trip(s, r)
        self.assertEqual(llm.calls, 2)
        asks = [c.args[2]["strategist"] for c in r.log.call_args_list if c.args[2]["strategist"]["event"] == "ask"]
        self.assertEqual([f["kind"] for f in asks[1]["triggers"][0]["finds"]], ["npc", "entrance"])

    def test_an_item_the_gems_can_now_buy(self):
        s, r = make(), fake_runner()
        r.world.entities = [Entity("supply", 8, (3, 3), "small_potion", gem_price=5)]
        r.world.gems = 2
        s._collect(r)
        r.world.gems = 5
        s._collect(r)
        self.assertEqual(s.inbox[-1]["finds"], [{"kind": "affordable", "code": "small_potion", "price": 5, "gems": 5}])

    def test_a_new_head_waits_for_the_walk_under_way(self):
        travel = {"op": "travel", "to": "town", "x": 0, "y": 0}
        llm = FakeLLM({"goals": [travel]})
        s, r = make(llm), fake_runner()
        head = r.plan.current()
        r.mem.held_queue = {"queue_id": "q", "next_index": 1}
        r.mem.path, r.mem.goal = [(1, 0)], "explore_area"
        round_trip(s, r)
        self.assertIs(r.plan.current(), head, "mid-walk: the old head carries on")
        self.assertEqual(r.mem.path, [(1, 0)])
        r.mem.held_queue = None  # the queue ran out: an action boundary
        s.on_window(r)
        self.assertEqual(r.plan.current()["to"], "town")
        self.assertEqual(r.mem.path, [])

    def test_a_deferred_head_takes_over_after_the_longest_wait(self):
        llm = FakeLLM({"goals": [{"op": "travel", "to": "town", "x": 0, "y": 0}]})
        s, r = make(llm), fake_runner()
        head = r.plan.current()
        r.mem.held_queue = {"queue_id": "q", "next_index": 1}  # a queue that never ends
        round_trip(s, r)
        s.clock.now += DEFER_WAIT - 0.1
        s.on_window(r)
        self.assertIs(r.plan.current(), head)
        s.clock.now += 0.1
        s.on_window(r)
        self.assertEqual(r.plan.current()["to"], "town")

    def test_a_deferred_head_is_dropped_when_the_stack_changed(self):
        llm = FakeLLM({"goals": [{"op": "travel", "to": "town", "x": 0, "y": 0}]})
        s, r = make(llm), fake_runner()
        r.mem.held_queue = {"queue_id": "q", "next_index": 1}
        round_trip(s, r)
        r.plan.finish_current("explored", memory=r.mem)  # the old head ended while it waited
        r.mem.held_queue = None
        s.on_window(r)
        self.assertIsNone(s.deferred)
        self.assertIsNone(r.plan.current(), "the stale reply did not overwrite the stack")


DISCOVERY_WAIT = 5.0  # discovery.DISCOVERY_GAP_S, spelled out so the test reads the spec
DEFER_WAIT = 10.0  # strategist.DEFER_MAX_S


if __name__ == "__main__":
    unittest.main()
