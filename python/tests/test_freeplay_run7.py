"""Free-play run 7 offline: Retreat and Flee pulled opposite ways, and a stale planner reply re-added a finished op (A9, A10, A35, A22).

1. For 19 s beside a hostile, Retreat aimed at a safe tile 2 cells from it
   while Flee stepped away: 6 hits, 10 health to 1. A safe tile is a
   refuge only outside every known hostile's ground, in view or
   remembered, the one Retreat runs from included
   (``hostile_ground.ground_by_hostile``, ``pathing.retreat_safe_goal``),
   and Flee runs toward the refuge Retreat picks, so the two agree.
2. A reply asked before ``buy matches`` finished was applied after it and
   put it back; buy-one-more bought a second pair with the last gems. A
   reply is reconciled with what changed since its prompt
   (``strategist.Asked``).
3. Gather's walk to an unseen named region keeps clear of known hostile
   ground too (``test_gather.KeepsClearTest``).
"""

from __future__ import annotations

import json
import random
import unittest
from types import SimpleNamespace
from unittest import mock

from agentrealm_adk.config import Policy
from agentrealm_adk.directives import Directives, PARAM_DEFAULTS
from agentrealm_adk.gem_yield import GemYieldTracker
from agentrealm_adk.hostile_ground import ground_by_hostile
from agentrealm_adk.item_table import InventorySupply
from agentrealm_adk.memory import Memory
from agentrealm_adk.pathing import RETREAT_TARGET, committed_safe, grid_params, reachable_safe_goal
from agentrealm_agent.plan import Plan
from agentrealm_agent.states import PlayContext, dispatch
from agentrealm_agent.states.retreat import retreat_step
from agentrealm_agent.strategist import Asked, Strategist, StrategistConfig
from agentrealm_adk.world import POST_STILL_TICKS, Entity, WorldModel, chebyshev
from agentrealm_adk.zone_discovery import apply_zone
from tests.test_strategist import SOLD_HERE, shop_selling

MAP = 1
HOSTILE = ("npc", "fake_gristle")
NEAR_SAFE = (6, 10)  # 2 cells from the hostile, nearest to the agent
FAR_SAFE = (16, 10)  # the other way, out of every reach


def world(at=(9, 10), health=10) -> WorldModel:
    w = WorldModel(character_id=1, map_id=MAP, pos=at, perception=8)
    for x in range(-10, 30):
        for y in range(0, 21):
            w.view.tiles[(x, y)] = "dirt"
    w.terrain_center, w.terrain_map = at, MAP
    w.alive, w.tick = True, 1000
    w.health, w.max_health, w.lives = health, 10, 5
    w.hostile_types.add(HOSTILE)
    for cell in (NEAR_SAFE, FAR_SAFE):
        apply_zone(w, MAP, cell[0], cell[1], {"safe": True})
    return w


def ctx(m: Memory | None = None, **kw) -> PlayContext:
    policy = Policy(kind="scripted", **{"on_hostile": "flee", "hostile_range": 2, **kw})
    return PlayContext(m or Memory(), policy, random.Random(0))


def gristle(at=(8, 10)) -> Entity:
    return Entity("npc", 7, at, HOSTILE[1])


def hit_by(w: WorldModel, e: Entity, damage: int = 2) -> None:
    w.attacked_tick, w.attacker = w.tick, (e.kind, e.id)
    w.threat.record(HOSTILE, damage)


def step(out) -> tuple[int, int]:
    return (out.intents[0]["x"], out.intents[0]["y"])


