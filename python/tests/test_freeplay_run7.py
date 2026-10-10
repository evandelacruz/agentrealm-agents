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
3. Gather's walks that #162 left unpriced (to an unseen named region, and
   off a shadowing hostile) keep clear of known hostile ground too.
"""

from __future__ import annotations

import json
import random
import unittest
from types import SimpleNamespace
from unittest import mock

from agentrealm_agent.config import Policy
from agentrealm_agent.directives import Directives, PARAM_DEFAULTS
from agentrealm_agent.gem_yield import GemYieldTracker
from agentrealm_agent.hostile_ground import ground_by_hostile
from agentrealm_agent.item_table import InventorySupply
from agentrealm_agent.memory import Memory
from agentrealm_agent.pathing import RETREAT_TARGET, committed_safe, grid_params, reachable_safe_goal
from agentrealm_agent.plan import Plan
from agentrealm_agent.states import PlayContext, dispatch
from agentrealm_agent.states.gather import gather_outcome
from agentrealm_agent.states.retreat import retreat_step
from agentrealm_agent.strategist import Asked, Strategist, StrategistConfig
from agentrealm_agent.world import POST_STILL_TICKS, Entity, WorldModel, chebyshev
from agentrealm_agent.zone_discovery import apply_zone

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

    def test_with_no_refuge_retreat_steps_away_instead_of_back_to_the_hostile(self):
        """Every safe cell lies in the hostile's reach: Retreat opens distance,
        never walks toward the one beside its pursuer."""
        w, c = world(health=2), ctx()
        apply_zone(w, MAP, FAR_SAFE[0], FAR_SAFE[1], {"safe": False})
        e = gristle()
        w.entities = [e]
        hit_by(w, e)
        out = dispatch(w, c)
        self.assertEqual((out.state, out.reason), ("Retreat", "no refuge outside hostile ground: open distance"))
        self.assertGreater(chebyshev(step(out), e.pos), chebyshev(w.pos, e.pos))

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

    def test_a_remembered_post_rules_a_safe_tile_out_for_heal(self):
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
        self.assertIn(NEAR_SAFE, ground_by_hostile(w, c.policy)[("npc", 7)])
        out = dispatch(w, c)
        self.assertEqual((out.state, out.reason), ("Heal", f"heal_measure → {FAR_SAFE}"))

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
    return SimpleNamespace(
        world=w,
        mem=Memory(),
        plan=Plan([dict(g) for g in goals], dict(PARAM_DEFAULTS)),
        directives=SimpleNamespace(directives=Directives(params=dict(PARAM_DEFAULTS), goals=[])),
        knowledge=None,
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


GATHER_MAP = 3


def field(at=(2, 10)) -> WorldModel:
    w = WorldModel(character_id=1, map_id=GATHER_MAP, pos=at, perception=6)
    for x in range(0, 60):
        for y in range(0, 21):
            w.view.tiles[(x, y)] = "dirt"
    w.terrain_center, w.terrain_map = at, GATHER_MAP
    w.health, w.max_health = 100, 100
    w.hostile_types.add(HOSTILE)
    return w


def guard_post_left_behind(w: WorldModel, post=(14, 10), at=(2, 10)) -> None:
    """A guard stood on ``post`` long enough to keep it; then it was out of view."""
    guard = Entity("npc", 9, post, HOSTILE[1])
    w.pos = (post[0] - 3, post[1])
    w.tick = 0
    w._set_entities([guard], 0)
    w.tick = POST_STILL_TICKS
    w._set_entities([guard], POST_STILL_TICKS)
    w.pos = w.terrain_center = at
    w.tick = POST_STILL_TICKS + 1
    w._set_entities([], w.tick)


GATHER_POLICY = Policy(kind="scripted", goals=[], on_hostile="ignore", hostile_range=2)
POST_GROUND = {(14 + dx, 10 + dy) for dx in range(-3, 4) for dy in range(-3, 4)}


def corridor(w: WorldModel) -> None:
    """Walls but for a corridor along y 9..11 that runs past the post; the
    named region (x 32..47) unknown."""
    for cell in list(w.view.tiles):
        if cell[0] >= 32:
            del w.view.tiles[cell]
        elif not 9 <= cell[1] <= 11:
            w.view.tiles[cell] = "wall"
    for x in range(-1, 33):
        w.view.tiles[(x, -1)] = w.view.tiles[(x, 21)] = "wall"
    for y in range(-1, 22):
        w.view.tiles[(-1, y)] = "wall"


class GatherWalksTest(unittest.TestCase):
    """Item 3: every Gather walk keeps clear of known hostile ground."""

    def test_the_walk_to_an_unseen_region_takes_no_way_past_a_remembered_post(self):
        w, m = field(), Memory()
        guard_post_left_behind(w)
        corridor(w)
        out = gather_outcome(w, m, GATHER_POLICY, op={"op": "gather_gems", "count": 99, "x": 40, "y": 10})
        self.assertFalse(set(m.path) & POST_GROUND, (out.reason, m.path))
        self.assertNotEqual((m.gather_target or ("",))[0], "region")

    def test_moving_off_a_shadow_keeps_off_a_remembered_post(self):
        """The nearest ground far enough from the shadow lies on another
        hostile's post: the move-off goes elsewhere, by a clear route."""
        w, m = field(at=(12, 10)), Memory()
        guard_post_left_behind(w, post=(23, 1), at=(12, 10))  # on the far cell the old walk picked, (22, 0)
        shadow = Entity("npc", 3, (10, 10), HOSTILE[1])
        w.entities = [shadow]
        out = gather_outcome(w, m, GATHER_POLICY, op={"op": "gather_gems", "count": 99}, shadow=shadow)
        self.assertEqual(m.gather_target[0], "off", out.reason)
        post_ground = {(23 + dx, 1 + dy) for dx in range(-3, 4) for dy in range(-3, 4)}
        self.assertNotIn(m.gather_target[1], post_ground)
        self.assertFalse(set(m.path) & post_ground, m.path)

    def test_moving_off_a_shadow_takes_the_post_ground_when_the_op_fights(self):
        w, m = field(at=(12, 10)), Memory()
        guard_post_left_behind(w, post=(23, 1), at=(12, 10))
        shadow = Entity("npc", 3, (10, 10), HOSTILE[1])
        w.entities = [shadow]
        op = {"op": "gather_gems", "count": 99, "fight": True}
        gather_outcome(w, m, GATHER_POLICY, op=op, shadow=shadow)
        self.assertEqual(m.gather_target, ("off", (22, 0)), "the nearest far cell, post or not")

    def test_the_walk_to_an_unseen_region_takes_the_fight_when_the_op_says(self):
        w, m = field(), Memory()
        guard_post_left_behind(w)
        corridor(w)
        op = {"op": "gather_gems", "count": 99, "x": 40, "y": 10, "fight": True}
        out = gather_outcome(w, m, GATHER_POLICY, op=op)
        self.assertTrue(out.intents, out.reason)
        self.assertTrue(set(m.path) & POST_GROUND, "past the post: the op chose the risk")


if __name__ == "__main__":
    unittest.main()