class RefugeTest(unittest.TestCase):
    """Item 1: one threat picture for Retreat, Flee, Park and Heal."""

    def test_retreat_skips_the_safe_tile_beside_the_hostile_it_runs_from(self):
        w, c = world(health=2), ctx()
        e = gristle()
        w.entities = [e]
        hit_by(w, e)
        out = dispatch(w, c)
        self.assertEqual(out.state, "Retreat")
        self.assertEqual(out.reason, f"retreat → safe {FAR_SAFE}")
        self.assertGreater(step(out)[0], w.pos[0], "away from the hostile, not back past it")

    def test_with_no_refuge_flee_runs_its_escape_without_pacing(self):
        """Every safe cell lies where the hostile gets first: Retreat sends
        nothing, so Flee's committed escape runs, decision after decision,
        opening distance and never stepping back onto a cell (review on
        #168: a greedy step from Retreat would take over from it)."""
        w, c = world(health=2), ctx()
        apply_zone(w, MAP, FAR_SAFE[0], FAR_SAFE[1], {"safe": False})
        e = gristle()
        w.entities = [e]
        seen = [w.pos]
        for _ in range(6):
            hit_by(w, e)
            c.memory.held_queue = None
            out = dispatch(w, c)
            self.assertEqual(out.state, "Flee", out.reason)
            self.assertTrue(out.intents, out.reason)
            nxt = step(out)
            self.assertNotIn(nxt, seen, "no pacing")
            self.assertGreaterEqual(chebyshev(nxt, e.pos), chebyshev(w.pos, e.pos), out.reason)
            seen.append(nxt)
            w.pos = w.terrain_center = nxt
            w.tick += 5
        self.assertNotIn(NEAR_SAFE, seen)

    def test_with_no_refuge_and_nothing_near_park_sends_nothing(self):
        w, c = world(), ctx()
        apply_zone(w, MAP, FAR_SAFE[0], FAR_SAFE[1], {"safe": False})
        guard = gristle(at=(4, 10))
        w.perception = 4
        w.pos = (6, 12)
        w.tick = 0
        w._set_entities([guard], 0)
        w.tick = POST_STILL_TICKS
        w._set_entities([guard], POST_STILL_TICKS)
        w.pos = w.terrain_center = (9, 10)
        w.tick = POST_STILL_TICKS + 1
        w._set_entities([], w.tick)  # its post holds the one safe tile, out of view
        out = retreat_step(w, c, "Park")
        self.assertEqual((out.intents, out.reason), (None, "no refuge outside hostile ground"))

    def test_flee_runs_toward_the_refuge_retreat_picks(self):
        w, c = world(health=10), ctx()
        w.entities = [gristle()]
        out = dispatch(w, c)
        self.assertEqual(out.state, "Flee")
        self.assertEqual(c.memory.flee_path[-1], FAR_SAFE, c.memory.flee_path)
        self.assertEqual(committed_safe(c.memory, w, RETREAT_TARGET), FAR_SAFE, "the same cell Retreat walks to")

    def test_flee_and_retreat_pull_the_same_way_as_health_crosses_the_floor(self):
        """The run's pattern: Flee, then Retreat once hit low, then Flee again
        once out of range, with the hostile a step behind all the way. Every
        step opens distance from it and closes on the refuge, until the
        character stands on it (review on #168: a pursuer a step behind must
        not cover the refuge)."""
        w, c = world(health=10), ctx()
        e = gristle()
        w.entities = [e]
        states = []
        for i in range(20):
            if w.pos == FAR_SAFE:
                break
            w.health = (10, 2, 2, 10)[i % 4]
            if w.health == 2:
                hit_by(w, e)
            c.memory.held_queue = None
            out = dispatch(w, c)
            states.append(out.state)
            self.assertTrue(out.intents, (out.state, out.reason))
            nxt = step(out)
            self.assertGreaterEqual(chebyshev(nxt, e.pos), chebyshev(w.pos, e.pos), (out.state, out.reason))
            self.assertLess(chebyshev(nxt, FAR_SAFE), chebyshev(w.pos, FAR_SAFE), (out.state, out.reason))
            e.pos, w.pos = w.pos, nxt  # it keeps pace, a step behind
            w.terrain_center = nxt
            if c.memory.path[:1] == [nxt]:
                del c.memory.path[0]  # walked, as the runner trims it
            w.tick += 5
        self.assertEqual(w.pos, FAR_SAFE, states)
        self.assertIn("Flee", states)
        self.assertIn("Retreat", states)

    def test_a_remembered_post_rules_a_safe_tile_out_of_the_refuge_pick(self):
        w, c = world(at=(9, 10), health=4), ctx(on_hostile="fight")
        w.perception = 4
        guard = gristle(at=(4, 10))
        w.pos = (6, 12)
        w.tick = 0
        w._set_entities([guard], 0)
        w.tick = POST_STILL_TICKS
        w._set_entities([guard], POST_STILL_TICKS)
        w.pos = w.terrain_center = (9, 10)
        w.tick = POST_STILL_TICKS + 1
        w._set_entities([], w.tick)  # out of view now; its post is remembered
        reach = ground_by_hostile(w, c.policy)
        self.assertIn(NEAR_SAFE, reach[("npc", 7)])
        params = grid_params(c.policy, set(), set())
        self.assertEqual(reachable_safe_goal(c.memory, w, [NEAR_SAFE, FAR_SAFE], params, None, reach), FAR_SAFE)

    def test_town_is_no_refuge_inside_a_hostiles_reach(self):
        w, m = world(), Memory()
        params = grid_params(Policy(kind="scripted"), set(), set())
        town = (12, 3)
        reach = {("npc", 7): {NEAR_SAFE, FAR_SAFE, town}}
        self.assertIsNone(reachable_safe_goal(m, w, [NEAR_SAFE, FAR_SAFE], params, town, reach))
        self.assertEqual(reachable_safe_goal(m, w, [NEAR_SAFE, FAR_SAFE], params, (12, 20), reach), (12, 20))


BUY = {"op": "buy", "code": "matches"}
WAIT = {"op": "wait", "seconds": 5, "why": "test"}


class FakeLLM:
    def __init__(self, *replies) -> None:
        self.replies = list(replies)

    def complete(self, messages):
        return json.dumps(self.replies.pop(0)), {"prompt_tokens": 10, "completion_tokens": 5}


class Clock:
    now = 1000.0

    def __call__(self) -> float:
        return self.now


def runner(goals: list[dict]) -> SimpleNamespace:
    w = WorldModel(character_id=1, map_id=7, pos=(0, 0), tick=10)
    w.alive, w.gems = True, 3
    w.self_read = w.inventory_read = True
    return SimpleNamespace(
        world=w,
        mem=Memory(),
        plan=Plan([dict(g) for g in goals], dict(PARAM_DEFAULTS)),
        directives=SimpleNamespace(directives=Directives(params=dict(PARAM_DEFAULTS), goals=[])),
        knowledge=shop_selling(*SOLD_HERE),
        tick_hz=10,
        server_tick=10,
        log=mock.MagicMock(),
        acceptance=None,
        gem_cuts=GemYieldTracker(),
    )


def strategist(*replies) -> Strategist:
    return Strategist(config=StrategistConfig(provider="openai", model="m", api_key="k"), client=FakeLLM(*replies), clock=Clock())


def buy_finishes(r) -> None:
    """The buy at the top completes: one more pair held."""
    r.plan.advance(r.world, r.mem)
    r.world.held_supplies.append(InventorySupply(len(r.world.held_supplies) + 1, "matches"))
    r.plan.advance(r.world, r.mem)


class StaleReplyTest(unittest.TestCase):
    """Item 2: a reply is applied to the stack as it is now."""

    def ask_while(self, r, s, meanwhile) -> None:
        s.on_window(r)  # sends, with the stack as it is
        self.assertIsNotNone(s.asked)
        meanwhile(r)
        s.serve_one(timeout=0)
        s.on_window(r)  # settles

    def test_an_op_that_finished_while_asked_is_not_put_back(self):
        r, s = runner([BUY, WAIT]), strategist({"goals": [BUY, WAIT]})
        self.ask_while(r, s, buy_finishes)
        self.assertEqual([g["op"] for g in r.plan.goals[r.plan.index :]], ["wait"])

    def test_a_second_copy_is_the_planner_asking_for_another(self):
        r, s = runner([BUY, WAIT]), strategist({"goals": [BUY, BUY, WAIT]})
        self.ask_while(r, s, buy_finishes)
        self.assertEqual([g["op"] for g in r.plan.goals[r.plan.index :]], ["buy", "wait"])
        r.plan.advance(r.world, r.mem)
        self.assertEqual(r.plan.current()["op"], "buy", "the pair bought since counts as the first")
        r.world.held_supplies.append(InventorySupply(9, "matches"))
        r.plan.advance(r.world, r.mem)
        self.assertEqual(r.plan.current()["op"], "wait", "and the second one is bought")

    def test_an_op_dropped_while_asked_is_not_put_back(self):
        r, s = runner([BUY, WAIT]), strategist({"goals": [BUY, WAIT]})
        self.ask_while(r, s, lambda r: r.plan.drop_current("no shop sells it", r.mem))
        self.assertEqual([g["op"] for g in r.plan.goals[r.plan.index :]], ["wait"])

    def test_a_new_buy_counts_what_was_held_when_asked(self):
        """One picked up while the call was out is the one more the planner wanted."""
        r, s = runner([WAIT]), strategist({"goals": [BUY, WAIT]})
        self.ask_while(r, s, lambda r: r.world.held_supplies.append(InventorySupply(1, "matches")))
        self.assertEqual(r.plan.current()["op"], "buy")
        r.plan.advance(r.world, r.mem)
        self.assertEqual(r.plan.current()["op"], "wait", "no second pair bought")

    def test_drop_ended_keeps_ops_the_prompt_never_showed(self):
        asked = Asked(stack=[WAIT], held=[])
        asked.done.append(BUY)
        self.assertEqual(asked.drop_ended([BUY, WAIT]), ([BUY, WAIT], []))
        asked = Asked(stack=[BUY, WAIT], held=[])
        asked.note({"trigger": "goal_done", "op": dict(BUY, why="reworded")})
        self.assertEqual(asked.drop_ended([dict(BUY, why="other"), WAIT]), ([WAIT], [dict(BUY, why="other")]))


if __name__ == "__main__":
    unittest.main()
